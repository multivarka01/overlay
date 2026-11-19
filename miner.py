"""Mining loop: pick a campaign, find a stream, "watch" it, claim finished drops."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from twitch_client import (
    AuthError, Campaign, Channel, Drop, QueryOutdated, SPECIAL_GAME_IDS, Twitch, TwitchError,
)

log = logging.getLogger("miner")

Notify = Callable[[str], Awaitable[None]]
WATCH_INTERVAL = 20      # seconds between "watch" pings
PROGRESS_EVERY = 60      # seconds between progress checks
REFRESH_EVERY = 300      # full inventory refresh while mining
IDLE_REFRESH = 180       # refresh interval when there is nothing to mine
IDLE_MIN = 30            # never poll more often than this
STALL_AFTER = 900        # no progress for this long -> switch channel
GAME_DAILY_SEC = 70 * 60  # default watch time per stage (badges want ~60 min/day)
MAX_GAMES = 3
# promotions with a fixed chain of stages: one stage (badge) per completed day
BADGE_CHAINS = {
    "grand-theft-auto-vi": ["Purple Chain", "Bronze Chain", "Silver Chain", "Gold Chain", "Diamond Chain", "Miami Chain"],
}
BADGE_DAYS = {k: len(v) for k, v in BADGE_CHAINS.items()}
# Promotions whose stages open at a fixed clock time instead of "24h after I finished".
# GTAVI Launch Celebration: 18.11.2026 11:00 UTC (= 16:00 Yekaterinburg) until
# 01.12.2026 08:00 UTC. One hour of watching secures one chain badge, and a new
# stage window opens every day at 16:00 Yekaterinburg (11:00 UTC) — the same
# clock time the promo started at. Progress inside a window does not carry over.
BADGE_SCHEDULES = {
    "grand-theft-auto-vi": {
        "start": datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc),
        "reset": (11, 0),          # UTC time of day when the next stage opens
        "daily_sec": 60 * 60,      # exactly one hour per chain badge
        "final": "Golden Iguana",
        "final_note": "для Golden Iguana нужны ещё 3 подписки на каналы GTA VI (бот их не оформляет)",
    },
}
PIN_PING_EVERY = 60      # seconds between credited pings while watching a pinned/category channel
MAX_MANUAL = 5           # channels the user can pin for watching


def _now() -> datetime:
    """Current time; isolated so tests can freeze the clock."""
    return datetime.now(timezone.utc)


def stage_seconds(slug: str) -> int:
    """How many seconds of watching one stage (badge) needs."""
    sched = BADGE_SCHEDULES.get(slug)
    return int(sched["daily_sec"]) if sched else GAME_DAILY_SEC


def stage_window(slug: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Current stage window (start, end), or None when the promo is not running.

    Scheduled promos use fixed 24h windows anchored at `reset` (11:00 UTC for
    GTA VI = 16:00 Yekaterinburg). Without a schedule we fall back to a UTC day.
    """
    now = now or _now()
    sched = BADGE_SCHEDULES.get(slug)
    if not sched:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if now < sched["start"] or now >= sched["end"]:
        return None
    hour, minute = sched["reset"]
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    start = max(start, sched["start"])
    end = min(start + timedelta(days=1), sched["end"])
    return start, end


def promo_state(slug: str, now: datetime | None = None) -> str:
    """'before' | 'running' | 'over' for a scheduled promo, 'unknown' otherwise."""
    sched = BADGE_SCHEDULES.get(slug)
    if not sched:
        return "unknown"
    now = now or _now()
    if now < sched["start"]:
        return "before"
    if now >= sched["end"]:
        return "over"
    return "running"


