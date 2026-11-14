"""Headless Twitch Drops client (no GUI, no browser).

The request flow is based on TwitchDropsMiner by DevilXD (AGPL-3.0),
so this project is AGPL-3.0 as well.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
from yarl import URL

from gql_ops import OPS

log = logging.getLogger("twitch")

ANDROID = {
    "url": "https://www.twitch.tv",
    "client_id": "kd1unb4b3q4t58fwlpcbzcbnm76a8fp",
    "ua": (
        "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
        "tv.twitch.android.app/31.8.0/3108006"
    ),
}
WEB = {
    "url": "https://www.twitch.tv",
    "client_id": "kimne78kx3ncx6brgo4mv6wki5h1ko",
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    ),
}
CLIENTS = {c["client_id"]: c for c in (ANDROID, WEB)}
SPECIAL_GAME_IDS = {509663, 509672}  # "Special Events": drops earnable on any stream
_CLAIM_GRACE = timedelta(hours=24)
HASH_SOURCE = "https://raw.githubusercontent.com/DevilXD/TwitchDropsMiner/master/constants.py"


class TwitchError(Exception):
    pass


class AuthError(TwitchError):
    pass


class QueryOutdated(TwitchError):
    """Twitch no longer knows one of the persisted query hashes."""


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def slugify(name: str) -> str:
    s = re.sub(r"'", "", name.lower())
    s = re.sub(r"\W+", "-", s)
    return re.sub(r"-{2,}", "-", s.strip("-"))


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------- data


@dataclass
class Drop:
    id: str
    name: str
    benefits: list[tuple[str, str, str]]  # (benefit_id, name, distributionType)
    starts: datetime
    ends: datetime
    required: int
    current: int
    claim_id: str | None
    claimed: bool
    preconditions: list[str]
    campaign: "Campaign | None" = field(default=None, repr=False)

    @property
    def remaining(self) -> int:
        return max(self.required - self.current, 0)

    @property
    def reward_text(self) -> str:
        return ", ".join(b[1] for b in self.benefits) or self.name


@dataclass
class Campaign:
    id: str
    name: str
    status: str
    game_id: int
    game_name: str
    game_slug: str
    starts: datetime
    ends: datetime
    linked: bool
    channels: list[tuple[int, str]]  # ACL: (channel_id, login); empty = any channel
    drops: dict[str, Drop] = field(default_factory=dict)

    @property
    def badge_like(self) -> bool:
        return any(b[2] in ("BADGE", "EMOTE") for d in self.drops.values() for b in d.benefits)

    @property
    def active(self) -> bool:
        return self.status != "EXPIRED" and self.starts <= datetime.now(timezone.utc) < self.ends

    @property
    def eligible(self) -> bool:
        # badge/emote campaigns need no account linking
        return self.badge_like or self.linked

    def earnable(self, now: datetime | None = None) -> list[Drop]:
        now = now or datetime.now(timezone.utc)
        chain = {p for d in self.drops.values() if not d.claimed for p in d.preconditions}
        out = []
        for d in self.drops.values():
            if d.claimed or d.required <= 0 or not (d.starts <= now < d.ends):
                continue
            if not all((self.drops[p].claimed if p in self.drops else True) for p in d.preconditions):
                continue
            if not d.benefits and d.id not in chain:
                continue
            out.append(d)
        return out

    def claimable(self) -> list[Drop]:
        """Drops whose minutes are done but which haven't been claimed yet."""
        now = datetime.now(timezone.utc)
        return [
            d for d in self.drops.values()
            if not d.claimed and d.required > 0 and d.current >= d.required
            and now < self.ends + _CLAIM_GRACE
        ]



@dataclass
class Channel:
    id: int
    login: str
    broadcast_id: int
    game_id: int | None
    game_name: str
    display: str = ""

    @property
    def name(self) -> str:
        return self.display or self.login


def parse_drop(d: dict, claimed_benefits: dict[str, datetime]) -> Drop:
    benefits = [
        (b["benefit"]["id"], b["benefit"]["name"], b["benefit"].get("distributionType", "UNKNOWN"))
        for b in (d.get("benefitEdges") or [])
    ]
    starts, ends = ts(d["startAt"]), ts(d["endAt"])
    claim_id, claimed, current = None, False, 0
    selfd = d.get("self")
    if selfd:
        claim_id = selfd.get("dropInstanceID")
        claimed = bool(selfd.get("isClaimed"))
        current = selfd.get("currentMinutesWatched") or 0
    else:
        dts = [claimed_benefits[b[0]] for b in benefits if b[0] in claimed_benefits]
        if dts and all(starts <= x < ends for x in dts):
            claimed = True
    required = d["requiredMinutesWatched"]
    if claimed:
        current = required
    return Drop(
        id=d["id"], name=d["name"], benefits=benefits, starts=starts, ends=ends,
        required=required, current=current, claim_id=claim_id, claimed=claimed,
        preconditions=[p["id"] for p in (d.get("preconditionDrops") or [])],
    )


