"""Entry point: Telegram control bot + health endpoint (for the uptime pinger) + miner."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from aiohttp import web

from miner import MAX_MANUAL, Miner
from points import MAX_CHANNELS, PointsFarmer, normalize_login
import ui
from html import escape as esc
from ui import BLUE
from twitch_client import AuthError, Twitch, TwitchError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OWNER_ID = int(os.environ.get("ALLOWED_USER_ID", "0") or 0)
ENV_TWITCH_TOKEN = os.environ.get("TWITCH_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
PORT = int(os.environ.get("PORT", "10000"))
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
LOCAL_STATE = Path(os.environ.get("STATE_FILE", "state.json"))

HELP_TEXT = (
    "ℹ️ <b>Помощь</b>\n\n"
    "Всё управление в панели: /menu\n"
    "Список команд открывается кнопкой «Меню» слева от поля ввода.\n\n"
    "Войти в Twitch: /login, затем /token <i>токен или адрес</i>\n\n"
    "Локальные значки канала: <code>/watch add имя_канала</code> (бот смотрит его в первую очередь), "
    "<code>/watch remove имя</code>, <code>/watch clear</code>\n"
    "Значки за просмотр (GTA VI и др.) включаются кнопкой «🎖 Значки за просмотр» в панели"
)

BOT_COMMANDS = [
    ("menu", "Панель управления"),
    ("status", "Что сейчас происходит"),
    ("campaigns", "Кампании со значками"),
    ("why", "Почему бот майнит или не майнит"),
    ("pause", "Остановить майнинг значков"),
    ("resume", "Продолжить майнинг значков"),
    ("refresh", "Перепроверить кампании сейчас"),
    ("points", "Баллы канала"),
    ("watch", "Каналы для значков вручную"),
    ("login", "Как войти в Twitch"),
    ("token", "Войти по токену"),
    ("help", "Помощь"),
]


# ------------------------------------------------------------------------ storage


class Store:
    """Key-value persistence: Supabase (PostgREST) if configured, otherwise a local file."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.remote = bool(SUPABASE_URL and SUPABASE_KEY)

    def _headers(self) -> dict:
        h = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
        if SUPABASE_KEY.startswith("eyJ"):  # legacy JWT-style keys also go in Authorization
            h["Authorization"] = f"Bearer {SUPABASE_KEY}"
        return h

    async def get(self, key: str) -> str | None:
        if self.remote:
            try:
                async with self.session.get(
                    f"{SUPABASE_URL}/rest/v1/kv", params={"key": f"eq.{key}", "select": "value"},
                    headers=self._headers(),
                ) as r:
                    if r.status == 200:
                        rows = await r.json()
                        return rows[0]["value"] if rows else None
                    log.warning("supabase get %s: HTTP %s", key, r.status)
            except Exception as exc:  # noqa: BLE001
                log.warning("supabase get failed: %r", exc)
            return None
        try:
            return json.loads(LOCAL_STATE.read_text()).get(key)
        except (OSError, ValueError):
            return None

    async def set(self, key: str, value: str) -> None:
        if self.remote:
            try:
                async with self.session.post(
                    f"{SUPABASE_URL}/rest/v1/kv", params={"on_conflict": "key"},
                    headers={**self._headers(), "Prefer": "resolution=merge-duplicates"},
                    json={"key": key, "value": value},
                ) as r:
                    if r.status >= 300:
                        log.warning("supabase set %s: HTTP %s %s", key, r.status, (await r.text())[:200])
            except Exception as exc:  # noqa: BLE001
                log.warning("supabase set failed: %r", exc)
            return
        try:
            data = json.loads(LOCAL_STATE.read_text()) if LOCAL_STATE.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[key] = value
        LOCAL_STATE.write_text(json.dumps(data))


# ------------------------------------------------------------------------ telegram


class Telegram:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base = f"https://api.telegram.org/bot{TG_TOKEN}"

    async def call(self, method: str, **params):
        async with self.session.post(
            f"{self.base}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=70)
        ) as r:
            data = await r.json()
        if not data.get("ok"):
            raise TwitchError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    async def _html(self, method: str, params: dict, text: str, markup: dict | None):
        p = dict(params, text=text[:4000], parse_mode="HTML")
        if markup:
            p["reply_markup"] = markup
        return await self.call(method, **p)

    async def send(self, text: str, markup: dict | None = None, html: bool = False) -> int | None:
        """Send a message to the owner; returns its message_id."""
        if not OWNER_ID:
            return None
        try:
            if markup or html:
                res = await self._html("sendMessage", {"chat_id": OWNER_ID, "disable_web_page_preview": True},
                                       text, markup)
                return res["message_id"]
            last = None
            for i in range(0, len(text), 3800):
                last = (await self.call("sendMessage", chat_id=OWNER_ID, text=text[i:i + 3800],
                                        disable_web_page_preview=True))["message_id"]
            return last
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed: %r", exc)
            return None

    async def edit(self, message_id: int, text: str, markup: dict | None = None) -> None:
        try:
            await self._html("editMessageText",
                             {"chat_id": OWNER_ID, "message_id": message_id, "disable_web_page_preview": True},
                             text, markup)
        except TwitchError as exc:
            if "not modified" not in str(exc):
                log.warning("telegram edit failed: %s", exc)


