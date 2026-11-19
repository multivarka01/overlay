"""Panel texts (HTML) and inline keyboards for the Telegram bot."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from html import escape as esc
from zoneinfo import ZoneInfo

from points import MAX_CHANNELS, STREAK_MIN_SEC

try:
    TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Yekaterinburg"))
except Exception:  # noqa: BLE001 - unknown zone or no tzdata
    TZ = timezone.utc


BLUE, GREEN, RED = "primary", "success", "danger"


def kb(*rows) -> dict:
    """Rows of (text, callback_data[, style]) buttons; style colors the button in recent Telegram apps."""
    def button(b) -> dict:
        d = {"text": b[0], "callback_data": b[1]}
        if len(b) > 2 and b[2] in (BLUE, GREEN, RED):
            d["style"] = b[2]
        return d
    return {"inline_keyboard": [[button(b) for b in row] for row in rows]}


def duration(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}ч {m}м" if h else f"{m}м"


def on_off(flag: bool) -> str:
    return "<b>ВКЛ</b>" if flag else "<b>ВЫКЛ</b>"


def mode_text(m) -> str:
    if m.paused:
        return "Пауза"
    if not m.tw.token:
        return "Нужен вход в Twitch (⚙️ Настройки)"
    if m.watching:
        return "Майнинг значка"
    return "Ожидание стримов / новых значков"


def panel_text(miner, points, tw) -> str:
    acct = f"@{tw.login}" if tw.login else "вход не выполнен"
    tgt = miner.target or miner.next_up
    game = esc(tgt[0].game_name) if tgt else "—"
    streamer = esc(miner.channel.name) if (miner.watching and miner.channel) else "Стримы оффлайн"

    if tgt:
        camp, drop = tgt
        badge = esc(drop.reward_text)
        if miner.watching:
            status = f"Набрано {drop.current}/{drop.required} мин"
        else:
            status = "Стримы оффлайн или значок получен. Ожидание запуска…"
    else:
        badge = "—"
        status = "Сейчас нет активных значков"
        if miner.next_start:
            local = miner.next_start.astimezone(TZ)
            status += f". Следующий откроется {local:%d.%m %H:%M}"

    points_note = f" · каналов {len(points.channels)}" if points.channels else ""
    now = datetime.now(TZ)
    return (
        f"👤 <b>АККАУНТ:</b> <code>{esc(acct)}</code>\n\n"
        "<b>❗ СТАТУС И СТРИМ</b>\n"
        f"┌ Режим: <i>{esc(mode_text(miner))}</i>\n"
        f"├ Игра: <i>{game}</i>\n"
        f"└ Стример (Drops): <i>{streamer}</i>\n\n"
        "<b>🎯 ЦЕЛЕВОЙ ЗНАЧОК</b>\n"
        f"┌ <b>{badge}</b>\n"
        f"└ <i>{esc(status)}</i>\n\n"
        "<b>⚙️ НАСТРОЙКИ</b>\n"
        f"┌ Автосбор значков: {on_off(not miner.paused)}\n"
        f"└ Баллы канала: {on_off(points.enabled and bool(points.channels))}{points_note}\n\n"
        "<b>📊 СТАТИСТИКА</b>\n"
        f"┌ Получено значков: <b>{miner.stats['badges']}</b>\n"
        f"├ Время просмотра: <b>{duration(miner.stats['watch_sec'])}</b>\n"
        f"└ Бонусов баллов собрано: <b>{sum(points.claims.values())}</b>\n\n"
        f"🕒 <i>Обновлено: {now:%H:%M:%S}</i>"
    )


def main_markup(miner) -> dict:
    run = ("▶️ Запустить", "toggle", GREEN) if miner.paused else ("⏸ Пауза", "toggle", RED)
    return kb(
        [run, ("🏅 Инвентарь", "inv", BLUE)],
        [("🎖 Значки за просмотр", "bdg", BLUE)],
        [("💎 Баллы канала", "pts", BLUE), ("⚙️ Настройки", "set")],
        [("❓ Почему", "why"), ("🔄 Обновить", "refresh")],
    )


def back_row():
    return [("⬅️ Назад", "menu")]


def inventory_text(miner) -> str:
    return "<b>🏅 КАМПАНИИ СО ЗНАЧКАМИ</b>\n\n" + esc(miner.campaigns_text())


def inventory_markup() -> dict:
    return kb(back_row())


def why_text(miner) -> str:
    return "<b>❓ ПОЧЕМУ ТАК</b>\n\n" + esc(miner.why_text())


MODE_ICON = {"points": "💰 Баллы", "streak": "🔥 Серия", "both": "💰🔥 Всё"}
MODE_COLOR = {"points": BLUE, "streak": RED, "both": GREEN}


def _num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def points_text(points) -> str:
    head = "💎 <b>БАЛЛЫ КАНАЛА</b>\n━━━━━━━━━━━━━━━━━━━━"
    if not points.channels:
        return (
            head
            + "\n\n<i>Пока ни одного канала.</i>\n"
            "Нажми <b>➕ Добавить канал</b> или отправь\n"
            "<code>/points add имя_канала</code>."
        )

    live = {c.login.lower(): c for c in points.live}
    lines = [head, "", f"📡 Каналов: <b>{len(points.channels)}</b> · в эфире: <b>{len(set(live) & set(points.channels))}</b>"]
    if len(points.channels) >= MAX_CHANNELS:
        lines += ["", f"⚠️ <i>Лимит {MAX_CHANNELS} каналов — чтобы добавить новый, убери один.</i>"]

    for login in points.channels:
        lines.append("")
        if login in points.missing:
            lines.append(f"❌ <b>{esc(login)}</b>")
            lines.append("<i>такого канала нет на Twitch — проверь логин</i>")
            continue
        ch = live.get(login)
        title = f"{'🟢' if ch else '⚫'} <b>{esc(login)}</b> · {MODE_ICON[points.mode_of(login)]}"
        if not ch:
            title += " <i>не в эфире</i>"
        lines.append(title)

        body = []
        if points.wants_points(login):
            bal = points.balance.get(login)
            if bal is not None:
                body.append(f"💰 Баланс: <b>{_num(bal)}</b>")
            rate = points.hourly_rate(login)
            if rate is not None and round(rate) != 0:
                body.append(f"📈 Доход: <b>≈{rate:+.0f} баллов/ч</b>")
            body.append(f"🎁 Бонусов: <b>{points.claims.get(login, 0)}</b>")
        if points.wants_streak(login):
            if ch is None:
                now_part = "ждём эфир"
            elif points.counted(ch):
                now_part = "эта трансляция засчитана ✅"
            else:
                now_part = f"эта трансляция: {points.broadcast_seconds(login) // 60}/{STREAK_MIN_SEC // 60} мин"
            body.append(f"🔥 Серия: <b>{points.streak.get(login, 0)}</b> ({now_part})")
        body.append(f"⏱ Отправлено: <b>{points.minutes_sent(login)} мин</b>")
        lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")

    if points.checked_at:
        lines += ["", f"🕒 <i>Проверено {int(time.time() - points.checked_at)} сек назад</i>"]
    if points.last_error:
        lines.append(f"⚠️ <i>{esc(points.last_error)}</i>")
    lines += ["", "<i>Серия считается по данным бота (по трансляциям). Настоящее число видно в чате канала на Twitch.</i>"]
    return "\n".join(lines)


def points_markup(points) -> dict:
    rows = [
        [(f"{login} · {MODE_ICON[points.mode_of(login)]}", f"pts_mode:{login}", MODE_COLOR[points.mode_of(login)])]
        for login in points.channels
    ]
    rows += [
        [("🔕 Выключить", "pts_toggle", RED) if points.enabled else ("🔔 Включить", "pts_toggle", GREEN)],
        [("➕ Добавить канал", "pts_add", GREEN), ("➖ Убрать канал", "pts_rm", RED)],
        back_row(),
    ]
    return kb(*rows)


def remove_text(points) -> str:
    if not points.channels:
        return "<b>➖ УБРАТЬ КАНАЛ</b>\n\nСписок пуст."
    return "<b>➖ УБРАТЬ КАНАЛ</b>\n\nВыбери канал:"


def remove_markup(points) -> dict:
    rows = [[(f"❌ {login}", f"prm:{login}", RED)] for login in points.channels]
    rows.append([("⬅️ Назад", "pts")])
    return kb(*rows)


def settings_text(miner, tw, remote_store: bool) -> str:
    acct = f"@{tw.login} (клиент: {tw.client_name})" if tw.login else "вход не выполнен"
    return (
        "<b>⚙️ НАСТРОЙКИ</b>\n\n"
        f"┌ Аккаунт: <code>{esc(acct)}</code>\n"
        f"├ Токен хранится: <i>{'в Supabase' if remote_store else 'локально (пропадёт при перезапуске)'}</i>\n"
        f"└ Часовой пояс панели: <i>{esc(str(TZ))}</i>"
    )


def settings_markup() -> dict:
    return kb([("🔑 Войти / сменить токен", "login", BLUE)], back_row())


# ---- viewership badges (watch a category, no Drops campaign needed)

# slug -> label; anything else the user adds is shown by its slug
BADGE_PRESETS = {
    "grand-theft-auto-vi": "🎮 GTA VI · Bling Badge",
}
CATEGORY_NAMES = {"grand-theft-auto-vi": "Grand Theft Auto VI"}
SHORT_TITLES = {"grand-theft-auto-vi": "GTA VI"}


def _game_label(slug: str) -> str:
    return BADGE_PRESETS.get(slug, f"📺 {slug}")


def badges_text(miner) -> str:
    head = "🎖 <b>ЗНАЧКИ ЗА ПРОСМОТР</b>\n━━━━━━━━━━━━━━━━━━━━"
    lines = [
        head, "",
        "<i>Бот сам находит стримера в выбранной категории и смотрит его, "
        "пока не наберётся норма текущего дня.</i>",
        "<i>Нажми на категорию, чтобы открыть меню прокачки значка.</i>",
    ]
    for slug in dict.fromkeys(list(BADGE_PRESETS) + miner.games):
        on = slug in miner.games
        lim = miner.day_limit(slug)
        extra = f" · {min(miner.days_done.get(slug, 0), lim)}/{lim}" if (on and lim) else ""
        if on and miner.game_finished(slug):
            extra += " 🏁"
        lines.append("")
        lines.append(f"{'🟢' if on else '⚫'} <b>{esc(_game_label(slug))}</b> — {on_off(on)}{extra}")
    if miner.paused:
        lines += ["", "⚠️ <i>Автосбор поставлен на паузу — значки не собираются.</i>"]
    return "\n".join(lines)


def badges_markup(miner) -> dict:
    from miner import MAX_GAMES
    rows = []
    for slug in dict.fromkeys(list(BADGE_PRESETS) + miner.games):
        on = slug in miner.games
        label = f"{'✅' if on else '⬜'} {_game_label(slug)[2:].strip()}"
        rows.append([(label, f"bdg_s:{slug}", GREEN if on else BLUE)])
    if len(miner.games) < MAX_GAMES:
        rows.append([("➕ Своя категория", "bdg_add", BLUE)])
    rows.append([("🔄 Обновить", "bdg"), ("⬅️ Назад", "menu")])
    return kb(*rows)


def _stage_no(miner, slug: str) -> int:
    lim = miner.day_limit(slug)
    n = miner.days_done.get(slug, 0) + 1
    return min(n, lim) if lim else n


def _chain_line(miner, slug: str) -> str:
    names = miner.chain(slug)
    done = miner.days_done.get(slug, 0)
    parts = []
    for i, name in enumerate(names):
        if i < done:
            parts.append(f"✅ {esc(name)}")
        elif i == done:
            parts.append(f"🔸 <b>{esc(name)}</b>")
        else:
            parts.append(esc(name))
    return " → ".join(parts)


def _reset_clock(sched) -> str:
    """Daily reset time (UTC in the schedule) shown in the panel time zone."""
    h, m = sched["reset"]
    return datetime(2026, 1, 1, h, m, tzinfo=timezone.utc).astimezone(TZ).strftime("%H:%M")


def evolution_text(miner, tw, slug: str) -> str:
    from miner import BADGE_SCHEDULES, promo_state
    acct = f"@{tw.login}" if tw.login else "вход не выполнен"
    title = SHORT_TITLES.get(slug, CATEGORY_NAMES.get(slug, slug))
    on = slug in miner.games
    norm = miner.daily_norm(slug) // 60
    lim = miner.day_limit(slug)
    sched = BADGE_SCHEDULES.get(slug)
    state = promo_state(slug)
    lines = [f"🎮 <b>{esc(title)}: ПРОКАЧКА ЗНАЧКА</b>", f"<i>Аккаунт: {esc(acct)}</i>", ""]
    if miner.chain(slug):
        lines += ["<b>⛓ ЦЕПОЧКА ПРОКАЧКИ</b>", _chain_line(miner, slug), ""]
    lines += [
        "<b>📋 УСЛОВИЯ ПРОКАЧКИ</b>",
        f"┌ <b>Категория:</b> {esc(CATEGORY_NAMES.get(slug, slug))}",
        f"├ <b>Автопрокачка:</b> {'включена' if on else 'выключена'}",
        f"└ <b>Норма:</b> {norm} минут за один день",
    ]
    if sched:
        lines.append(f"   <i>Новый день начинается в {_reset_clock(sched)} ({esc(str(TZ))})</i>")
        lines.append("")
        lines.append("<b>🗓 АКЦИЯ</b>")
        lines.append(f"┌ Старт: <b>{sched['start'].astimezone(TZ):%d.%m в %H:%M}</b>")
        lines.append(f"└ Финиш: <b>{sched['end'].astimezone(TZ):%d.%m в %H:%M}</b>")
    lines += ["", "<b>📈 ПРОГРЕСС ПРОКАЧКИ</b>"]
    day = _stage_no(miner, slug)
    day_txt = f"День {day}/{lim}" if lim else f"День {day}"
    if state == "before":
        lines += [
            f"└ {day_txt}: ⏳ <b>значки ещё не выдаются</b>",
            f"<i>Первая стадия откроется {sched['start'].astimezone(TZ):%d.%m в %H:%M} "
            f"({esc(str(TZ))}).</i>",
        ]
    elif state == "over":
        lines += [
            f"└ {day_txt}: 🏁 <b>акция завершена</b>",
            "<i>Новые стадии больше не выдаются.</i>",
        ]
    elif miner.game_finished(slug):
        lines += [f"└ 🏁 <b>Все {lim} стадий пройдены</b>", "<i>Бот больше не смотрит эту категорию.</i>"]
    elif miner.game_day_counted(slug):
        nxt = miner.next_available(slug)
        if nxt:
            lines += [
                f"└ {day_txt}: ✅ <b>значок за этот день получен</b>",
                f"<i>Следующая стадия откроется {nxt.astimezone(TZ):%d.%m в %H:%M} "
                f"({esc(str(TZ))}) — в {_reset_clock(sched) if sched else '00:00'}.</i>",
            ]
        else:
            lines += [
                f"└ {day_txt}: ✅ <b>значок за этот день получен</b>",
                "<i>Новых дней в акции больше нет.</i>",
            ]
    else:
        done = miner.game_today(slug)
        lines.append(f"└ {day_txt}: ⏱ <b>{min(done, norm)}/{norm} мин</b>")
        ch = miner.game_channel.get(slug)
        if not on:
            lines.append("<i>Автопрокачка выключена — бот не смотрит. Включи её кнопкой ниже.</i>")
        elif ch:
            lines.append(f"<i>📡 Смотрю: {esc(ch)}</i>")
        else:
            lines.append("<i>⏳ Ожидание стримера-участника акции (с включёнными Drops).</i>")
    if sched and sched.get("final"):
        lines += ["", f"🏆 <i>Финальная награда: {esc(sched['final'])}. {esc(sched.get('final_note', ''))}</i>"]
    if miner.paused:
        lines += ["", "⚠️ <i>Автосбор поставлен на паузу — значки не собираются.</i>"]
    return "\n".join(lines)


def evolution_markup(miner, slug: str) -> dict:
    from miner import promo_state
    on = slug in miner.games
    norm = miner.daily_norm(slug) // 60
    day = _stage_no(miner, slug)
    state = promo_state(slug)
    if state == "before":
        status = "⏳ Акция ещё не началась"
    elif state == "over":
        status = "🏁 Акция завершена"
    elif miner.game_finished(slug):
        status = "🏁 Все стадии пройдены"
    elif miner.game_day_counted(slug):
        nxt = miner.next_available(slug)
        status = (f"🕒 Следующий значок {nxt.astimezone(TZ):%d.%m в %H:%M}" if nxt
                  else "✅ Значок получен — новых дней нет")
    else:
        status = f"⏱ День {day}: {min(miner.game_today(slug), norm)}/{norm} мин"
    rows = [[(status, f"bdg_s:{slug}")]]
    if state == "running" and on and not miner.game_finished(slug) and not miner.game_day_counted(slug):
        rows.append([("✔️ Значок за сегодня уже получен(а)", f"bdg_day:{slug}")])
    rows.append([("⏻ Автопрокачка: Вкл", f"bdg_t:{slug}", GREEN) if on else ("⏻ Автопрокачка: Выкл", f"bdg_t:{slug}", RED)])
    rows.append([("🔄 Обновить", f"bdg_s:{slug}"), ("⬅️ Назад к значкам", "bdg")])
    return kb(*rows)