def parse_campaign(d: dict, claimed_benefits: dict[str, datetime]) -> Campaign:
    game = d["game"]
    name = game.get("displayName") or game["name"]
    allow = d.get("allow") or {}
    channels = (
        [(int(c["id"]), c["name"]) for c in (allow.get("channels") or [])]
        if allow.get("isEnabled", True) else []
    )
    c = Campaign(
        id=d["id"], name=d["name"], status=d["status"], game_id=int(game["id"]),
        game_name=name, game_slug=game.get("slug") or slugify(name),
        starts=ts(d["startAt"]), ends=ts(d["endAt"]),
        linked=bool((d.get("self") or {}).get("isAccountConnected")), channels=channels,
    )
    for dd in d["timeBasedDrops"]:
        drop = parse_drop(dd, claimed_benefits)
        drop.campaign = c
        c.drops[drop.id] = drop
    return c


# ------------------------------------------------------------------------- client


class Twitch:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.token: str | None = None
        self.user_id: int | None = None
        self.login: str | None = None
        self.client = ANDROID
        self.ops = {k: list(v) for k, v in OPS.items()}
        self.device_id = "".join(random.choices("0123456789abcdef", k=32))
        self.session_id = "".join(random.choices("0123456789abcdef", k=16))
        self._sem = asyncio.Semaphore(4)
        self._urls: dict[str, tuple[str, float]] = {}

    # -- lifecycle / auth

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=10),
            headers={"User-Agent": self.client["ua"]},
        )
        try:  # Twitch hands out a device id via cookie, the app keeps it for the session
            async with self.session.get(self.client["url"], headers=self.headers()) as r:
                await r.read()
            cookie = self.session.cookie_jar.filter_cookies(URL(self.client["url"]))
            if "unique_id" in cookie:
                self.device_id = cookie["unique_id"].value
        except Exception as exc:  # noqa: BLE001 - not critical
            log.warning("device id fetch failed: %s", exc)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @property
    def client_name(self) -> str:
        return "Android" if self.client is ANDROID else "Web"

    def headers(self, *, gql: bool = False) -> dict:
        h = {
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": self.client["client_id"],
            "User-Agent": self.client["ua"],
            "Client-Session-Id": self.session_id,
            "X-Device-Id": self.device_id,
        }
        if gql:
            h["Origin"] = self.client["url"]
            h["Referer"] = self.client["url"]
            h["Authorization"] = f"OAuth {self.token}"
        return h

    async def set_token(self, token: str) -> dict:
        """Validate a token and pick the matching client identity."""
        assert self.session
        token = token.strip().removeprefix("OAuth ").strip()
        async with self.session.get(
            "https://id.twitch.tv/oauth2/validate", headers={"Authorization": f"OAuth {token}"}
        ) as r:
            if r.status == 401:
                raise AuthError("Токен недействителен или отозван")
            if r.status != 200:
                raise TwitchError(f"validate: HTTP {r.status}")
            data = await r.json()
        cid = data.get("client_id")
        if cid not in CLIENTS:
            raise AuthError(f"Неизвестный client_id токена: {cid}")
        self.client = CLIENTS[cid]
        self.session.headers["User-Agent"] = self.client["ua"]
        self.token = token
        self.user_id = int(data["user_id"])
        self.login = data.get("login")
        self._urls.clear()
        return data

    async def device_start(self) -> dict:
        """Start the device-code login (Android app client). Returns Twitch's JSON."""
        assert self.session
        h = {
            "Accept": "application/json",
            "Client-Id": ANDROID["client_id"],
            "Origin": ANDROID["url"],
            "Referer": ANDROID["url"],
            "User-Agent": ANDROID["ua"],
            "X-Device-Id": self.device_id,
        }
        async with self.session.post(
            "https://id.twitch.tv/oauth2/device", headers=h,
            data={"client_id": ANDROID["client_id"], "scopes": ""},
        ) as r:
            if r.status != 200:
                raise TwitchError(f"Вход по коду недоступен: HTTP {r.status} {(await r.text())[:150]}")
            return await r.json()

    async def device_poll(self, info: dict) -> str:
        assert self.session
        h = {
            "Accept": "application/json",
            "Client-Id": ANDROID["client_id"],
            "Origin": ANDROID["url"],
            "Referer": ANDROID["url"],
            "User-Agent": ANDROID["ua"],
            "X-Device-Id": self.device_id,
        }
        deadline = time.monotonic() + info.get("expires_in", 1800)
        while time.monotonic() < deadline:
            await asyncio.sleep(info.get("interval", 5))
            async with self.session.post(
                "https://id.twitch.tv/oauth2/token", headers=h,
                data={
                    "client_id": ANDROID["client_id"],
                    "device_code": info["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            ) as r:
                if r.status == 200:
                    return (await r.json())["access_token"]
        raise AuthError("Код истёк, запроси новый")

    # -- GQL

    def op(self, key: str, **variables) -> dict:
        name, sha = self.ops[key]
        d: dict = {"operationName": name, "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}}}
        if variables:
            d["variables"] = variables
        return d

    async def gql(self, payload):
        assert self.session
        for attempt in range(3):
            async with self._sem:
                async with self.session.post(
                    "https://gql.twitch.tv/gql", json=payload, headers=self.headers(gql=True)
                ) as r:
                    if r.status == 401:
                        raise AuthError("GQL вернул 401: токен больше не действует")
                    if r.status >= 500:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    data = await r.json(content_type=None)
            retry = False
            for item in (data if isinstance(data, list) else [data]):
                for err in item.get("errors") or []:
                    msg = str(err.get("message", ""))
                    low = msg.lower()
                    if msg == "PersistedQueryNotFound":
                        raise QueryOutdated(item.get("extensions", {}).get("operationName", "?"))
                    if msg == "service error" and attempt < 2:
                        retry = True
                    elif "integrity" in low:
                        op = item.get("extensions", {}).get("operationName", "?")
                        raise TwitchError(f"Twitch: проверка integrity не пройдена на запросе {op} (клиент токена: {self.client_name})")
                    elif low in ("unauthenticated", "invalid oauth token"):
                        raise AuthError(msg)
            if retry:
                await asyncio.sleep(5)
                continue
            return data
        raise TwitchError("GQL: сервер не отвечает")

    async def refresh_hashes(self) -> int:
        """Pull fresh query hashes from the miner's repo; returns how many changed."""
        assert self.session
        async with self.session.get(HASH_SOURCE) as r:
            if r.status != 200:
                raise TwitchError(f"hash source: HTTP {r.status}")
            text = await r.text()
        found = dict(re.findall(r'GQLPersistedQuery\(\s*"(\w+)",\s*"([0-9a-f]{64})"', text))
        changed = 0
        for entry in self.ops.values():
            new = found.get(entry[0])
            if new and new != entry[1]:
                entry[1] = new
                changed += 1
        return changed

    # -- campaigns

    async def fetch_campaigns(self) -> list[Campaign]:
        assert self.user_id
        inv = (await self.gql(self.op("Inventory", fetchRewardCampaigns=False)))["data"]["currentUser"]["inventory"]
        in_progress = {c["id"]: c for c in (inv.get("dropCampaignsInProgress") or [])}
        claimed_benefits = {
            b["id"]: ts(b["lastAwardedAt"]) for b in (inv.get("gameEventDrops") or [])
        }
        dash = (await self.gql(self.op("Campaigns", fetchRewardCampaigns=False)))["data"]["currentUser"]
        ids = [
            c["id"] for c in (dash.get("dropCampaigns") or [])
            if c["status"] in ("ACTIVE", "UPCOMING")
        ]
        details: dict[str, dict] = {}
        for part in chunks(ids, 20):
            res = await self.gql([
                self.op("CampaignDetails", channelLogin=str(self.user_id), dropID=cid) for cid in part
            ])
            for item in res:
                camp = ((item.get("data") or {}).get("user") or {}).get("dropCampaign")
                if camp and camp.get("game"):
                    details[camp["id"]] = camp
        # overlay per-drop progress from the inventory (it carries the "self" edge)
        for cid, camp in details.items():
            inv_c = in_progress.get(cid)
            if not inv_c:
                continue
            selfmap = {d["id"]: d.get("self") for d in inv_c.get("timeBasedDrops") or []}
            for d in camp["timeBasedDrops"]:
                if selfmap.get(d["id"]):
                    d["self"] = selfmap[d["id"]]
        out = []
        for camp in details.values():
            try:
                out.append(parse_campaign(camp, claimed_benefits))
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("skip campaign %s: %r", camp.get("id"), exc)
        return out

    async def claim(self, drop: Drop) -> bool:
        claim_id = drop.claim_id or f"{self.user_id}#{drop.campaign.id}#{drop.id}"
        try:
            res = await self.gql(self.op("ClaimDrop", input={"dropInstanceID": claim_id}))
        except (QueryOutdated, AuthError):
            raise
        except TwitchError as exc:
            log.warning("claim failed: %s", exc)
            return False
        data = res.get("data") or {}
        payload = data.get("claimDropRewards")
        return bool(payload) and payload.get("status") in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED")

    # -- channels

    async def streams_info(self, logins: list[str]) -> list[Channel]:
        out: list[Channel] = []
        for part in chunks(logins, 20):
            res = await self.gql([self.op("GetStreamInfo", channel=l) for l in part])
            for login, item in zip(part, res):
                user = (item.get("data") or {}).get("user")
                if not user or not user.get("stream"):
                    continue
                game = (user.get("broadcastSettings") or {}).get("game") or {}
                out.append(Channel(
                    id=int(user["id"]), login=user.get("login") or login,
                    broadcast_id=int(user["stream"]["id"]),
                    game_id=int(game["id"]) if game.get("id") else None,
                    game_name=game.get("displayName") or game.get("name") or "",
                    display=user.get("displayName") or "",
                ))
        return out

    async def directory(self, slug: str, limit: int = 30) -> list[Channel]:
        res = await self.gql(self.op(
            "GameDirectory", limit=limit, slug=slug, imageWidth=50, includeCostreaming=False,
            options={
                "broadcasterLanguages": [], "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"}, "sort": "RELEVANCE",
                "systemFilters": ["DROPS_ENABLED"], "tags": [], "requestID": "JIRA-VXP-2397",
            },
            sortTypeIsRecency=False,
        ))
        game = (res.get("data") or {}).get("game")
        if not game:
            return []
        out = []
        for edge in game["streams"]["edges"]:
            node, bc = edge["node"], edge["node"].get("broadcaster")
            if not bc:
                continue
            g = node.get("game") or {}
            out.append(Channel(
                id=int(bc["id"]), login=bc["login"], broadcast_id=int(node["id"]),
                game_id=int(g["id"]) if g.get("id") else None,
                game_name=g.get("displayName") or g.get("name") or "",
                display=bc.get("displayName") or "",
            ))
        return out

    async def find_channels(self, camp: Campaign) -> list[Channel]:
        special = camp.game_id in SPECIAL_GAME_IDS
        if camp.channels:
            found = await self.streams_info([login for _, login in camp.channels])
        else:
            found = await self.directory(camp.game_slug)
        return [c for c in found if special or c.game_id == camp.game_id]

    # -- watching

    async def _stream_url(self, ch: Channel) -> str | None:
        assert self.session
        cached = self._urls.get(ch.login)
        if cached and time.monotonic() - cached[1] < 600:
            return cached[0]
        res = await self.gql(self.op(
            "PlaybackAccessToken", isLive=True, isVod=False, login=ch.login,
            platform="web", playerType="site", vodID="",
        ))
        tok = (res.get("data") or {}).get("streamPlaybackAccessToken")
        if not tok:
            return None
        url = URL(
            f"https://usher.ttvnw.net/api/channel/hls/{ch.login}.m3u8"
            f"?sig={tok['signature']}&token={tok['value']}"
        )
        async with self.session.get(url) as r:
            text = await r.text()
        if _looks_like_error(text):
            return None
        lines = [l for l in text.strip().split("\n") if l.startswith("http")]
        if not lines:
            return None
        self._urls[ch.login] = (lines[-1], time.monotonic())  # last = lowest quality
        return lines[-1]

    async def watch_once(self, ch: Channel) -> bool:
        """Pretend to watch: fetch the playlist and HEAD its chunks (advances drops)."""
        assert self.session
        url = await self._stream_url(ch)
        if not url:
            return False
        async with self.session.get(URL(url), headers={"Connection": "close"}) as r:
            if r.status >= 400:
                self._urls.pop(ch.login, None)
                return False
            text = await r.text()
        if _looks_like_error(text):
            self._urls.pop(ch.login, None)
            return False
        for line in text.strip().split("\n"):
            if line.startswith("http"):
                async with self.session.head(URL(line)) as hr:
                    if hr.status != 200:
                        return False
        return True

    async def current_drop(self, ch: Channel) -> dict | None:
        res = await self.gql(self.op("CurrentDrop", channelID=str(ch.id), channelLogin=""))
        return ((res.get("data") or {}).get("currentUser") or {}).get("dropCurrentSession")


def _looks_like_error(text: str) -> bool:
    """Twitch answers errors as JSON (sometimes with broken quoting) instead of a playlist."""
    t = text.strip()
    if not t.startswith(("{", "[")):
        return False
    t = re.sub(r'"url": ?".+}",', "", t)
    try:
        j = json.loads(t)
    except json.JSONDecodeError:
        return False
    if isinstance(j, list) and j:
        j = j[0]
    return isinstance(j, dict) and "error" in j
