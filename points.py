"""Channel points and watch streaks for chosen channels.

Every channel has its own mode:
  points - keep watching while live and claim the bonus chest
  streak - watch just long enough for the current broadcast to count, then free the slot
  both   - keep watching (so the streak counts too) and claim chests

Twitch counts a viewer on two streams at most, so only WATCH_SLOTS channels are
watched at once. Channels that still need minutes for their streak go first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from twitch_client import AuthError, Channel, QueryOutdated, Twitch, TwitchError

log = logging.getLogger("points")

PING_EVERY = 20       # seconds between "watch" rounds, same cadence as the reference miner
CHECK_EVERY = 60      # seconds between live checks and bonus claims
WATCH_SLOTS = 2       # Twitch only credits two simultaneous streams
MAX_CHANNELS = 8
STREAK_MIN_SEC = 600  # watch a broadcast this long for it to count (Twitch doesn't publish its own limit)
RATE_WINDOW = 1800    # build the points/hour estimate over at most 30 minutes
RATE_MIN = 600        # ...and only show it after 10 minutes of history
MODES = ("points", "streak", "both")
MODE_LABELS = {"points": "Баллы", "streak": "Серия", "both": "Баллы + Серия"}
LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")

# Any Twitch link, with or without a scheme and with any subdomain (m., www., player.).
# Without this, links copied from the phone app ("twitch.tv/name", "https://m.twitch.tv/name")
# were rejected as "not a channel name" — which looked like the bot refusing to add channels.
TWITCH_URL_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)*twitch\.tv/([^/?#\s]+)", re.I)
CHANNEL_PARAM_RE = re.compile(r"[?&]channel=([a-z0-9_]+)", re.I)
# Paths that are never channel logins, so we don't add "https://twitch.tv/videos" as a channel.
RESERVED_LOGINS = {
    "about", "accessibility", "broadcast", "communities", "creatorcamp", "dashboard",
    "directory", "downloads", "drops", "following", "friends", "inventory", "jobs",
    "legal", "login", "p", "privacy", "search", "security", "settings", "signup",
    "store", "subscriptions", "teams", "turbo", "videos", "wallet",
}


def normalize_login(raw: str) -> str | None:
    """Accept a login, @login or any Twitch link (phone/desktop, with or without https://)."""
    s = (raw or "").strip().lower()
    m = CHANNEL_PARAM_RE.search(s)
    if m:                                    # player popout: player.twitch.tv/?channel=name
        s = m.group(1)
    else:
        m = TWITCH_URL_RE.search(s)
        if m:                                # twitch.tv/name, m.twitch.tv/name, .../name/videos
            s = m.group(1)
    s = s.strip().lstrip("@").strip(".,;:!?()[]{}<>\"'«»…")
    if s in RESERVED_LOGINS or not LOGIN_RE.match(s):
        return None
    return s


def _dig(data, *keys):
    for k in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


class PointsFarmer:
    def __init__(self, tw: Twitch, miner, channels: list[str] | None = None, default_mode: str = "both"):
        self.tw = tw
        self.miner = miner
        self.channels: list[str] = []
        for c in channels or []:
            login = normalize_login(c)
            if login and login not in self.channels:
                self.channels.append(login)
        self.channels = self.channels[:MAX_CHANNELS]
        self.enabled = True
        self.default_mode = default_mode if default_mode in MODES else "both"
        self.modes: dict[str, str] = {}               # per channel; missing = default_mode
        self.live: list[Channel] = []
        self.claims: dict[str, int] = {}
        self.balance: dict[str, int] = {}
        self.sent: dict[str, int] = {}
        self.last_error = ""
        self.missing: set[str] = set()
        self.checked_at = 0.0
        self.streak: dict[str, int] = {}              # consecutive broadcasts the bot watched long enough
        self._bc: dict[str, dict] = {}                # current broadcast: {"id", "secs", "counted"}
        self._marks: dict[str, list[tuple[float, int]]] = {}  # balance history for points/hour
        self._last_check = float("-inf")
        self._wake = asyncio.Event()

    # ---- control

    def add(self, raw: str) -> str:
        login = normalize_login(raw)
        if not login:
            return "Не похоже на имя канала. Пример: /points add shroud"
        if login in self.channels:
            return f"{login} уже в списке"
        if len(self.channels) >= MAX_CHANNELS:
            return f"Максимум {MAX_CHANNELS} каналов, сначала убери один: /points remove <канал>"
        self.channels.append(login)
        self._last_check = float("-inf")
        self._wake.set()
        return f"Добавил {login}"

    def remove(self, raw: str) -> str:
        login = normalize_login(raw)
        if not login or login not in self.channels:
            return "Такого канала нет в списке"
        self.channels.remove(login)
        self.live = [c for c in self.live if c.login.lower() != login]
        self.missing.discard(login)
        for table in (self.modes, self.streak, self._bc, self.sent, self.claims, self.balance, self._marks):
            table.pop(login, None)
        return f"Убрал {login}"

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self._wake.set()

    # ---- per-channel modes

    def mode_of(self, login: str) -> str:
        return self.modes.get(login.lower(), self.default_mode)

    def mode_label(self, login: str) -> str:
        return MODE_LABELS[self.mode_of(login)]

    def set_mode(self, raw: str, mode: str) -> str:
        login = normalize_login(raw)
        if mode not in MODES:
            return "Режим: points (баллы), streak (серия) или both (всё вместе)"
        if not login or login not in self.channels:
            return "Такого канала нет в списке"
        self.modes[login] = mode
        self._wake.set()
        return f"{login}: {MODE_LABELS[mode]}"

    def set_streak(self, raw: str, value: str) -> str:
        """Manual sync: Twitch doesn't expose the real streak, so the user copies it from chat."""
        login = normalize_login(raw)
        if not login or login not in self.channels:
            return "Такого канала нет в списке"
        value = value.strip()
        if not value.isdigit() or int(value) > 10000:
            return "Пример: /points streak shroud 12"
        self.streak[login] = int(value)
        return f"{login}: серия = {int(value)}"

    def cycle_mode(self, login: str) -> str | None:
        login = login.lower()
        if login not in self.channels:
            return None
        nxt = MODES[(MODES.index(self.mode_of(login)) + 1) % len(MODES)]
        self.modes[login] = nxt
        self._wake.set()
        return nxt

    def wants_points(self, login: str) -> bool:
        return self.mode_of(login) in ("points", "both")

    def wants_streak(self, login: str) -> bool:
        return self.mode_of(login) in ("streak", "both")

    # ---- persistence (streak counters survive restarts)

    def export_state(self) -> str:
        return json.dumps({"streak": self.streak, "bc": self._bc}, sort_keys=True)

    def load_state(self, raw: str | None) -> None:
        try:
            data = json.loads(raw or "{}")
            self.streak = {str(k): int(v) for k, v in (data.get("streak") or {}).items()}
            self._bc = {
                str(k): {"id": int(v["id"]), "secs": int(v["secs"]), "counted": bool(v["counted"])}
                for k, v in (data.get("bc") or {}).items()
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            self.streak, self._bc = {}, {}

    def modes_json(self) -> str:
        return json.dumps(self.modes, sort_keys=True)

    def load_modes(self, raw: str | None) -> None:
        try:
            data = json.loads(raw or "{}")
            self.modes = {str(k).lower(): v for k, v in data.items() if v in MODES}
        except (ValueError, AttributeError):
            self.modes = {}

    # ---- watch streak bookkeeping (per broadcast, not per time segment)

    def _observe(self, ch: Channel) -> None:
        """Called for every live channel: notices a new broadcast and breaks the streak if one was missed."""
        login = ch.login.lower()
        cur = self._bc.get(login)
        if cur is not None and cur["id"] == ch.broadcast_id:
            return
        if cur is not None and not cur["counted"]:
            self.streak[login] = 0               # a broadcast went by without being watched long enough
        self._bc[login] = {"id": ch.broadcast_id, "secs": 0, "counted": False}

    def _credit(self, ch: Channel) -> None:
        """One successful watch round for this broadcast."""
        self._observe(ch)
        login = ch.login.lower()
        bc = self._bc[login]
        bc["secs"] += PING_EVERY
        if not bc["counted"] and bc["secs"] >= STREAK_MIN_SEC:
            bc["counted"] = True
            self.streak[login] = self.streak.get(login, 0) + 1

    def counted(self, ch: Channel) -> bool:
        bc = self._bc.get(ch.login.lower())
        return bool(bc and bc["id"] == ch.broadcast_id and bc["counted"])

    def broadcast_seconds(self, login: str) -> int:
        bc = self._bc.get(login.lower())
        return bc["secs"] if bc else 0

    def minutes_sent(self, login: str) -> int:
        return self.sent.get(login, 0) * PING_EVERY // 60

    # ---- scheduling

    def pick(self) -> list[Channel]:
        """Which live channels to watch this round (at most WATCH_SLOTS).

        Channels that still need minutes for the streak go first: they free their slot when done.
        """
        needs, steady = [], []
        for ch in self.live:                      # already in the user's order
            login = ch.login.lower()
            if self.wants_streak(login) and not self.counted(ch):
                needs.append(ch)
            elif self.wants_points(login):
                steady.append(ch)
        return (needs + steady)[:WATCH_SLOTS]

    # ---- loop

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def run(self) -> None:
        while True:
            try:
                if not (self.enabled and self.channels and self.tw.token):
                    self.live = []
                    await self._sleep(30)
                    continue
                await self._round()
                await self._sleep(PING_EVERY)
            except asyncio.CancelledError:
                raise
            except AuthError:
                await self._sleep(60)           # the miner already tells the user
            except QueryOutdated:
                try:
                    await self.tw.refresh_hashes()
                except Exception:  # noqa: BLE001
                    pass
                await self._sleep(300)
            except Exception as exc:  # noqa: BLE001
                log.warning("points round failed: %r", exc)
                await self._sleep(60)

    async def _round(self) -> None:
        now = time.monotonic()
        check = now - self._last_check >= CHECK_EVERY
        if check:
            missing: set[str] = set()
            found = await self.tw.streams_info(self.channels, missing)
            order = {login: i for i, login in enumerate(self.channels)}
            self.live = sorted(found, key=lambda c: order.get(c.login.lower(), 99))
            self.missing = missing
            self.checked_at = time.time()
            self._last_check = now
            for ch in self.live:
                self._observe(ch)
        for ch in self.pick():
            try:
                if await self.tw.watch_points(ch):
                    login = ch.login.lower()
                    self.sent[login] = self.sent.get(login, 0) + 1
                    self._credit(ch)
                    self.last_error = ""
                else:
                    self.last_error = f"Twitch не принял просмотр ({ch.login}): {self.tw.watch_note or 'причина неизвестна'}"
            except (AuthError, QueryOutdated):
                raise
            except TwitchError as exc:
                self.last_error = str(exc)
                log.warning("points %s: %s", ch.login, exc)
        if check:
            for ch in self.live:
                if not self.wants_points(ch.login.lower()):
                    continue                      # streak-only channels need no balance or chests
                try:
                    await self._check(ch)
                except (AuthError, QueryOutdated):
                    raise
                except TwitchError as exc:
                    log.warning("points check %s: %s", ch.login, exc)

    async def _check(self, ch: Channel) -> None:
        res = await self.tw.gql(self.tw.op("ChannelPointsContext", channelLogin=ch.login))
        ctx = _dig(res, "data", "community", "channel", "self", "communityPoints")
        if not ctx:
            return
        login = ch.login.lower()
        if ctx.get("balance") is not None:
            bal = int(ctx["balance"])
            self.balance[login] = bal
            self._remember_balance(login, bal)
        claim_id = (ctx.get("availableClaim") or {}).get("id")
        if claim_id:
            r = await self.tw.gql(self.tw.op(
                "ClaimCommunityPoints", input={"channelID": str(ch.id), "claimID": claim_id}
            ))
            if not r.get("errors"):
                self.claims[login] = self.claims.get(login, 0) + 1

    def _remember_balance(self, login: str, bal: int) -> None:
        now = time.monotonic()
        hist = self._marks.setdefault(login, [])
        if not hist or hist[-1][1] != bal:          # Twitch credits in batches: keep changes only
            hist.append((now, bal))
        while len(hist) > 2 and now - hist[0][0] > RATE_WINDOW:
            hist.pop(0)

    def hourly_rate(self, login: str) -> float | None:
        """Observed points per hour (incl. bonus chests), once there's enough history."""
        hist = self._marks.get(login) or []
        if len(hist) < 2:
            return None
        (t0, b0), (t1, b1) = hist[0], hist[-1]
        dt = t1 - t0
        if dt < RATE_MIN:
            return None
        return (b1 - b0) * 3600 / dt

    # ---- reporting

    def summary(self) -> str:
        if not self.channels:
            return "Баллы: каналы не выбраны (/points add <канал>)"
        total = sum(self.claims.values())
        state = "включены" if self.enabled else "выключены"
        return f"Баллы: {state} · каналов {len(self.channels)} · бонусов собрано {total}"

    def status_text(self) -> str:
        if not self.channels:
            return "Каналы для баллов не выбраны. Добавь: /points add <канал>"
        live = {c.login.lower() for c in self.live}
        lines = [self.summary()]
        for login in self.channels:
            if login in self.missing:
                lines.append(f"• {login}: ❌ такого канала нет на Twitch, проверь имя (нужен логин из ссылки twitch.tv/…)")
                continue
            parts = [f"• {login} [{self.mode_label(login)}]: {'в эфире' if login in live else 'не в эфире'}"]
            if self.wants_points(login):
                bal = self.balance.get(login)
                if bal is not None:
                    parts.append(f"баланс {bal}")
                rate = self.hourly_rate(login)
                if rate is not None:
                    parts.append(f"≈{rate:+.0f} баллов/ч")
                parts.append(f"бонусов {self.claims.get(login, 0)}")
            if self.wants_streak(login):
                parts.append(f"серия {self.streak.get(login, 0)}")
            parts.append(f"минут отправлено {self.minutes_sent(login)}")
            lines.append(", ".join(parts))
        if self.checked_at:
            lines.append(f"Проверено {int(time.time() - self.checked_at)} с назад")
        if self.last_error:
            lines.append(f"⚠️ {self.last_error}")
        return "\n".join(lines)
