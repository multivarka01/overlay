"""Mining loop: pick a campaign, find a stream, "watch" it, claim finished drops."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
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


class Settings:
    """Runtime settings (changed from Telegram, persisted by the app)."""

    def __init__(self, mode: str = "badges", extra_games: list[str] | None = None):
        self.mode = mode if mode in ("badges", "all") else "badges"
        self.extra_games = [g.strip().lower() for g in (extra_games or []) if g.strip()]


class Miner:
    def __init__(self, tw: Twitch, notify: Notify, settings: Settings):
        self.tw = tw
        self.notify = notify
        self.settings = settings
        self.paused = False
        self.state = "запускаюсь"
        self.target: tuple[Campaign, Drop] | None = None
        self.channel: Channel | None = None
        self.campaigns: list[Campaign] = []
        self.claimed: deque[str] = deque(maxlen=10)
        self.next_start: datetime | None = None
        self._wake = asyncio.Event()
        self._known: set[str] | None = None
        self._announced: set[str] = set()
        self._claim_fails: dict[str, int] = {}
        self._avoid: dict[str, float] = {}
        self._no_streams: dict[str, float] = {}
        self._last_error: tuple[str, float] = ("", 0.0)

    # ---- control

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
        if not (camp.active and camp.eligible):
            return False
        name = camp.game_name.lower()
        if any(g in name or g in camp.name.lower() for g in self.settings.extra_games):
            return True
        return self.settings.mode == "all" or camp.badge_like

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
        """Tell the user once if a wanted drop can no longer be finished in time."""
        now = datetime.now(timezone.utc)
        for camp in camps:
            if not self.wanted(camp):
                continue
            for d in camp.earnable(now):
                left = (min(camp.ends, d.ends) - now).total_seconds() / 60
                key = f"late:{d.id}"
                if d.remaining > 0 and d.remaining > left and key not in self._announced:
                    self._announced.add(key)
                    await self.notify(
                        f"⏰ Не успею: {camp.game_name} — {d.reward_text}. "
                        f"Нужно ещё {d.remaining} мин, до конца {max(int(left), 0)} мин."
                    )

    def _next_start(self, camps: list[Campaign]) -> datetime | None:
        now = datetime.now(timezone.utc)
        upcoming = [
            d.starts for c in camps if c.eligible and self.wanted_ignoring_time(c)
            for d in c.drops.values() if not d.claimed and d.starts > now
        ]
        return min(upcoming) if upcoming else None

    def wanted_ignoring_time(self, camp: Campaign) -> bool:
        name = camp.game_name.lower()
        if any(g in name for g in self.settings.extra_games):
            return True
        return self.settings.mode == "all" or camp.badge_like

    async def _announce_new(self, camps: list[Campaign]) -> None:
        ids = {c.id for c in camps}
        if self._known is None:        # first cycle after start: remember silently
            self._known = ids
            return
        for camp in camps:
            if camp.id in self._known or camp.id in self._announced:
                continue
            self._announced.add(camp.id)
            if self.wanted_ignoring_time(camp) and camp.eligible:
                rewards = ", ".join(sorted({d.reward_text for d in camp.drops.values()}))
                await self.notify(
                    f"🆕 Новая кампания: {camp.name} ({camp.game_name})\n"
                    f"Награды: {rewards}\n"
                    f"{camp.starts:%d.%m %H:%M} → {camp.ends:%d.%m %H:%M} UTC"
                )
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
                await self._error(f"⚠️ Ошибка майнера: {exc!r}"[:400], every=1800)
                await self._sleep(60)

    async def _handle_outdated(self, op_name: str) -> None:
        self.state = "Twitch поменял запросы, обновляю"
        try:
            changed = await self.tw.refresh_hashes()
        except Exception as exc:  # noqa: BLE001
            changed = 0
            log.warning("hash refresh failed: %r", exc)
        if changed:
            await self.notify(f"🔧 Twitch обновил запросы ({op_name}), подтянул свежие. Продолжаю.")
            return
        await self._error(
            f"🛠 Twitch изменил запрос {op_name}, а свежих хэшей пока нет. "
            "Проверю снова через час; скорее всего нужно дождаться обновления TwitchDropsMiner.",
            every=6 * 3600,
        )
        await self._sleep(3600)

    async def _cycle(self) -> None:
        self.state = "обновляю инвентарь"
        camps = await self.tw.fetch_campaigns()
        self.campaigns = camps
        await self._announce_new(camps)
        await self._warn_deadlines(camps)
        await self._claim_ready(camps)

        ranked = self.rank(camps)
        self.next_start = self._next_start(camps)
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
            await self._watch(camp, drop, ch)
            return

        self.target = self.channel = None
        self.state = f"нет подходящих стримов (кампаний в очереди: {len(ranked)})"
        await self._sleep(120)

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
        self.channel = ch
        if drop.id not in self._announced:
            self._announced.add(drop.id)
            await self.notify(
                f"▶️ Майню: {camp.game_name} — {drop.reward_text} "
                f"({drop.current}/{drop.required} мин), канал {ch.name}"
            )
        started = last_progress = last_gain = time.monotonic()
        best = drop.current
        fails = 0
        while not self.paused:
            if await self.tw.watch_once(ch):
                fails = 0
            else:
                fails += 1
                if fails >= 3:
                    self.state = f"{ch.name} недоступен, ищу другой стрим"
                    return
            now = time.monotonic()
            if now - last_progress >= PROGRESS_EVERY:
                last_progress = now
                cur = await self.tw.current_drop(ch)
                if cur and cur.get("dropID") in camp.drops:
                    d = camp.drops[cur["dropID"]]
                    d.current = max(d.current, int(cur.get("currentMinutesWatched") or 0))
                    self.target = (camp, d)
                    if d.current > best:
                        best, last_gain = d.current, now
                    if d.current >= d.required:
                        self.state = "дроп набран, забираю"
                        return
            if now - last_gain >= STALL_AFTER:
                self._avoid[ch.login] = now + 3600
                self.state = f"прогресс на {ch.name} стоит, ищу другой стрим"
                return
            d = self.target[1] if self.target else drop
            self.state = f"смотрю {ch.name}: {camp.game_name} — {d.reward_text} ({d.current}/{d.required} мин)"
            if now - started >= REFRESH_EVERY:
                return
            self._wake.clear()
            await self._sleep(WATCH_INTERVAL)

    # ---- reporting

    def status_text(self) -> str:
        lines = [f"Состояние: {self.state}"]
        if self.tw.login:
            lines.append(f"Аккаунт: {self.tw.login} (клиент: {self.tw.client_name})")
        lines.append(f"Режим: {self.settings.mode}" + (
            f" + {', '.join(self.settings.extra_games)}" if self.settings.extra_games else ""))
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
        rows = []
        for camp in sorted(self.campaigns, key=lambda c: c.ends):
            if not (self.wanted_ignoring_time(camp) and camp.eligible) or camp.ends < datetime.now(timezone.utc):
                continue
            if camp.drops and all(d.claimed for d in camp.drops.values()):
                continue                      # everything already claimed
            done = sum(d.claimed for d in camp.drops.values())
            rows.append(
                f"• {camp.game_name}: {camp.name} — {done}/{len(camp.drops)}, до {camp.ends:%d.%m %H:%M} UTC"
            )
        return "\n".join(rows[:25]) or "Подходящих кампаний сейчас нет."