class Miner:
    def __init__(self, tw: Twitch, notify: Notify):
        self.tw = tw
        self.notify = notify
        self.paused = False
        self.state = "запускаюсь"
        self.target: tuple[Campaign, Drop] | None = None
        self.channel: Channel | None = None
        self.campaigns: list[Campaign] = []
        self.claimed: deque[str] = deque(maxlen=10)
        self.next_start: datetime | None = None
        self.next_up: tuple[Campaign, Drop] | None = None
        self.watching = False
        self.stats = {"badges": 0, "watch_sec": 0}
        self._wake = asyncio.Event()
        self._known: set[str] | None = None
        self._announced: set[str] = set()
        self._claim_fails: dict[str, int] = {}
        self._avoid: dict[str, float] = {}
        self._no_streams: dict[str, float] = {}
        self._gain: dict[str, tuple[int, float]] = {}   # drop id -> (best minutes, when it last grew)
        self._last_error: tuple[str, float] = ("", 0.0)
        self.last_trace = ""                             # crash detail for /status
        self.manual: list[str] = []                      # channels the user wants watched first (local badges)
        self._manual_warned: set[str] = set()
        self.games: list[str] = []                       # category slugs to watch automatically (viewership badges)
        self._game_sec: dict[str, int] = {}              # slug -> seconds watched in the current stage window
        self._game_win: dict[str, float] = {}            # slug -> start (unix) of the window the seconds belong to
        self.game_channel: dict[str, str] = {}           # slug -> channel being watched right now
        self.days_done: dict[str, int] = {}              # slug -> completed days (norm reached)
        self.done_at: dict[str, float] = {}              # slug -> unix time the last stage was completed
        self.on_change: Callable[[], Awaitable[None]] | None = None   # persist hook (set by the app)
        self.on_new_badge: Callable[[str, str], Awaitable[None]] | None = None   # (badge, game) -> message

    # ---- control

    def add_manual(self, login: str) -> str:
        if login in self.manual:
            return f"{login} уже в списке"
        if len(self.manual) >= MAX_MANUAL:
            return f"Максимум {MAX_MANUAL} каналов, сначала убери один: /watch remove <канал>"
        self.manual.append(login)
        self._manual_warned.discard(login)
        self._wake.set()
        return f"Добавил {login}. Буду смотреть его в первую очередь, пока он в эфире и под него есть кампания."

    def remove_manual(self, login: str) -> str:
        if login not in self.manual:
            return "Такого канала нет в списке"
        self.manual.remove(login)
        self._avoid.pop(login, None)
        self._wake.set()
        return f"Убрал {login}"

    def clear_manual(self) -> str:
        self.manual.clear()
        self._wake.set()
        return "Список очищен, снова выбираю каналы сам"

    def manual_text(self) -> str:
        if not self.manual:
            return "Ручных каналов нет. Добавить: /watch add <канал>"
        cur = self.channel.login.lower() if self.channel else ""
        return "Смотрю в первую очередь:\n" + "\n".join(
            f"• {c}" + (" ← сейчас" if c == cur and self.watching else "") for c in self.manual
        )

    @staticmethod
    def game_slug(raw: str) -> str:
        """Accepts a slug, a twitch.tv/directory/category/... URL or a few aliases."""
        t = raw.strip().lower().rstrip("/")
        if "/category/" in t:
            t = t.split("/category/")[-1].split("?")[0]
        elif "/game/" in t:
            t = t.split("/game/")[-1].split("?")[0]
        t = t.replace(" ", "-")
        if t in ("gta6", "gta-6", "gta-vi", "gtavi", "gta"):
            t = "grand-theft-auto-vi"
        return t if t and all(ch.isalnum() or ch in "-_%" for ch in t) else ""

    def add_game(self, raw: str) -> str:
        slug = self.game_slug(raw)
        if not slug:
            return "Не понял категорию. Пример: /watch game gta6 или ссылка twitch.tv/directory/category/grand-theft-auto-vi"
        if slug in self.games:
            return f"Категория {slug} уже в списке"
        if len(self.games) >= MAX_GAMES:
            return f"Максимум {MAX_GAMES} категории, сначала убери одну: /watch game remove <категория>"
        self.games.append(slug)
        self._wake.set()
        return (f"Добавил категорию {slug}. Сам найду стримера и буду смотреть "
                f"~{stage_seconds(slug) // 60} мин в день.")

    def remove_game(self, raw: str) -> str:
        slug = self.game_slug(raw)
        if slug not in self.games:
            return "Такой категории нет в списке"
        self.games.remove(slug)
        self._wake.set()
        return f"Убрал категорию {slug}"

    def toggle_game(self, slug: str) -> bool:
        """Switch a category on/off; returns the new state (True = on)."""
        if slug in self.games:
            self.games.remove(slug)
            self.game_channel.pop(slug, None)
            on = False
        elif len(self.games) < MAX_GAMES:
            self.games.append(slug)
            on = True
        else:
            return False
        self._wake.set()
        return on

    def day_limit(self, slug: str) -> int | None:
        return BADGE_DAYS.get(slug)

    def daily_norm(self, slug: str) -> int:
        """Seconds of watching needed for the current stage."""
        return stage_seconds(slug)

    def promo_state(self, slug: str) -> str:
        return promo_state(slug)

    def stage_window(self, slug: str) -> tuple[datetime, datetime] | None:
        return stage_window(slug)

    def game_finished(self, slug: str) -> bool:
        lim = self.day_limit(slug)
        return lim is not None and self.days_done.get(slug, 0) >= lim

    def _sync_window(self, slug: str) -> None:
        """Drop progress when a new day/stage window has begun."""
        win = stage_window(slug)
        if win is None:
            return
        key = win[0].timestamp()
        prev = self._game_win.get(slug)
        if prev is not None and prev != key:
            self._game_sec[slug] = 0
        self._game_win[slug] = key

    def game_day_counted(self, slug: str) -> bool:
        """True if the stage for the *current* window was already completed."""
        win = stage_window(slug)
        at = self.done_at.get(slug)
        if win is None or at is None:
            return False
        return win[0].timestamp() <= at < win[1].timestamp()

    def cooldown_left(self, slug: str) -> float:
        """Seconds until the next stage window opens (0 if it is already open)."""
        if not self.game_day_counted(slug):
            return 0.0
        win = stage_window(slug)
        if win is None:
            return 0.0
        return max(0.0, (win[1] - _now()).total_seconds())

    def next_available(self, slug: str) -> datetime | None:
        """When the next stage opens (None if nothing is pending or the promo ends)."""
        if not self.game_day_counted(slug):
            return None
        win = stage_window(slug)
        if win is None:
            return None
        sched = BADGE_SCHEDULES.get(slug)
        if sched and win[1] >= sched["end"]:
            return None
        return win[1]

    def chain(self, slug: str) -> list[str]:
        return BADGE_CHAINS.get(slug, [])

    async def mark_day(self, slug: str, manual: bool = False) -> None:
        """Complete the current stage (norm reached, or the user got the badge themselves)."""
        if self.game_day_counted(slug) or self.game_finished(slug):
            return
        self._sync_window(slug)
        self.days_done[slug] = self.days_done.get(slug, 0) + 1
        self.done_at[slug] = _now().timestamp()
        self._game_sec[slug] = 0
        lim = self.day_limit(slug)
        n = self.days_done[slug]
        log.info("%s: stage %s/%s done", slug, n, lim)
        if self.on_change:
            await self.on_change()

    async def set_progress(self, slug: str, days: int, minutes: int = 0) -> str:
        """Manual sync with reality: `days` stages already earned, `minutes` watched in the current window."""
        lim = self.day_limit(slug)
        if lim is not None and not 0 <= days <= lim:
            return f"Дней должно быть от 0 до {lim}"
        if not 0 <= minutes <= self.daily_norm(slug) // 60:
            return f"Минут должно быть от 0 до {self.daily_norm(slug) // 60}"
        self.days_done[slug] = days
        self.done_at.pop(slug, None)          # current window is not completed
        self._sync_window(slug)
        win = stage_window(slug)
        if win:
            self._game_win[slug] = win[0].timestamp()
        self._game_sec[slug] = minutes * 60
        if self.on_change:
            await self.on_change()
        self._wake.set()
        return f"{slug}: значков {days}, минут в текущем окне {minutes}"

    def days_json(self) -> str:
        return json.dumps({"done": self.days_done, "at": self.done_at, "sec": self._game_sec,
                           "win": self._game_win})

    def load_days(self, raw: str | None) -> None:
        try:
            d = json.loads(raw or "{}")
            self.days_done = {str(k): int(v) for k, v in (d.get("done") or {}).items()}
            self.done_at = {str(k): float(v) for k, v in (d.get("at") or {}).items()}
            self._game_sec = {str(k): int(v) for k, v in (d.get("sec") or {}).items()}
            self._game_win = {str(k): float(v) for k, v in (d.get("win") or {}).items()}
            for k, v in (d.get("marked") or {}).items():          # older format: UTC date of the last day
                if k not in self.done_at:
                    self.done_at[k] = datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError, AttributeError):
            pass

    def game_today(self, slug: str) -> int:
        """Minutes watched in the current stage window."""
        self._sync_window(slug)
        return self._game_sec.get(slug, 0) // 60

    def games_text(self) -> str:
        if not self.games:
            return "Категорий нет. Добавить: /watch game gta6"
        return "Слежу за категориями:\n" + "\n".join(
            f"• {g}: сейчас {self.game_today(g)}/{self.daily_norm(g) // 60} мин" for g in self.games
        )

    def wake(self) -> None:
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _error(self, text: str, *, every: float = 3600) -> None:
        """Notify about an error, but not more often than once per `every` seconds."""
        last_text, last_at = self._last_error
        if text == last_text and time.monotonic() - last_at < every:
            return
        self._last_error = (text, time.monotonic())
        await self.notify(text)

    # ---- selection

    def wanted(self, camp: Campaign) -> bool:
        """Only active campaigns that contain a badge."""
        return camp.active and camp.badge_like

    def rank(self, camps: list[Campaign]) -> list[tuple[Campaign, Drop]]:
        """Candidates ordered by least slack: minutes until the drop expires minus minutes still needed."""
        now = datetime.now(timezone.utc)
        ranked: list[tuple[tuple[float, int], Campaign, Drop]] = []
        for camp in camps:
            if not self.wanted(camp):
                continue
            todo = [d for d in camp.earnable(now) if d.remaining > 0]
            if not todo:
                continue
            drop = min(todo, key=lambda d: d.remaining)
            left = (min(camp.ends, drop.ends) - now).total_seconds() / 60
            ranked.append(((left - drop.remaining, drop.remaining), camp, drop))
        ranked.sort(key=lambda x: x[0])
        return [(c, d) for _, c, d in ranked]

    def pick(self, camps: list[Campaign]) -> tuple[Campaign, Drop] | None:
        ranked = self.rank(camps)
        return ranked[0] if ranked else None

    async def _warn_deadlines(self, camps: list[Campaign]) -> None:
        """Kept as a hook; deadline warnings are only logged, not sent."""
        return None

    def _next_start(self, camps: list[Campaign]) -> datetime | None:
        now = datetime.now(timezone.utc)
        upcoming = [
            d.starts for c in camps if c.eligible and self.wanted_ignoring_time(c)
            for d in c.drops.values() if not d.claimed and d.starts > now
        ]
        return min(upcoming) if upcoming else None

    def wanted_ignoring_time(self, camp: Campaign) -> bool:
        return camp.badge_like

    async def _announce_new(self, camps: list[Campaign]) -> None:
        ids = {c.id for c in camps}
        if self._known is None:        # first cycle after start: remember silently
            self._known = ids
            return
        for camp in camps:
            if camp.id in self._known or camp.id in self._announced:
                continue
            self._announced.add(camp.id)
            if self.wanted_ignoring_time(camp) and camp.eligible and self.on_new_badge:
                for d in camp.drops.values():
                    if d.is_badge:
                        await self.on_new_badge(d.reward_text, camp.game_name)
        self._known |= ids

    # ---- main loop

    async def run(self) -> None:
        while True:
            try:
                if self.paused:
                    self.state = "на паузе"
                    await self._sleep(30)
                elif not self.tw.token:
                    self.state = "нет токена Twitch (/login или /token)"
                    await self._sleep(30)
                else:
                    await self._cycle()
                    self.last_trace = ""
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                self.tw.token = None
                self.state = "токен Twitch не работает"
                await self._error(f"🔑 {exc}. Войди заново: /login или /token <токен>", every=6 * 3600)
                await self._sleep(60)
            except QueryOutdated as exc:
                await self._handle_outdated(str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle failed")
                self.state = f"ошибка: {exc!r}"[:120]
                self.last_trace = f"{exc!r}{self._where(exc)}"
                await self._sleep(60)

    @staticmethod
    def _where(exc: BaseException) -> str:
        """File and line where the exception was actually raised (for the report)."""
        tb = traceback.extract_tb(exc.__traceback__)
        if not tb:
            return ""
        frame = tb[-1]
        return f" [{os.path.basename(frame.filename)}:{frame.lineno}]"

    async def _handle_outdated(self, op_name: str) -> None:
        self.state = "Twitch поменял запросы, обновляю"
        try:
            changed = await self.tw.refresh_hashes()
        except Exception as exc:  # noqa: BLE001
            changed = 0
            log.warning("hash refresh failed: %r", exc)
        if changed:
            log.info("hashes refreshed after %s", op_name)
            return
        log.warning("query %s outdated, no fresh hashes yet", op_name)
        await self._sleep(3600)

    async def _cycle(self) -> None:
        self.state = "обновляю инвентарь"
        camps = await self.tw.fetch_campaigns()
        self.campaigns = camps
        await self._announce_new(camps)
        await self._warn_deadlines(camps)
        await self._claim_ready(camps)

        ranked = self.rank(camps)
        self.next_up = ranked[0] if ranked else None
        self.next_start = self._next_start(camps)
        if not ranked and (self.manual or self.games):
            if await self._try_manual(ranked) or await self._try_games():
                return
        if not ranked:
            self.target = self.channel = None
            self.state = "жду новые дропы"
            wait = IDLE_REFRESH
            if self.next_start:
                until = (self.next_start - datetime.now(timezone.utc)).total_seconds() + 5
                wait = max(IDLE_MIN, min(wait, until))
            await self._sleep(wait)
            return

        now = time.monotonic()
        if await self._try_manual(ranked):
            return
        if await self._try_games():
            return
        for camp, drop in ranked:
            if self._no_streams.get(camp.id, 0) > now:
                continue                      # recently had no streams, try the next campaign
            ch = await self._current_channel_if_valid(camp)
            if ch is None:
                channels = [
                    c for c in await self.tw.find_channels(camp)
                    if self._avoid.get(c.login, 0) < now
                ]
                if not channels:
                    self._no_streams[camp.id] = now + 300
                    continue
                ch = channels[0]
            self.target = (camp, drop)
            self._note_progress(drop)
            await self._watch(camp, drop, ch)
            return

        self.target = self.channel = None
        self.state = f"нет подходящих стримов (кампаний в очереди: {len(ranked)})"
        await self._sleep(120)

    @staticmethod
    def _fits(camp: Campaign, ch: Channel) -> bool:
        """Can this live channel earn progress for the campaign?"""
        if camp.channels:
            return ch.login.lower() in {l.lower() for _, l in camp.channels}
        return camp.game_id in SPECIAL_GAME_IDS or ch.game_id == camp.game_id

    async def _try_manual(self, ranked: list[tuple[Campaign, Drop]]) -> bool:
        """Watch a user-pinned channel if it is live and some wanted campaign accepts it."""
        if not self.manual:
            return False
        now = time.monotonic()
        logins = [c for c in self.manual if self._avoid.get(c, 0) < now]
        if not logins:
            return False
        live = {c.login.lower(): c for c in await self.tw.streams_info(logins)}
        for login in logins:
            ch = live.get(login)
            if ch is None:
                continue                          # offline right now, try the next one
            for camp, drop in ranked:
                if self._fits(camp, ch):
                    self.target = (camp, drop)
                    self._note_progress(drop)
                    await self._watch(camp, drop, ch)
                    return True
            # no drop campaign fits: still watch the pinned channel (viewership badges, reward campaigns)
            await self._watch_pinned(ch)
            return True
        return False

    async def _try_games(self) -> bool:
        """Pick a live channel in a followed category and watch until the stage norm is done."""
        now = time.monotonic()
        for slug in list(self.games):
            state = promo_state(slug)
            sched = BADGE_SCHEDULES.get(slug)
            if state == "before":
                self.state = (f"{slug}: акция начнётся {sched['start']:%d.%m %H:%M} UTC — пока не смотрю")
                continue
            if state == "over":
                self.state = f"{slug}: акция завершена — смотреть больше не нужно"
                continue
            if self.game_finished(slug) or self.game_day_counted(slug):
                continue
            self._sync_window(slug)
            # only channels that take part in the promotion count: those have Drops enabled
            chans = [c for c in await self.tw.directory(slug) if self._avoid.get(c.login, 0) < now]
            if not chans:
                self.state = f"в категории {slug} нет стримов-участников акции (Drops включены), жду"
                continue
            ch = chans[0]
            await self._watch_pinned(ch, slug)
            return True
        return False

    async def _watch_pinned(self, ch: Channel, slug: str | None = None) -> None:
        """Plain viewing of a channel: playlist + minute-watched event once a minute."""
        self.watching = True
        self.channel, self.target = ch, None
        if slug:
            self.game_channel[slug] = ch.name
        started = time.monotonic()
        last_ping = 0.0                       # when the last successful ping was credited
        fails = 0
        try:
            while not self.paused and (slug is not None and slug in self.games or ch.login in self.manual):
                norm = 0
                if slug is not None:
                    state = promo_state(slug)
                    if state == "before":
                        self.state = f"{slug}: акция ещё не началась"
                        return
                    if state == "over":
                        self.state = f"{slug}: акция завершена"
                        return
                    self._sync_window(slug)
                    if self.game_day_counted(slug):
                        nxt = self.next_available(slug)
                        self.state = (f"по {slug} значок за этот день уже получен"
                                      + (f", следующий — {nxt:%d.%m %H:%M} UTC" if nxt else ""))
                        return
                    if self.game_finished(slug):
                        self.state = f"по {slug} все стадии пройдены"
                        return
                    norm = self.daily_norm(slug)
                    if self._game_sec.get(slug, 0) >= norm:
                        self.state = f"норма по {slug} выполнена — значок за этот день получен"
                        await self.mark_day(slug)
                        return
                # Don't ping more often than once a minute: a wake-up (button press, refresh)
                # used to trigger an extra ping and an extra 60 s of credit.
                gap = time.monotonic() - last_ping if last_ping else PIN_PING_EVERY
                if gap < PIN_PING_EVERY - 1:
                    self._wake.clear()
                    await self._sleep(PIN_PING_EVERY - gap)
                    continue
                if await self.tw.watch_points(ch):
                    fails = 0
                    now_m = time.monotonic()
                    # credit real elapsed time (capped), not a fixed 60 s per loop pass
                    credit = int(min(now_m - last_ping, PIN_PING_EVERY + 10)) if last_ping else PIN_PING_EVERY
                    last_ping = now_m
                    self.stats["watch_sec"] += credit
                    if slug is not None:
                        self._game_sec[slug] = self._game_sec.get(slug, 0) + credit
                else:
                    fails += 1
                    if fails >= 3:
                        self._avoid[ch.login] = time.monotonic() + 300
                        self.state = f"{ch.name}: просмотр не засчитывается ({self.tw.watch_note or '?'})"
                        return
                live = await self.tw.streams_info([ch.login])
                if not live:
                    self._avoid[ch.login] = time.monotonic() + 300
                    self.state = f"{ch.name} закончил стрим"
                    return
                if slug:
                    done = self.game_today(slug)
                    self.state = f"смотрю {ch.name} ({slug}: {done}/{norm // 60} мин)"
                else:
                    self.state = f"смотрю {ch.name} (закреплён, {live[0].game_name or '—'})"
                if time.monotonic() - started >= REFRESH_EVERY:
                    return
                self._wake.clear()
                await self._sleep(PIN_PING_EVERY)
        finally:
            self.watching = False
            if slug:
                self.game_channel.pop(slug, None)

    async def _current_channel_if_valid(self, camp: Campaign) -> Channel | None:
        """Stay on the channel we are already watching while it still fits the campaign."""
        cur = self.channel
        if cur is None or self._avoid.get(cur.login, 0) > time.monotonic():
            return None
        if camp.channels and cur.login.lower() not in {l.lower() for _, l in camp.channels}:
            return None
        try:
            live = await self.tw.streams_info([cur.login])
        except TwitchError:
            return None
        if not live:
            return None                      # went offline
        if camp.game_id not in SPECIAL_GAME_IDS and live[0].game_id != camp.game_id:
            return None                      # switched to another game
        return live[0]

    async def _claim_ready(self, camps: list[Campaign]) -> None:
        for camp in camps:
            if not self.wanted(camp):
                continue
            for drop in camp.claimable():
                if self._claim_fails.get(drop.id, 0) >= 5:
                    continue
                if await self.tw.claim(drop):
                    drop.claimed = True
                    if drop.is_badge:
                        self.stats["badges"] += 1
                    line = f"{camp.game_name}: {drop.reward_text}"
                    self.claimed.appendleft(f"{datetime.now(timezone.utc):%d.%m %H:%M} {line}")
                    await self.notify(f"🏅 Получено: {line}")
                else:
                    n = self._claim_fails[drop.id] = self._claim_fails.get(drop.id, 0) + 1
                    if n == 5:
                        await self.notify(
                            f"⚠️ Не удалось забрать «{drop.reward_text}» (5 попыток). "
                            "Заберите вручную: twitch.tv/drops/inventory"
                        )
                    else:
                        await self._sleep(20)

    async def _watch(self, camp: Campaign, drop: Drop, ch: Channel) -> None:
        self.watching = True
        try:
            await self._watch_inner(camp, drop, ch)
        finally:
            self.watching = False

    async def _watch_inner(self, camp: Campaign, drop: Drop, ch: Channel) -> None:
        self.channel = ch
        started = last_progress = time.monotonic()
        fails = 0
        while not self.paused:
            if await self.tw.watch_once(ch):
                fails = 0
                self.stats["watch_sec"] += WATCH_INTERVAL
            else:
                fails += 1
                if fails >= 3:
                    self.state = f"{ch.name} недоступен, ищу другой стрим"
                    return
            now = time.monotonic()
            if now - last_progress >= PROGRESS_EVERY:
                last_progress = now
                if not await self._still_live(camp, ch):
                    self._avoid[ch.login] = now + 600
                    self.state = f"{ch.name} закончил стрим, ищу другой"
                    return
                cur = await self.tw.current_drop(ch)
                if cur and cur.get("dropID") in camp.drops:
                    d = camp.drops[cur["dropID"]]
                    d.current = max(d.current, int(cur.get("currentMinutesWatched") or 0))
                    self.target = (camp, d)
                    self._note_progress(d)
                    if d.current >= d.required:
                        self.state = "дроп набран, забираю"
                        return
            _, grew_at = self._gain.get(drop.id, (0, now))
            if now - grew_at >= STALL_AFTER:
                self._avoid[ch.login] = now + 3600
                self._gain[drop.id] = (drop.current, now)      # give the next channel a fresh window
                self.state = f"прогресс на {ch.name} стоит, ищу другой стрим"
                return
            d = self.target[1] if self.target else drop
            self.state = f"смотрю {ch.name}: {camp.game_name} — {d.reward_text} ({d.current}/{d.required} мин)"
            if now - started >= REFRESH_EVERY:
                return
            self._wake.clear()
            await self._sleep(WATCH_INTERVAL)

    def _note_progress(self, drop: Drop) -> None:
        """Remember when this drop's minutes last grew (survives inventory refreshes)."""
        best, _ = self._gain.get(drop.id, (-1, 0.0))
        if drop.current > best:
            self._gain[drop.id] = (drop.current, time.monotonic())

    async def _still_live(self, camp: Campaign, ch: Channel) -> bool:
        """Is the channel still streaming the right game? (an ended stream keeps serving stale chunks)"""
        try:
            live = await self.tw.streams_info([ch.login])
        except (AuthError, QueryOutdated):
            raise
        except TwitchError:
            return True                      # can't tell, don't switch on a hiccup
        if not live:
            return False
        return camp.game_id in SPECIAL_GAME_IDS or live[0].game_id == camp.game_id

    # ---- reporting

    def status_text(self) -> str:
        lines = [f"Состояние: {self.state}"]
        if self.last_trace and self.state.startswith("ошибка"):
            lines.append(f"Сбой: {self.last_trace}"[:300])
        if self.tw.login:
            lines.append(f"Аккаунт: {self.tw.login} (клиент: {self.tw.client_name})")
        lines.append("Режим: только значки")
        if self.target:
            camp, drop = self.target
            lines.append(f"Цель: {camp.game_name} — {drop.reward_text} ({drop.current}/{drop.required} мин)")
        if self.channel:
            lines.append(f"Канал: {self.channel.name}")
        if self.next_start and not self.target:
            lines.append(f"Следующий дроп откроется: {self.next_start:%d.%m %H:%M} UTC")
        if self.claimed:
            lines.append("Последние награды:\n" + "\n".join(f"• {c}" for c in list(self.claimed)[:5]))
        return "\n".join(lines)

    def campaigns_text(self) -> str:
        rows, hidden = [], 0
        now = datetime.now(timezone.utc)
        for camp in sorted(self.campaigns, key=lambda c: c.ends):
            if not (self.wanted_ignoring_time(camp) and camp.eligible) or camp.ends < now:
                continue
            if camp.drops and all(d.claimed for d in camp.drops.values() if d.id in camp.needed_ids()):
                continue                      # every badge already claimed
            if not camp.watchable:
                hidden += 1                   # sub-only / no watch time: nothing to mine
                continue
            need = camp.needed_ids()
            done = sum(d.claimed for d in camp.drops.values() if d.id in need)
            rows.append(
                f"• {camp.game_name}: {camp.name} — {done}/{len(need)}, до {camp.ends:%d.%m %H:%M} UTC"
            )
        text = "\n".join(rows[:25]) or "Подходящих кампаний сейчас нет."
        if hidden:
            text += f"\n\nСкрыто кампаний только за подписку или без просмотра: {hidden} (подробнее: /why)"
        return text

    def why_text(self) -> str:
        """Per-campaign explanation of what can (not) be mined right now."""
        now = datetime.now(timezone.utc)
        blocks = []
        for camp in sorted(self.campaigns, key=lambda c: c.ends):
            if camp.ends < now or not camp.badge_like:
                continue
            head = [f"{camp.game_name}: {camp.name} (до {camp.ends:%d.%m %H:%M} UTC, статус {camp.status})"]
            if not camp.active:
                head.append(f"  ! кампания не активна, старт {camp.starts:%d.%m %H:%M} UTC")
            if not camp.eligible:
                head.append("  ! в кампании нет значков, пропускаю")
            if self._no_streams.get(camp.id, 0) > time.monotonic():
                head.append("  ! нет стримов с дропами (проверю позже)")
            need = camp.needed_ids()
            for d in camp.drops.values():
                if d.id not in need:
                    continue
                if d.claimed:
                    head.append(f"  ✓ {d.reward_text}: получен")
                    continue
                why = []
                if d.required_subs > 0:
                    why.append("нужна подписка на канал")
                elif d.required <= 0:
                    why.append("в условии 0 минут просмотра")
                if d.starts > now:
                    why.append(f"откроется {d.starts:%d.%m %H:%M} UTC")
                if d.ends <= now:
                    why.append("окно закрыто")
                if any(p in camp.drops and not camp.drops[p].claimed for p in d.preconditions):
                    why.append("ждёт предыдущий дроп")
                if not d.benefits:
                    why.append("без награды")
                if d.required > 0 and d.current >= d.required:
                    why.append("набран, но не забран")
                if self._claim_fails.get(d.id, 0) >= 5:
                    why.append("забрать не удалось (5 попыток)")
                head.append(
                    f"  • {d.reward_text}: {d.current}/{d.required} мин — "
                    + (", ".join(why) if why else "можно майнить")
                )
            blocks.append("\n".join(head))
        text = "\n\n".join(blocks) or "Активных кампаний нет."
        skipped = getattr(self.tw, "skipped", [])
        total = getattr(self.tw, "dash_total", 0)
        text += (f"\n\nTwitch отдал кампаний: {total}, из них активных/будущих: "
                 f"{getattr(self.tw, 'dash_active', '?')}, загружено: {len(self.campaigns)}")
        if skipped:
            text += "\nНе загружены:\n" + "\n".join(f"• {x}" for x in skipped[:15])
        keys = ("chain", "miami", "iguana", "heist", "gta", "grand theft", "busted", "scored big")
        def hit(c):
            hay = " ".join([c.name, c.game_name] + [d.name + " " + d.reward_text for d in c.drops.values()]).lower()
            return any(k in hay for k in keys)
        for c in [c for c in self.campaigns if hit(c)][:3]:
            text += f"\n\nНАЙДЕНО: {c.game_name} / {c.name} ({c.status}, до {c.ends:%d.%m %H:%M}), linked={c.linked}, каналов в списке: {len(c.channels)}"
            for d in c.drops.values():
                text += f"\n  - {d.name} [{','.join(b[2] for b in d.benefits)}] {d.current}/{d.required} мин, subs={d.required_subs}, claimed={d.claimed}"
        names = sorted({c.game_name for c in self.campaigns})
        text += "\n\nИгры в загруженных: " + ", ".join(names)[:900]
        return text