class App:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession
        self.tw = Twitch()
        self.tg: Telegram
        self.store: Store
        self.miner: Miner
        self.points: PointsFarmer
        self.awaiting: tuple[str, int] | None = None
        self._saved_stats = (-1, -1)
        self._saved_points_state = ""

    # ---- twitch auth helpers

    async def apply_token(self, token: str) -> str:
        m = re.search(r"access_token=([A-Za-z0-9]+)", token)
        if m:
            token = m.group(1)
        info = await self.tw.set_token(token)
        await self.store.set("twitch_token", self.tw.token or "")
        self.miner.wake()
        return info.get("login") or str(info.get("user_id"))

    LOGIN_URL = (
        "https://www.twitch.tv/login?client_id=kd1unb4b3q4t58fwlpcbzcbnm76a8fp&redirect_params="
        "client_id%3Dkd1unb4b3q4t58fwlpcbzcbnm76a8fp%26redirect_uri%3Dhttps%253A%252F%252F"
        "www.twitch.tv%252F%26response_type%3Dtoken"
    )

    async def login_help(self) -> None:
        await self.tg.send(
            "Вход в Twitch (нужен компьютер):\n"
            "1) Открой эту ссылку и войди в нужный аккаунт:\n" + self.LOGIN_URL + "\n"
            "2) После входа тебя перекинет на twitch.tv, а в адресной строке будет "
            "…/#access_token=ТОКЕН&… Пока страница не перезаписала адрес, скопируй его целиком.\n"
            "3) Отправь мне: /token <скопированный адрес или сам токен>\n\n"
            "Если адрес сразу пропадает: F12 → вкладка Network → включи Preserve log, "
            "повтори вход и найди в запросах адрес с access_token."
        )

    # ---- telegram commands

    async def handle(self, text: str, message_id: int) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        m = self.miner
        if cmd in ("/start", "/menu"):
            await self.send_panel()
        elif cmd == "/help":
            await self.tg.send(HELP_TEXT, ui.kb([("📋 Открыть панель", "menu")]), html=True)
        elif cmd == "/status":
            await self.tg.send(m.status_text() + "\n" + self.points.summary())
        elif cmd == "/campaigns":
            await self.tg.send(m.campaigns_text())
        elif cmd == "/why":
            await self.tg.send(m.why_text())
        elif cmd == "/pause":
            m.paused = True
            m.wake()
            await self.tg.send("⏸ Пауза.")
        elif cmd == "/resume":
            m.paused = False
            m.wake()
            await self.tg.send("▶️ Продолжаю.")
        elif cmd == "/refresh":
            m.wake()
            await self.tg.send("Перепроверяю кампании…")
        elif cmd == "/points":
            await self.handle_points(arg)
        elif cmd == "/watch":
            await self.handle_watch(arg)
        elif cmd == "/login":
            await self.login_help()
        elif cmd == "/token":
            try:
                await self.tg.call("deleteMessage", chat_id=OWNER_ID, message_id=message_id)
            except Exception:  # noqa: BLE001
                pass
            if not arg:
                await self.tg.send("Использование: /token <значение>")
                return
            try:
                login = await self.apply_token(arg)
                await self.tg.send(f"✅ Вход выполнен: {login}")
            except TwitchError as exc:
                await self.tg.send(f"❌ {exc}")
        else:
            await self.send_panel()

    async def handle_watch(self, arg: str) -> None:
        sub, _, rest = arg.partition(" ")
        sub = sub.lower()
        m = self.miner
        if sub in ("add", "+"):
            login = normalize_login(rest)
            await self.tg.send(m.add_manual(login) if login else "Не похоже на имя канала. Пример: /watch add shroud")
        elif sub in ("remove", "del", "-"):
            login = normalize_login(rest)
            await self.tg.send(m.remove_manual(login) if login else "Такого канала нет в списке")
        elif sub == "clear":
            await self.tg.send(m.clear_manual())
        elif sub == "days":
            # /watch days gta6 2 [минут]  — выставить реальное число полученных значков
            parts = rest.split()
            if len(parts) < 2 or not parts[1].isdigit() or (len(parts) > 2 and not parts[2].isdigit()):
                await self.tg.send("Пример: /watch days gta6 2 (получено 2 значка) или /watch days gta6 2 34 (и 34 минуты сегодня)")
                return
            await self.tg.send(await m.set_progress(m.game_slug(parts[0]), int(parts[1]),
                                                    int(parts[2]) if len(parts) > 2 else 0))
            return
        elif sub == "game":
            act, _, val = rest.partition(" ")
            if act.lower() in ("remove", "del", "-"):
                await self.tg.send(m.remove_game(val))
            elif act.lower() in ("", "list"):
                await self.tg.send(m.games_text())
                return
            else:
                await self.tg.send(m.add_game(rest))
            await self.store.set("watch_games", ",".join(m.games))
            return
        else:
            await self.tg.send(m.manual_text())
            return
        await self.store.set("manual_channels", ",".join(m.manual))

    async def handle_points(self, arg: str) -> None:
        sub, _, rest = arg.partition(" ")
        sub = sub.lower()
        pf = self.points
        if sub in ("add", "+"):
            await self.tg.send(pf.add(rest))
        elif sub in ("remove", "del", "-"):
            await self.tg.send(pf.remove(rest))
        elif sub == "mode":
            login, _, mode = rest.strip().partition(" ")
            await self.tg.send(pf.set_mode(login, mode.strip().lower()))
        elif sub == "streak":
            login, _, num = rest.strip().partition(" ")
            await self.tg.send(pf.set_streak(login, num))
        elif sub in ("on", "off"):
            pf.set_enabled(sub == "on")
            await self.tg.send("Сбор баллов " + ("включён" if sub == "on" else "выключен"))
        else:
            await self.tg.send(pf.status_text())
            return
        await self.persist_points()

    # ---- panel (inline keyboard UI)

    def screen(self, name: str) -> tuple[str, dict]:
        m, pf = self.miner, self.points
        if name == "inv":
            return ui.inventory_text(m), ui.inventory_markup()
        if name == "why":
            return ui.why_text(m), ui.inventory_markup()
        if name == "pts":
            return ui.points_text(pf), ui.points_markup(pf)
        if name == "pts_rm":
            return ui.remove_text(pf), ui.remove_markup(pf)
        if name == "bdg":
            return ui.badges_text(m), ui.badges_markup(m)
        if name.startswith("bdg_s:"):
            return ui.evolution_text(m, self.tw, name[6:]), ui.evolution_markup(m, name[6:])
        if name == "set":
            return ui.settings_text(m, self.tw, self.store.remote), ui.settings_markup()
        return ui.panel_text(m, pf, self.tw), ui.main_markup(m)

    async def setup_commands(self) -> None:
        """Fill the blue "Menu" button next to the input field with our commands."""
        try:
            await self.tg.call("setMyCommands",
                               commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
            await self.tg.call("setChatMenuButton", menu_button={"type": "commands"})
        except Exception as exc:  # noqa: BLE001
            log.warning("setMyCommands failed: %r", exc)

    async def send_panel(self) -> None:
        text, markup = self.screen("menu")
        await self.tg.send(text, markup, html=True)

    async def handle_callback(self, cq: dict) -> None:
        if (cq.get("from") or {}).get("id") != OWNER_ID:
            return
        try:
            await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:  # noqa: BLE001
            pass
        data = cq.get("data", "")
        mid = (cq.get("message") or {}).get("message_id")
        if mid is None:
            return
        # Every button press cancels a pending free-text prompt ("add channel"),
        # otherwise a later random message would be taken as a channel name.
        self.awaiting = None
        m, pf = self.miner, self.points
        screen = "menu"
        if data == "refresh":
            m.wake()
        elif data == "toggle":
            m.paused = not m.paused
            m.wake()
        elif data in ("inv", "why", "pts", "set", "pts_rm", "bdg"):
            screen = data
        elif data.startswith("bdg_s:"):
            screen = data
        elif data.startswith("bdg_t:"):
            slug = data[6:]
            if not m.toggle_game(slug) and slug not in m.games:
                await self.tg.send("⚠️ Достигнут лимит категорий, сначала выключи одну.")
            await self.store.set("watch_games", ",".join(m.games))
            screen = f"bdg_s:{slug}"
        elif data.startswith("bdg_day:"):
            await m.mark_day(data[8:], manual=True)
            screen = f"bdg_s:{data[8:]}"
        elif data == "bdg_add":
            self.awaiting = ("game_add", mid)
            await self.tg.edit(mid, "<b>➕ СВОЯ КАТЕГОРИЯ</b>\n\nПришли ссылку на категорию Twitch, например\n"
                                    "<code>twitch.tv/directory/category/grand-theft-auto-vi</code>\n"
                                    "или короткое имя вроде <code>gta6</code>.",
                               ui.kb([("⬅️ Отмена", "bdg")]))
            return
        elif data == "pts_toggle":
            pf.set_enabled(not pf.enabled)
            await self.persist_points()
            screen = "pts"
        elif data.startswith("pts_mode:"):
            pf.cycle_mode(data[9:])
            await self.persist_points()
            screen = "pts"
        elif data == "pts_add":
            self.awaiting = ("points_add", mid)
            prompt = ("<b>➕ ДОБАВИТЬ КАНАЛ</b>\n\n"
                      "Пришли имя канала или ссылку на него.\n"
                      "Подойдёт <code>shroud</code>, <code>twitch.tv/shroud</code> "
                      "или <code>https://m.twitch.tv/shroud</code>.")
            if len(pf.channels) >= MAX_CHANNELS:
                prompt += (f"\n\n⚠️ Уже <b>{len(pf.channels)}</b> из {MAX_CHANNELS} каналов — "
                           "сначала убери один кнопкой «➖ Убрать канал».")
            await self.tg.edit(mid, prompt, ui.kb([("⬅️ Отмена", "pts")]))
            return
        elif data.startswith("prm:"):
            pf.remove(data[4:])
            await self.persist_points()
            screen = "pts_rm" if pf.channels else "pts"
        elif data == "login":
            await self.login_help()
            return
        text, markup = self.screen(screen)
        await self.tg.edit(mid, text, markup)

    async def announce_badge(self, badge: str, game: str) -> None:
        text = (
            "🎁 <b>Вышел новый значок Twitch!</b>\n\n"
            f"💎 <b>{esc(badge)}</b> [{esc(game)}]\n\n"
            "Перейдите в ваши аккаунты, чтобы запустить сбор значка!"
        )
        await self.tg.send(text, ui.kb([("🚀 Перейти в аккаунты", "menu", BLUE)],
                                       [("🏅 Каталог значков", "inv", BLUE)]), html=True)

    async def persist_days(self) -> None:
        await self.store.set("watch_days", self.miner.days_json())

    async def persist_points(self) -> None:
        await self.store.set("points_channels", ",".join(self.points.channels))
        await self.store.set("points_enabled", "1" if self.points.enabled else "0")
        await self.store.set("points_modes", self.points.modes_json())

    async def handle_awaited(self, text: str, message_id: int) -> bool:
        """Free-text answer to a button prompt. Returns True if consumed."""
        if not self.awaiting or text.startswith("/"):
            return False
        kind, panel_id = self.awaiting
        self.awaiting = None
        if kind == "points_add":
            result = self.points.add(text)
            added = result.startswith("Добавил")
            await self.persist_points()
            try:
                await self.tg.call("deleteMessage", chat_id=OWNER_ID, message_id=message_id)
            except Exception:  # noqa: BLE001
                pass
            # A separate short message: the result used to be appended to the bottom of a long
            # edited panel, where it was easy to miss — it looked like nothing had happened.
            await self.tg.send(("✅ " if added else "⚠️ ") + result)
            if not added:                       # let the user correct the name without pressing the button again
                self.awaiting = ("points_add", panel_id)
            t, markup = self.screen("pts")
            await self.tg.edit(panel_id, t, markup)
            return True
        if kind == "game_add":
            result = self.miner.add_game(text)
            added = result.startswith("Добавил")
            await self.store.set("watch_games", ",".join(self.miner.games))
            try:
                await self.tg.call("deleteMessage", chat_id=OWNER_ID, message_id=message_id)
            except Exception:  # noqa: BLE001
                pass
            await self.tg.send(("✅ " if added else "⚠️ ") + result)
            if not added:
                self.awaiting = ("game_add", panel_id)
            t, markup = self.screen("bdg")
            await self.tg.edit(panel_id, t, markup)
            return True
        return False

    async def save_stats_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            cur = (self.miner.stats["badges"], self.miner.stats["watch_sec"])
            if cur != self._saved_stats:
                await self.store.set("stat_badges", str(cur[0]))
                await self.store.set("stat_watch_sec", str(cur[1]))
                self._saved_stats = cur
            days = self.miner.days_json()
            if days != getattr(self, "_saved_days", days):
                await self.store.set("watch_days", days)
            self._saved_days = days
            state = self.points.export_state()
            if state != self._saved_points_state:
                await self.store.set("points_state", state)
                self._saved_points_state = state

    async def poll_telegram(self) -> None:
        offset = 0
        while True:
            try:
                updates = await self.tg.call("getUpdates", offset=offset, timeout=50,
                                             allowed_updates=["message", "callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed: %r", exc)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    try:
                        await self.handle_callback(u["callback_query"])
                    except Exception as exc:  # noqa: BLE001
                        log.exception("callback failed")
                        await self.tg.send(f"⚠️ {exc!r}"[:400])
                    continue
                msg = u.get("message") or {}
                if (msg.get("from") or {}).get("id") != OWNER_ID or not msg.get("text"):
                    continue  # ignore everyone but the owner
                try:
                    if await self.handle_awaited(msg["text"], msg["message_id"]):
                        continue
                    await self.handle(msg["text"], msg["message_id"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("command failed")
                    await self.tg.send(f"⚠️ {exc!r}"[:400])

    # ---- web (health) + keepalive

    async def health(self, request: web.Request) -> web.Response:
        return web.Response(text=f"ok | {self.miner.state}\n")

    async def self_ping(self) -> None:
        """Backup for the external uptime pinger: hit our own public URL every 10 min."""
        if not SELF_URL:
            return
        while True:
            await asyncio.sleep(600)
            try:
                async with self.session.get(f"{SELF_URL}/health") as r:
                    await r.read()
            except Exception as exc:  # noqa: BLE001
                log.warning("self ping failed: %r", exc)

    # ---- startup

    async def run(self) -> None:
        if not TG_TOKEN or not OWNER_ID:
            raise SystemExit("Нужны переменные TELEGRAM_TOKEN и ALLOWED_USER_ID")
        self.session = aiohttp.ClientSession()
        self.store = Store(self.session)
        self.tg = Telegram(self.session)
        await self.tw.start()

        self.miner = Miner(self.tw, self.tg.send)
        saved = await self.store.get("points_channels")
        if saved is None:
            saved = os.environ.get("POINTS_CHANNELS", "")
        self.miner.manual = [
            c for c in ((normalize_login(x) for x in (await self.store.get("manual_channels") or "").split(",")))
            if c
        ][:MAX_MANUAL]
        self.miner.games = [
            g for g in ((self.miner.game_slug(x) for x in (await self.store.get("watch_games") or "").split(",")))
            if g
        ][:3]
        self.miner.load_days(await self.store.get("watch_days"))
        self.miner.on_change = self.persist_days
        self.miner.on_new_badge = self.announce_badge
        self.points = PointsFarmer(self.tw, self.miner, [c for c in saved.split(",") if c.strip()])
        self.points.set_enabled((await self.store.get("points_enabled")) != "0")
        old_mode = await self.store.get("points_gather")      # older versions had one mode for all channels
        if old_mode in ("points", "streak", "both"):
            self.points.default_mode = old_mode
        self.points.load_modes(await self.store.get("points_modes"))
        self.points.load_state(await self.store.get("points_state"))
        self._saved_points_state = self.points.export_state()
        for key, name in (("stat_badges", "badges"), ("stat_watch_sec", "watch_sec")):
            try:
                self.miner.stats[name] = int(await self.store.get(key) or 0)
            except ValueError:
                pass
        self._saved_stats = (self.miner.stats["badges"], self.miner.stats["watch_sec"])

        # web server first: Render wants the port bound quickly
        web_app = web.Application()
        web_app.add_routes([
            web.get("/", self.health),
            web.get("/health", self.health),
        ])
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("health endpoint on :%s", PORT)

        await self.setup_commands()
        token = await self.store.get("twitch_token") or ENV_TWITCH_TOKEN
        note = ""
        if token:
            try:
                await self.apply_token(token)
            except AuthError as exc:
                note = f"🔑 {exc}. Войди заново: ⚙️ Настройки → 🔑 или /login"
            except TwitchError as exc:
                note = f"⚠️ Проверка токена не удалась: {exc}"
        else:
            note = "🟡 Вход в Twitch не выполнен. Нажми ⚙️ Настройки → 🔑 или отправь /login"
        if note:
            await self.tg.send(note)
        await asyncio.gather(self.miner.run(), self.poll_telegram(), self.self_ping(), self.points.run(), self.save_stats_loop())


if __name__ == "__main__":
    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        pass
