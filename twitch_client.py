"""Headless Twitch Drops client (no GUI, no browser).

The request flow is based on TwitchDropsMiner by DevilXD (AGPL-3.0),
so this project is AGPL-3.0 as well.
"""
from __future__ import annotations

import asyncio
import base64
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
SPADE_PATTERN = re.compile(r'"spade_url":"(.*?)"')
SETTINGS_PATTERN = re.compile(
    r"(https://static\.twitchcdn\.net/config/settings.*?js|https://assets\.twitch\.tv/config/settings.*?\.js)"
)
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


def as_dict(value) -> dict:
    """Twitch sometimes sends null where an object is expected — treat it as empty."""
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


# Twitch answers these with "data": null; the request is simply worth repeating.
RETRY_MESSAGES = ("service error", "service unavailable", "service timeout", "context deadline exceeded")


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
    required_subs: int = 0
    campaign: "Campaign | None" = field(default=None, repr=False)

    @property
    def remaining(self) -> int:
        return max(self.required - self.current, 0)

    @property
    def is_badge(self) -> bool:
        return any(b[2] == "BADGE" for b in self.benefits)

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
        """Only real badges count (no emotes, no in-game items)."""
        return any(d.is_badge for d in self.drops.values())

    @property
    def active(self) -> bool:
        return self.status != "EXPIRED" and self.starts <= datetime.now(timezone.utc) < self.ends

    @property
    def eligible(self) -> bool:
        # badge campaigns need no account linking
        return self.badge_like

    def needed_ids(self) -> set[str]:
        """Badge drops plus every drop they depend on (a chain has to be walked in order)."""
        need = {d.id for d in self.drops.values() if d.is_badge}
        stack = list(need)
        while stack:
            drop = self.drops.get(stack.pop())
            if not drop:
                continue
            for pid in drop.preconditions:
                if pid in self.drops and pid not in need:
                    need.add(pid)
                    stack.append(pid)
        return need

    def earnable(self, now: datetime | None = None) -> list[Drop]:
        now = now or datetime.now(timezone.utc)
        chain = {p for d in self.drops.values() if not d.claimed for p in d.preconditions}
        need = self.needed_ids()
        out = []
        for d in self.drops.values():
            if d.id not in need:
                continue
            if d.claimed or d.required <= 0 or d.required_subs > 0 or not (d.starts <= now < d.ends):
                continue
            if not all((self.drops[p].claimed if p in self.drops else True) for p in d.preconditions):
                continue
            if not d.benefits and d.id not in chain:
                continue
            out.append(d)
        return out

    @property
    def watchable(self) -> bool:
        """Has at least one unclaimed drop that can be earned by watching."""
        need = self.needed_ids()
        return any(
            not d.claimed and d.required > 0 and d.required_subs == 0
            for d in self.drops.values() if d.id in need
        )

    def claimable(self) -> list[Drop]:
        """Drops whose minutes are done but which haven't been claimed yet."""
        now = datetime.now(timezone.utc)
        need = self.needed_ids()
        return [
            d for d in self.drops.values()
            if d.id in need and not d.claimed and d.required > 0 and d.current >= d.required
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
        for b in as_list(d.get("benefitEdges"))
        if isinstance(b, dict) and isinstance(b.get("benefit"), dict)
    ]
    starts, ends = ts(d["startAt"]), ts(d["endAt"])
    claim_id, claimed, current = None, False, 0
    selfd = as_dict(d.get("self"))
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
        preconditions=[p["id"] for p in as_list(d.get("preconditionDrops"))
                       if isinstance(p, dict) and p.get("id")],
        required_subs=int(d.get("requiredSubs") or 0),
    )


