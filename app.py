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

from miner import Miner, Settings
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

HELP = (
    "Команды:\n"
    "/status — что сейчас происходит\n"
    "/campaigns — подходящие кампании\n"
    "/pause, /resume — остановить / продолжить\n"
    "/refresh — перепроверить кампании прямо сейчас\n"
    "/mode badges|all — только значки и эмоуты (по умолчанию) или всё подряд\n"
    "/games Название, Название — доп. игры, дроп которых тоже нужен (пусто = сброс)\n"
    "/login — как войти в Twitch\n"
    "/token <токен или адрес> — войти по токену (сообщение будет удалено)"
)


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

    async def send(self, text: str) -> None:
        if not OWNER_ID:
            return
        try:
            for i in range(0, len(text), 3800):
                await self.call("sendMessage", chat_id=OWNER_ID, text=text[i:i + 3800],
                                disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed: %r", exc)


class App:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession
        self.tw = Twitch()
        self.tg: Telegram
        self.store: Store
        self.settings = Settings(
            os.environ.get("MINE_MODE", "badges"),
            [g for g in os.environ.get("EXTRA_GAMES", "").split(",")],
        )
        self.miner: Miner

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
        if cmd in ("/start", "/help"):
            await self.tg.send(HELP)
        elif cmd == "/status":
            await self.tg.send(m.status_text())
        elif cmd == "/campaigns":
            await self.tg.send(m.campaigns_text())
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
        elif cmd == "/mode":
            if arg not in ("badges", "all"):
                await self.tg.send("Использование: /mode badges или /mode all")
                return
            self.settings.mode = arg
            await self.store.set("mode", arg)
            m.wake()
            await self.tg.send(f"Режим: {arg}")
        elif cmd == "/games":
            names = [g.strip().lower() for g in arg.split(",") if g.strip()]
            self.settings.extra_games = names
            await self.store.set("extra_games", ",".join(names))
            m.wake()
            await self.tg.send("Доп. игры: " + (", ".join(names) or "нет"))
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
            await self.tg.send(HELP)

    async def poll_telegram(self) -> None:
        offset = 0
        while True:
            try:
                updates = await self.tg.call("getUpdates", offset=offset, timeout=50,
                                             allowed_updates=["message"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed: %r", exc)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if (msg.get("from") or {}).get("id") != OWNER_ID or not msg.get("text"):
                    continue  # ignore everyone but the owner
                try:
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

        # restore persisted settings
        mode = await self.store.get("mode")
        if mode in ("badges", "all"):
            self.settings.mode = mode
        games = await self.store.get("extra_games")
        if games is not None:
            self.settings.extra_games = [g for g in games.split(",") if g]
        self.miner = Miner(self.tw, self.tg.send, self.settings)

        # web server first: Render wants the port bound quickly
        web_app = web.Application()
        web_app.add_routes([web.get("/", self.health), web.get("/health", self.health)])
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("health endpoint on :%s", PORT)

        token = await self.store.get("twitch_token") or ENV_TWITCH_TOKEN
        if token:
            try:
                login = await self.apply_token(token)
                await self.tg.send(f"🟢 Бот запущен, аккаунт Twitch: {login}\n\n{self.miner.status_text()}")
            except AuthError as exc:
                await self.tg.send(f"🔑 {exc}. Войди заново: /login или /token <токен>")
            except TwitchError as exc:
                await self.tg.send(f"⚠️ Проверка токена не удалась: {exc}")
        else:
            await self.tg.send("🟡 Бот запущен, но токена Twitch нет. Отправь /login или /token <токен>.\n\n" + HELP)

        await asyncio.gather(self.miner.run(), self.poll_telegram(), self.self_ping())


if __name__ == "__main__":
    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        pass