def parse_campaign(d: dict, claimed_benefits: dict[str, datetime]) -> Campaign:
    game = d["game"]
    name = game.get("displayName") or game["name"]
    allow = as_dict(d.get("allow"))
    channels = (
        [(int(c["id"]), c["name"]) for c in as_list(allow.get("channels"))
         if isinstance(c, dict) and c.get("id")]
        if allow.get("isEnabled", True) else []
    )
    c = Campaign(
        id=d["id"], name=d["name"], status=d["status"], game_id=int(game["id"]),
        game_name=name, game_slug=game.get("slug") or slugify(name),
        starts=ts(d["startAt"]), ends=ts(d["endAt"]),
        linked=bool(as_dict(d.get("self")).get("isAccountConnected")), channels=channels,
    )
    for dd in as_list(d.get("timeBasedDrops")):
        if not isinstance(dd, dict):
            continue
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
        self._spade_url: str | None = None
        self.watch_note = ""          # why the last watch attempt failed (for diagnostics)
        self._retry_wait = 5          # seconds between GQL retries (tests set it to 0)

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
                        await asyncio.sleep(self._retry_wait * (attempt + 1))
                        continue
                    data = await r.json(content_type=None)
            # Twitch can answer with a bare "null" or null "data" plus an error message.
            # It used to reach the callers as None and blow up with
            # AttributeError: 'NoneType' object has no attribute 'get'.
            if not isinstance(data, (dict, list)):
                if attempt < 2:
                    await asyncio.sleep(self._retry_wait * (attempt + 1))
                    continue
                raise TwitchError("GQL: пустой ответ Twitch, попробую ещё раз")
            retry = False
            reason = ""
            usable = False
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    retry, reason = True, "пустой элемент ответа"
                    continue
                for err in item.get("errors") or []:
                    msg = str(err.get("message", "") if isinstance(err, dict) else err)
                    low = msg.lower()
                    if msg == "PersistedQueryNotFound":
                        raise QueryOutdated(as_dict(item.get("extensions")).get("operationName", "?"))
                    if low in RETRY_MESSAGES:
                        retry, reason = True, msg
                    elif "integrity" in low:
                        op = as_dict(item.get("extensions")).get("operationName", "?")
                        raise TwitchError(f"Twitch: проверка integrity не пройдена на запросе {op} (клиент токена: {self.client_name})")
                    elif low in ("unauthenticated", "invalid oauth token"):
                        raise AuthError(msg)
                if item.get("data") is None:
                    retry, reason = True, reason or "data: null"
                else:
                    usable = True
            if retry and attempt < 2:
                await asyncio.sleep(self._retry_wait * (attempt + 1))
                continue
            if retry and not usable:
                raise TwitchError(f"Twitch не отдал данные ({reason}), попробую позже")
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
        inv_res = as_dict(await self.gql(self.op("Inventory", fetchRewardCampaigns=False)))
        inv = as_dict(as_dict(inv_res.get("data")).get("currentUser")).get("inventory")
        inv = as_dict(inv)
        in_progress = {
            c["id"]: c for c in as_list(inv.get("dropCampaignsInProgress"))
            if isinstance(c, dict) and c.get("id")
        }
        claimed_benefits = {
            b["id"]: ts(b["lastAwardedAt"]) for b in as_list(inv.get("gameEventDrops"))
            if isinstance(b, dict) and b.get("id") and b.get("lastAwardedAt")
        }
        dash_res = as_dict(await self.gql(self.op("Campaigns", fetchRewardCampaigns=False)))
        dash = as_dict(as_dict(dash_res.get("data")).get("currentUser"))
        ids = [
            c["id"] for c in as_list(dash.get("dropCampaigns"))
            if isinstance(c, dict) and c.get("id") and c.get("status") in ("ACTIVE", "UPCOMING")
        ]
        for cid in in_progress:                     # campaigns the dashboard doesn't list but Twitch is tracking
            if cid not in ids:
                ids.append(cid)
        details: dict[str, dict] = {}
        self.skipped = []
        self.dash_total = len(as_list(dash.get("dropCampaigns")))
        names = {c["id"]: c.get("name") or c["id"] for c in as_list(dash.get("dropCampaigns"))
                 if isinstance(c, dict) and c.get("id")}
        for c in as_list(dash.get("dropCampaigns")):
            if isinstance(c, dict) and c.get("id") and c.get("status") not in ("ACTIVE", "UPCOMING", "EXPIRED"):
                self.skipped.append(f"{names[c['id']]}: статус {c.get('status')}")
        self.dash_active = len(ids)
        for part in chunks(ids, 20):
            res = await self.gql([
                self.op("CampaignDetails", channelLogin=str(self.user_id), dropID=cid) for cid in part
            ])
            for item in (as_list(res) if isinstance(res, list) else [res]):
                if not isinstance(item, dict):
                    continue
                camp = as_dict(as_dict(item.get("data")).get("user")).get("dropCampaign")
                if isinstance(camp, dict) and camp.get("id") and isinstance(camp.get("game"), dict):
                    details[camp["id"]] = camp
                elif isinstance(camp, dict) and camp.get("id"):
                    self.skipped.append(f"{camp.get('name') or camp['id']}: нет данных об игре")
                else:
                    self.skipped.append("кампания: Twitch не отдал детали")
        # overlay per-drop progress from the inventory (it carries the "self" edge)
        for cid, camp in details.items():
            inv_c = in_progress.get(cid)
            if not inv_c:
                continue
            selfmap = {
                d["id"]: d.get("self") for d in as_list(inv_c.get("timeBasedDrops"))
                if isinstance(d, dict) and d.get("id")
            }
            for d in as_list(camp.get("timeBasedDrops")):
                if isinstance(d, dict) and d.get("id") in selfmap and selfmap[d["id"]]:
                    d["self"] = selfmap[d["id"]]
        out = []
        for camp in details.values():
            try:
                out.append(parse_campaign(camp, claimed_benefits))
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                log.warning("skip campaign %s: %r", camp.get("id"), exc)
                self.skipped.append(f"{camp.get('name') or camp.get('id')}: ошибка разбора {exc!r}")
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
        data = as_dict(as_dict(res).get("data"))
        payload = data.get("claimDropRewards")
        return isinstance(payload, dict) and payload.get("status") in (
            "ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED",
        )

    # -- channels

    async def streams_info(self, logins: list[str], missing: set[str] | None = None) -> list[Channel]:
        """Live channels among `logins`; logins Twitch doesn't know are added to `missing`."""
        out: list[Channel] = []
        for part in chunks(logins, 20):
            res = await self.gql([self.op("GetStreamInfo", channel=l) for l in part])
            for login, item in zip(part, (res if isinstance(res, list) else [])):
                user = as_dict(as_dict(as_dict(item).get("data")).get("user"))
                if not user:
                    if missing is not None:
                        missing.add(login.lower())
                    continue
                stream = as_dict(user.get("stream"))
                if not stream or not stream.get("id"):
                    continue
                game = as_dict(as_dict(user.get("broadcastSettings")).get("game"))
                out.append(Channel(
                    id=int(user["id"]), login=user.get("login") or login,
                    broadcast_id=int(stream["id"]),
                    game_id=int(game["id"]) if game.get("id") else None,
                    game_name=game.get("displayName") or game.get("name") or "",
                    display=user.get("displayName") or "",
                ))
        return out

    async def directory(self, slug: str, limit: int = 30, drops_only: bool = True) -> list[Channel]:
        res = await self.gql(self.op(
            "GameDirectory", limit=limit, slug=slug, imageWidth=50, includeCostreaming=False,
            options={
                "broadcasterLanguages": [], "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"}, "sort": "RELEVANCE",
                "systemFilters": ["DROPS_ENABLED"] if drops_only else [], "tags": [], "requestID": "JIRA-VXP-2397",
            },
            sortTypeIsRecency=False,
        ))
        game = as_dict(as_dict(res).get("data")).get("game")
        if not isinstance(game, dict):
            return []
        out = []
        for edge in as_list(as_dict(game.get("streams")).get("edges")):
            node = as_dict(as_dict(edge).get("node"))
            bc = as_dict(node.get("broadcaster"))
            if not node.get("id") or not bc.get("id") or not bc.get("login"):
                continue
            g = as_dict(node.get("game"))
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

    async def _stream_url(self, ch: Channel, ua: str | None = None) -> str | None:
        assert self.session
        key = f"{ch.login}|{ua}"
        cached = self._urls.get(key)
        if cached:
            # failed lookups are cached briefly too, so a blocked playlist isn't hammered
            ttl = 600 if cached[0] else 120
            if time.monotonic() - cached[1] < ttl:
                return cached[0] or None
        res = await self.gql(self.op(
            "PlaybackAccessToken", isLive=True, isVod=False, login=ch.login,
            platform="web", playerType="site", vodID="",
        ))
        tok = as_dict(as_dict(as_dict(res).get("data")).get("streamPlaybackAccessToken"))
        if not tok.get("signature") or not tok.get("value"):
            self.watch_note = "Twitch не выдал токен доступа к стриму (PlaybackAccessToken)"
            self._urls[key] = ("", time.monotonic())
            return None
        url = URL(
            f"https://usher.ttvnw.net/api/channel/hls/{ch.login}.m3u8"
            f"?sig={tok['signature']}&token={tok['value']}"
        )
        headers = {"User-Agent": ua} if ua else {}
        headers.setdefault("Referer", "https://player.twitch.tv/")
        headers.setdefault("Origin", "https://player.twitch.tv")
        async with self.session.get(url, headers=headers) as r:
            text = await r.text()
            status = r.status
        if status >= 400 or _looks_like_error(text):
            reason = _error_note(text) if _looks_like_error(text) else f"HTTP {status}"
            self.watch_note = f"плейлист: {reason}"
            self._urls[key] = ("", time.monotonic())
            return None
        lines = [l for l in text.strip().split("\n") if l.startswith("http")]
        if not lines:
            self.watch_note = "плейлист пустой"
            self._urls[key] = ("", time.monotonic())
            return None
        self._urls[key] = (lines[-1], time.monotonic())  # last = lowest quality
        return lines[-1]

    async def watch_once(self, ch: Channel, ua: str | None = None, last_only: bool = False) -> bool:
        """Pretend to watch: fetch the playlist and HEAD its chunks (advances drops)."""
        assert self.session
        headers = {"User-Agent": ua} if ua else {}
        url = await self._stream_url(ch, ua)
        if not url:
            return False
        key = f"{ch.login}|{ua}"
        async with self.session.get(URL(url), headers={**headers, "Connection": "close"}) as r:
            if r.status >= 400:
                self._urls.pop(key, None)
                self.watch_note = f"плейлист: HTTP {r.status}"
                return False
            text = await r.text()
        if _looks_like_error(text):
            self._urls.pop(key, None)
            self.watch_note = f"плейлист: {_error_note(text)}"
            return False
        chunk_urls = [l for l in text.strip().split("\n") if l.startswith("http")]
        if last_only:
            chunk_urls = chunk_urls[-1:]
        for line in chunk_urls:
            async with self.session.head(URL(line), headers=headers) as hr:
                if hr.status != 200:
                    self.watch_note = f"видео-кусочек: HTTP {hr.status}"
                    return False
        return True

    async def _fetch_spade_url(self, login: str) -> str:
        """streamer page (HTML) -> settings JS -> spade URL (same chain as the reference miners)."""
        assert self.session
        headers = {"User-Agent": WEB["ua"]}
        async with self.session.get(f"https://www.twitch.tv/{login}", headers=headers) as r:
            html = await r.text(encoding="utf8")
        m2 = SETTINGS_PATTERN.search(html)
        if not m2:
            raise TwitchError("ссылка на настройки Twitch не найдена на странице канала")
        async with self.session.get(m2.group(1), headers=headers) as r:
            js = await r.text(encoding="utf8")
        m = SPADE_PATTERN.search(js)
        if not m:
            raise TwitchError("spade_url не найден в настройках Twitch")
        return m.group(1)

    async def watch_spade(self, ch: Channel) -> bool:
        """POST a "minute-watched" event the way channel-points miners do."""
        assert self.session
        if not self._spade_url:
            self._spade_url = await self._fetch_spade_url(ch.login)
        payload = [{
            "event": "minute-watched",
            "properties": {
                "channel_id": str(ch.id),
                "broadcast_id": str(ch.broadcast_id),
                "player": "site",
                "user_id": self.user_id,
                "live": True,
                "channel": ch.login,
            },
        }]
        data = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf8")).decode("utf8")
        async with self.session.post(
            self._spade_url, data={"data": data}, headers={"User-Agent": WEB["ua"]}
        ) as r:
            ok, status = r.status == 204, r.status
        if not ok:
            self._spade_url = None               # re-fetch next time
            self.watch_note = f"событие просмотра: HTTP {status}"
        return ok

    async def watch_points(self, ch: Channel) -> bool:
        """What Twitch needs to credit channel points: send the "minute-watched" event.

        The playlist fetch only emulates a player (it helps drops) and is kept
        best-effort: Twitch blocks usher for datacenter/VPN IPs with
        ``anonymizer_blocked``, and channel points don't need the playlist anyway.
        """
        self.watch_note = ""
        await self.watch_once(ch, last_only=True)
        playlist_note = self.watch_note
        self.watch_note = ""
        if await self.watch_spade(ch):
            return True
        self.watch_note = self.watch_note or playlist_note
        return False

    async def current_drop(self, ch: Channel) -> dict | None:
        res = await self.gql(self.op("CurrentDrop", channelID=str(ch.id), channelLogin=""))
        cur = as_dict(as_dict(as_dict(res).get("data")).get("currentUser")).get("dropCurrentSession")
        return cur if isinstance(cur, dict) else None


def _parse_error(text: str) -> dict | None:
    """Twitch answers errors as JSON (sometimes with broken quoting) instead of a playlist."""
    t = text.strip()
    if not t.startswith(("{", "[")):
        return None
    t = re.sub(r'"url": ?".+}",', "", t)
    try:
        j = json.loads(t)
    except json.JSONDecodeError:
        return None
    if isinstance(j, list) and j:
        j = j[0]
    return j if isinstance(j, dict) and "error" in j else None


def _looks_like_error(text: str) -> bool:
    return _parse_error(text) is not None


def _error_note(text: str) -> str:
    """Human-readable error from Twitch's JSON: "Content Restricted In Region (anonymizer_blocked)"."""
    j = _parse_error(text) or {}
    err = str(j.get("error") or "Twitch вернул ошибку")
    code = j.get("error_code")
    return f"{err} ({code})" if code else err
