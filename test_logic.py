"""Offline tests: parsing, selection, full mining cycle against a fake Twitch.
Run: python test_logic.py
"""
import asyncio
import time
import json
import copy
from datetime import datetime, timedelta, timezone

import miner as miner_mod
from miner import Miner
from points import PointsFarmer, normalize_login, MAX_CHANNELS
from twitch_client import (
    Twitch, parse_campaign, slugify, _looks_like_error, _error_note, Channel,
)

NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_drop(i, *, chain=True, start=None, end=None, minutes=60, kind="BADGE", claimed=None):
    d = {
        "id": f"drop{i}", "name": f"Day {i}",
        "startAt": iso(start or NOW - timedelta(days=1)),
        "endAt": iso(end or NOW + timedelta(days=10)),
        "requiredMinutesWatched": minutes,
        "benefitEdges": [{"benefit": {
            "id": f"ben{i}", "name": f"Chain {i}", "distributionType": kind, "imageAssetURL": "x"}}],
        "preconditionDrops": [{"id": f"drop{i-1}"}] if (chain and i > 0) else None,
    }
    if claimed is not None:
        d["self"] = {"currentMinutesWatched": 0, "dropInstanceID": None, "isClaimed": claimed}
    return d


def raw_campaign(drops, *, game_id="999", linked=False, acl=None):
    return {
        "id": "camp1", "name": "GTAVI Launch Celebration", "status": "ACTIVE",
        "startAt": iso(NOW - timedelta(days=1)), "endAt": iso(NOW + timedelta(days=12)),
        "game": {"id": game_id, "displayName": "Grand Theft Auto VI", "slug": "grand-theft-auto-vi"},
        "allow": {"channels": acl, "isEnabled": True},
        "self": {"isAccountConnected": linked},
        "timeBasedDrops": drops,
    }


# ----------------------------------------------------------------------------- unit


def test_parse_and_selection():
    camp = parse_campaign(raw_campaign([raw_drop(i) for i in range(3)]), {})
    assert camp.badge_like and camp.eligible and camp.active
    assert [d.id for d in camp.earnable()] == ["drop0"], "chain: only first drop is earnable"
    camp.drops["drop0"].claimed = True
    assert [d.id for d in camp.earnable()] == ["drop1"]

    # in-game item campaigns are not wanted at all, linked or not
    c2 = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")]), {})
    assert not c2.eligible
    c3 = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")], linked=True), {})
    assert not c3.eligible

    # drop window not open yet -> not earnable
    c4 = parse_campaign(raw_campaign([raw_drop(0, start=NOW + timedelta(hours=5))]), {})
    assert c4.earnable() == []

    # claimed via claimed_benefits when there is no "self" edge
    d = raw_drop(0, start=NOW - timedelta(days=2), end=NOW + timedelta(days=2))
    c5 = parse_campaign(raw_campaign([d]), {"ben0": NOW - timedelta(hours=1)})
    assert c5.drops["drop0"].claimed

    # ACL
    c6 = parse_campaign(raw_campaign([raw_drop(0)], acl=[{"id": "5", "name": "streamer"}]), {})
    assert c6.channels == [(5, "streamer")]


def test_misc():
    assert slugify("Grand Theft Auto VI") == "grand-theft-auto-vi"
    assert slugify("Tom Clancy's Rainbow Six") == "tom-clancys-rainbow-six"
    assert _looks_like_error('[{"error":"Not Found","status":404}]')
    assert not _looks_like_error("#EXTM3U\nhttps://x/y.m3u8")
    # usher blocks datacenter/VPN IPs; the note must say why instead of dumping the URL
    usher_403 = (
        '[{"url":"/api/channel/hls/x.m3u8?sig=abc\\u0026token=%7B%7D",'
        '"error":"Content Restricted In Region","error_code":"anonymizer_blocked",'
        '"type":"error"}]'
    )
    assert _looks_like_error(usher_403)
    assert _error_note(usher_403) == "Content Restricted In Region (anonymizer_blocked)"


def test_deadline_priority():
    # A: ends in 3h, needs 60 min. B: ends in 10 days, needs only 30 min.
    a = parse_campaign(raw_campaign([raw_drop(0, end=NOW + timedelta(hours=3))]), {})
    b = parse_campaign(raw_campaign([raw_drop(0, minutes=30)]), {})
    b.id = "camp2"
    m = Miner(Twitch(), None)
    assert m.pick([b, a])[0] is a, "must prefer the one that can run out of time"


async def test_new_badge_announcement():
    sent, badges = [], []

    async def notify(t):
        sent.append(t)

    async def on_new(badge, game):
        badges.append((badge, game))

    m = Miner(Twitch(), notify)
    m.on_new_badge = on_new
    old = parse_campaign(raw_campaign([raw_drop(0)]), {})
    await m._announce_new([old])                         # first cycle: silent
    raw_new = raw_campaign([raw_drop(0)])
    raw_new["id"] = "new1"
    new = parse_campaign(raw_new, {})
    await m._announce_new([old, new])
    await m._announce_new([old, new])                    # only once
    assert len(badges) == 1 and badges[0][0] == "Chain 0" and not sent


async def test_announce_badge_message():
    import app as app_mod
    a = app_mod.App()
    out = []

    class TG:
        async def send(self, text, markup=None, html=False):
            out.append((text, markup))

    a.tg = TG()
    await a.announce_badge("RuneScape <Bones>", "Old School RuneScape")
    text, markup = out[0]
    assert "Вышел новый значок Twitch!" in text and "RuneScape &lt;Bones&gt;" in text and "[Old School RuneScape]" in text
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == ["menu", "inv"]


def test_badges_only():
    badge = parse_campaign(raw_campaign([raw_drop(0)]), {})
    game = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")], linked=True), {})
    emote = parse_campaign(raw_campaign([raw_drop(0, kind="EMOTE")]), {})
    game.id, emote.id = "camp2", "camp3"
    m = Miner(Twitch(), None)
    assert m.pick([game, emote]) is None, "game items and emotes are ignored"
    assert m.pick([game, emote, badge])[0] is badge


def test_chain_prerequisite_is_mined():
    # drop0: in-game item, drop1 (badge) requires drop0 -> drop0 must be mined first
    item = raw_drop(0, kind="DIRECT_ENTITLEMENT")
    badge = raw_drop(1)
    camp = parse_campaign(raw_campaign([item, badge]), {})
    assert [d.id for d in camp.earnable()] == ["drop0"]
    camp.drops["drop0"].claimed = True
    assert [d.id for d in camp.earnable()] == ["drop1"]
    # an unrelated item drop in the same campaign is not mined
    other = raw_drop(5, kind="DIRECT_ENTITLEMENT", chain=False)
    camp2 = parse_campaign(raw_campaign([raw_drop(0), other]), {})
    assert [d.id for d in camp2.earnable()] == ["drop0"]


# ------------------------------------------------------------------ full cycle (fake)


class FakeTwitch:
    """Stands in for Twitch: serves raw campaign dicts through the real parser."""

    def __init__(self, raws):
        self.raws = raws
        self.token, self.login, self.user_id = "tok", "tester", 1
        self.client_name = "Android"
        self.watch_calls = 0
        self.progress = 0
        self.refreshed = 0

    async def fetch_campaigns(self):
        return [parse_campaign(copy.deepcopy(r), {}) for r in self.raws]

    async def find_channels(self, camp):
        return [Channel(1, "streamer", 10, camp.game_id, camp.game_name)]

    async def streams_info(self, logins):
        return [Channel(1, l, 10, 999, "Grand Theft Auto VI") for l in logins if l in self.online]

    online = {"streamer"}

    async def watch_once(self, ch):
        self.watch_calls += 1
        return True

    def _current_raw(self):
        for r in self.raws:
            for d in r["timeBasedDrops"]:
                if not (d.get("self") or {}).get("isClaimed"):
                    pre = [p["id"] for p in (d.get("preconditionDrops") or [])]
                    if all(any(x["id"] == p and (x.get("self") or {}).get("isClaimed")
                               for x in r["timeBasedDrops"]) for p in pre):
                        return d

    async def current_drop(self, ch):
        d = self._current_raw()
        self.progress += 30
        cur = min(self.progress, 60)
        d["self"] = {"currentMinutesWatched": cur, "isClaimed": False,
                     "dropInstanceID": "inst" if cur >= 60 else None}
        return {"dropID": d["id"], "currentMinutesWatched": cur}

    async def claim(self, drop):
        for r in self.raws:
            for d in r["timeBasedDrops"]:
                if d["id"] == drop.id:
                    d["self"] = {"currentMinutesWatched": 60, "isClaimed": True, "dropInstanceID": "inst"}
        self.progress = 0
        return True


async def test_full_cycle():
    miner_mod.WATCH_INTERVAL = 0.01
    miner_mod.PROGRESS_EVERY = 0
    miner_mod.IDLE_REFRESH = 0.01
    raw = raw_campaign([raw_drop(i) for i in range(3)])
    tw = FakeTwitch([raw])
    sent = []

    async def notify(text):
        sent.append(text)

    m = Miner(tw, notify)
    for _ in range(12):          # enough cycles to finish all 3 chained drops
        await asyncio.wait_for(m._cycle(), 5)
    claimed = [s for s in sent if s.startswith("🏅")]
    assert len(claimed) == 3, sent
    assert "Chain 0" in claimed[0] and "Chain 2" in claimed[2]
    assert not any(s.startswith("▶️") for s in sent)       # no "started mining" spam
    assert m.pick(await tw.fetch_campaigns()) is None, "everything claimed -> idle"
    assert "Последние награды" in m.status_text()
    assert m.campaigns_text() == "Подходящих кампаний сейчас нет.", "claimed campaign is hidden"


async def test_sticky_channel():
    miner_mod.WATCH_INTERVAL = 0.01
    miner_mod.PROGRESS_EVERY = 0
    raw = raw_campaign([raw_drop(0)])
    tw = FakeTwitch([raw])
    found = []
    orig = tw.find_channels

    async def counting(camp):
        found.append(1)
        return await orig(camp)

    tw.find_channels = counting

    async def notify(text):
        pass

    m = Miner(tw, notify)
    camp = parse_campaign(copy.deepcopy(raw), {})
    m.channel = Channel(1, "streamer", 10, 999, "Grand Theft Auto VI")
    assert (await m._current_channel_if_valid(camp)).login == "streamer"   # still live -> stay
    tw.online = set()
    assert await m._current_channel_if_valid(camp) is None                  # offline -> switch
    tw.online = {"streamer"}
    m._avoid["streamer"] = 10**12
    assert await m._current_channel_if_valid(camp) is None                  # stalled -> avoid
    m._avoid.clear()
    assert found == []


async def test_skip_campaign_without_streams():
    rawA = raw_campaign([raw_drop(0, end=NOW + timedelta(hours=1), minutes=15)])
    rawA["id"] = "campA"
    rawB = raw_campaign([raw_drop(0)])
    rawB["id"] = "campB"
    tw = FakeTwitch([rawA, rawB])
    orig = tw.find_channels

    async def fc(camp):
        return [] if camp.id == "campA" else await orig(camp)

    tw.find_channels = fc
    seen = []

    async def notify(t):
        pass

    m = Miner(tw, notify)

    async def fake_watch(camp, drop, ch):
        seen.append(camp.id)

    m._watch = fake_watch
    await m._cycle()
    assert seen == ["campB"], seen
    assert m._no_streams["campA"] > 0


def test_why_text():
    future = parse_campaign(raw_campaign([raw_drop(0, start=NOW + timedelta(hours=5)), raw_drop(1)]), {})
    m = Miner(Twitch(), None)
    m.campaigns = [future]
    text = m.why_text()
    assert "откроется" in text and "ждёт предыдущий дроп" in text, text


def test_sub_only_hidden():
    sub = raw_drop(0, minutes=0)
    sub["requiredSubs"] = 1
    raw = raw_campaign([sub])
    raw["name"] = "SubBadge"
    watch = raw_campaign([raw_drop(0)])
    watch["name"] = "WatchBadge"
    camp = parse_campaign(raw, {})
    assert camp.earnable() == [] and not camp.watchable
    m = Miner(Twitch(), None)
    m.campaigns = [camp, parse_campaign(watch, {})]
    text = m.campaigns_text()
    assert "WatchBadge" in text and "SubBadge" not in text and "Скрыто" in text, text
    assert "нужна подписка" in m.why_text()


def test_campaigns_hide_finished():
    done_raw = raw_campaign([raw_drop(0, claimed=True)])
    done_raw["name"] = "AlreadyDone"
    open_raw = raw_campaign([raw_drop(0)])
    open_raw["name"] = "StillOpen"
    m = Miner(Twitch(), None)
    m.campaigns = [parse_campaign(done_raw, {}), parse_campaign(open_raw, {})]
    text = m.campaigns_text()
    assert "StillOpen" in text and "AlreadyDone" not in text, text


async def test_leaves_ended_stream():
    miner_mod.WATCH_INTERVAL = 0.01
    miner_mod.PROGRESS_EVERY = 0
    raw = raw_campaign([raw_drop(0)])
    tw = FakeTwitch([raw])
    tw.online = set()                       # stream already ended, but stale chunks still "work"

    async def notify(t):
        pass

    m = Miner(tw, notify)
    camp = parse_campaign(copy.deepcopy(raw), {})
    ch = Channel(1, "streamer", 10, 999, "Grand Theft Auto VI")
    await asyncio.wait_for(m._watch(camp, camp.drops["drop0"], ch), 5)
    assert "закончил стрим" in m.state
    assert m._avoid["streamer"] > 0


async def test_stall_survives_refresh():
    miner_mod.WATCH_INTERVAL = 0.01
    miner_mod.PROGRESS_EVERY = 0
    raw = raw_campaign([raw_drop(0)])
    tw = FakeTwitch([raw])

    async def no_progress(ch):
        return None

    tw.current_drop = no_progress

    async def notify(t):
        pass

    m = Miner(tw, notify)
    camp = parse_campaign(copy.deepcopy(raw), {})
    drop = camp.drops["drop0"]
    ch = Channel(1, "streamer", 10, 999, "Grand Theft Auto VI")
    import time as _t
    m._gain[drop.id] = (0, _t.monotonic() - 1000)         # grew 1000 s ago, in an earlier cycle
    await asyncio.wait_for(m._watch(camp, drop, ch), 5)
    assert "стоит" in m.state and m._avoid["streamer"] > 0


async def test_manual_channels():
    miner_mod.WATCH_INTERVAL = 0.01
    miner_mod.PROGRESS_EVERY = 0
    raw = raw_campaign([raw_drop(0)], acl=[{"id": "5", "name": "localguy"}])
    tw = FakeTwitch([raw])
    tw.online = {"localguy", "other"}
    notes = []

    async def notify(text):
        notes.append(text)

    m = Miner(tw, notify)
    assert "Добавил localguy" in m.add_manual("localguy")
    assert "уже" in m.add_manual("localguy")
    for i in range(miner_mod.MAX_MANUAL):
        m.add_manual(f"c{i}")
    assert "Максимум" in m.add_manual("overflow")
    m.clear_manual()
    m.add_manual("localguy")
    camps = [parse_campaign(copy.deepcopy(raw), {})]
    ranked = m.rank(camps)

    watched = []

    async def fake_watch(camp, drop, ch):
        watched.append(ch.login)

    m._watch = fake_watch
    assert await m._try_manual(ranked) is True and watched == ["localguy"]   # fits the campaign ACL

    m.clear_manual()
    m.add_manual("other")                                                   # live, but not in the ACL
    pinned = []

    async def fake_pinned(ch):
        pinned.append(ch.login)

    m._watch_pinned = fake_pinned
    assert await m._try_manual(ranked) is True and watched == ["localguy"]   # no campaign -> plain watching
    assert pinned == ["other"]
    assert not notes

    tw.online = set()
    m.clear_manual()
    m.add_manual("localguy")
    assert await m._try_manual(ranked) is False                             # offline -> auto mode
    m._avoid["localguy"] = 10**12
    tw.online = {"localguy"}
    assert await m._try_manual(ranked) is False                             # stalled -> skipped
    assert "Убрал" in m.remove_manual("localguy") and not m.manual


async def test_game_watch():
    import miner as miner_mod
    from twitch_client import Channel
    tw = FakeTwitch([])
    calls = {"pts": 0}

    async def directory(slug, limit=30, drops_only=True):
        return [Channel(7, "gtaguy", 70, 1, "Grand Theft Auto VI")]

    async def watch_points(ch):
        calls["pts"] += 1
        return True

    tw.directory, tw.watch_points = directory, watch_points
    tw.online = {"gtaguy"}
    notes = []

    async def notify(t):
        notes.append(t)

    saved_now = miner_mod._now
    saved_sec = miner_mod.BADGE_SCHEDULES["grand-theft-auto-vi"]["daily_sec"]
    miner_mod._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # inside the promo
    miner_mod.BADGE_SCHEDULES["grand-theft-auto-vi"]["daily_sec"] = 180          # 3 minutes of watching
    try:
        m = miner_mod.Miner(tw, notify)
        assert m.game_slug("https://www.twitch.tv/directory/category/grand-theft-auto-vi?sort=x") == "grand-theft-auto-vi"
        assert m.game_slug("GTA6") == "grand-theft-auto-vi"
        assert "Добавил" in m.add_game("gta6") and "уже" in m.add_game("gta6")
        clock = {"t": 1000.0}                                  # virtual time: sleeping advances it
        real_mono = miner_mod.time.monotonic
        miner_mod.time.monotonic = lambda: clock["t"]

        async def vsleep(sec):
            clock["t"] += sec
            await asyncio.sleep(0)

        m._sleep = vsleep
        try:
            assert await m._try_games() is True
        finally:
            miner_mod.time.monotonic = real_mono
        assert calls["pts"] == 3 and "выполнена" in m.state
        assert not notes                                        # no "watching category" spam
        assert m.days_done == {"grand-theft-auto-vi": 1} and m.game_day_counted("grand-theft-auto-vi")
        assert await m._try_games() is False                  # norm done for this day
        assert "Убрал" in m.remove_game("gta6") and not m.games
    finally:
        miner_mod._now = saved_now
        miner_mod.BADGE_SCHEDULES["grand-theft-auto-vi"]["daily_sec"] = saved_sec


async def test_game_minutes_follow_real_time():
    """Wake-ups (button presses) must not credit extra minutes: 54 shown vs 34 real was this bug."""
    import miner as miner_mod
    from twitch_client import Channel
    tw = FakeTwitch([])
    pings = []

    async def watch_points(ch):
        pings.append(clock["t"])
        return True

    tw.watch_points = watch_points
    tw.online = {"gtaguy"}

    async def notify(t):
        pass

    saved_now = miner_mod._now
    miner_mod._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    clock = {"t": 1000.0}
    real_mono = miner_mod.time.monotonic
    miner_mod.time.monotonic = lambda: clock["t"]
    try:
        m = miner_mod.Miner(tw, notify)
        m.add_game("gta6")
        slug = "grand-theft-auto-vi"
        n = {"i": 0}

        async def noisy_sleep(sec):
            n["i"] += 1
            # every other sleep is cut short by a wake-up (panel button), real time passes only 5 s
            clock["t"] += 5 if n["i"] % 2 else sec
            await asyncio.sleep(0)

        m._sleep = noisy_sleep
        ch = Channel(7, "gtaguy", 70, 1, "Grand Theft Auto VI")
        start = clock["t"]
        stop = start + 600
        orig = tw.streams_info

        async def streams_info(logins, missing=None):
            if clock["t"] >= stop:
                m.games.clear()                                 # ends the loop after ~10 real minutes
            return await orig(logins, missing) if missing is not None else await orig(logins)

        tw.streams_info = streams_info
        await m._watch_pinned(ch, slug)
        real_min = (clock["t"] - start) / 60
        assert m._game_sec[slug] / 60 <= real_min + 1, (m._game_sec[slug], real_min)
    finally:
        miner_mod.time.monotonic = real_mono
        miner_mod._now = saved_now


async def test_set_progress():
    import miner as miner_mod
    saved_now = miner_mod._now
    miner_mod._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    try:
        async def notify(t):
            pass
        m = miner_mod.Miner(FakeTwitch([]), notify)
        slug = "grand-theft-auto-vi"
        m.days_done[slug] = 3
        m.done_at[slug] = miner_mod._now().timestamp()
        assert "значков 2" in await m.set_progress(slug, 2, 34)
        assert m.days_done[slug] == 2 and not m.game_day_counted(slug) and m.game_today(slug) == 34
        assert "от 0 до 6" in await m.set_progress(slug, 9)
    finally:
        miner_mod._now = saved_now


async def test_badges_menu():
    import app as app_mod
    import ui
    app_mod.OWNER_ID = 42
    a = app_mod.App()
    calls = {"sent": [], "edits": []}

    class FakeTG:
        async def send(self, text, markup=None, html=False):
            calls["sent"].append(text)
            return 1

        async def edit(self, mid, text, markup=None):
            calls["edits"].append((mid, text, markup))

        async def call(self, method, **kw):
            pass

    class FakeStore:
        data = {}
        remote = True

        async def set(self, k, v):
            self.data[k] = v

        async def get(self, k):
            return self.data.get(k)

    a.tg, a.store = FakeTG(), FakeStore()
    a.tw.login, a.tw.token = "tester", "tok"
    a.miner = Miner(a.tw, a.tg.send)
    a.points = PointsFarmer(a.tw, a.miner)
    a.miner.on_change = a.persist_days

    import miner as miner_mod
    saved_now = miner_mod._now
    miner_mod._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    try:
        await _badges_menu_scenario(a, ui, calls)
    finally:
        miner_mod._now = saved_now


async def _badges_menu_scenario(a, ui, calls):
    def cb(data):
        return {"id": "1", "from": {"id": 42}, "data": data, "message": {"message_id": 5}}

    assert any(b["callback_data"] == "bdg" for row in ui.main_markup(a.miner)["inline_keyboard"] for b in row)

    await a.handle_callback(cb("bdg"))
    _, text, markup = calls["edits"][-1]
    assert "ЗНАЧКИ ЗА ПРОСМОТР" in text and "⚫" in text and "ВЫКЛ" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "bdg_s:grand-theft-auto-vi"

    await a.handle_callback(cb("bdg_s:grand-theft-auto-vi"))             # evolution menu (off)
    _, text, markup = calls["edits"][-1]
    assert "GTA VI: ПРОКАЧКА ЗНАЧКА" in text and "@tester" in text and "Purple Chain" in text and "Miami Chain" in text
    assert "Автопрокачка:</b> выключена" in text and "День 1/6" in text
    assert "60 минут" in text and "16:00" in text and "Golden Iguana" in text
    assert markup["inline_keyboard"][-2][0]["text"] == "⏻ Автопрокачка: Выкл"

    await a.handle_callback(cb("bdg_t:grand-theft-auto-vi"))             # turn on
    assert a.miner.games == ["grand-theft-auto-vi"]
    assert a.store.data["watch_games"] == "grand-theft-auto-vi"
    a.miner._game_sec["grand-theft-auto-vi"] = 45 * 60
    a.miner.game_channel["grand-theft-auto-vi"] = "gta<guy>"
    text = ui.evolution_text(a.miner, a.tw, "grand-theft-auto-vi")
    assert "включена" in text and "45/60" in text and "gta&lt;guy&gt;" in text and "День 1/6" in text
    assert "✅" in ui.badges_markup(a.miner)["inline_keyboard"][0][0]["text"]
    await a.handle_callback(cb("bdg_day:grand-theft-auto-vi"))           # "already got today's badge"
    assert a.miner.days_done["grand-theft-auto-vi"] == 1 and "watch_days" in a.store.data
    _, text, markup = calls["edits"][-1]
    assert "✅ Purple Chain" in text and "🔸 <b>Bronze Chain" in text and "День 2/6" in text
    assert "значок за этот день получен" in text and "Следующая стадия откроется" in text
    assert markup["inline_keyboard"][0][0]["text"].startswith("🕒 Следующий значок")
    assert "1/6" in ui.badges_text(a.miner)
    assert a.miner.game_today("grand-theft-auto-vi") == 0 and await a.miner._try_games() is False
    a.miner.done_at["grand-theft-auto-vi"] -= 24 * 3600 + 1             # previous window
    assert not a.miner.game_day_counted("grand-theft-auto-vi")

    await a.handle_callback(cb("bdg_t:grand-theft-auto-vi"))             # turn off
    assert a.miner.games == [] and a.store.data["watch_games"] == ""

    await a.handle_callback(cb("bdg_add"))                               # custom category by text
    assert a.awaiting == ("game_add", 5)
    assert await a.handle_awaited("https://www.twitch.tv/directory/category/just-chatting", 77) is True
    assert a.miner.games == ["just-chatting"]
    assert "just-chatting" in ui.badges_text(a.miner)


async def test_badge_days():
    import miner as miner_mod
    tw = FakeTwitch([])
    notes, saved = [], []

    async def notify(t):
        notes.append(t)

    async def on_change():
        saved.append(1)

    m = miner_mod.Miner(tw, notify)
    m.on_change = on_change
    slug = "grand-theft-auto-vi"
    saved_now = miner_mod._now
    miner_mod._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    try:
        m.add_game("gta6")
        await m.mark_day(slug, manual=True)                      # user got today's badge himself
        assert m.days_done[slug] == 1 and m.game_day_counted(slug) and saved
        await m.mark_day(slug, manual=True)                      # same day twice -> still 1
        assert m.days_done[slug] == 1
        m.days_done[slug] = 5
        m.done_at[slug] = miner_mod._now().timestamp() - 25 * 3600  # stage done in a previous window
        assert not m.game_day_counted(slug)
        await m.mark_day(slug)
        assert m.game_finished(slug) and not notes
        assert await m._try_games() is False                      # finished -> no more watching
        m2 = miner_mod.Miner(tw, notify)
        m2.load_days(m.days_json())
        assert m2.days_done == m.days_done and m2.done_at == m.done_at
        m3 = miner_mod.Miner(tw, notify)                         # older save format (UTC date)
        m3.load_days('{"done": {"x": 2}, "marked": {"x": "2000-01-01"}}')
        assert m3.days_done == {"x": 2} and not m3.game_day_counted("x")
    finally:
        miner_mod._now = saved_now


def test_points_basics():
    assert normalize_login("https://www.twitch.tv/Shroud?x=1") == "shroud"
    assert normalize_login("@Some_User") == "some_user"
    assert normalize_login("a") is None and normalize_login("bad name") is None
    # links copied from the phone app / without a scheme must work too
    assert normalize_login("twitch.tv/shroud") == "shroud"
    assert normalize_login("www.twitch.tv/shroud") == "shroud"
    assert normalize_login("https://m.twitch.tv/shroud?tt_content=url") == "shroud"
    assert normalize_login("https://player.twitch.tv/?channel=shroud&parent=twitch.tv") == "shroud"
    assert normalize_login("https://www.twitch.tv/shroud/videos") == "shroud"
    assert normalize_login("  https://TWITCH.TV/Shroud/  ") == "shroud"
    assert normalize_login("https://www.twitch.tv/videos") is None, "not a channel, but a site page"
    assert normalize_login("https://twitch.tv/") is None and normalize_login("") is None
    pf = PointsFarmer(Twitch(), Miner(Twitch(), None), ["a_streamer", "A_STREAMER", "x"])
    assert pf.channels == ["a_streamer"]
    for i in range(MAX_CHANNELS + 2):
        pf.add(f"chan{i}x")
    assert len(pf.channels) == MAX_CHANNELS
    assert "Убрал" in pf.remove("chan0x") and "нет в списке" in pf.remove("nobody")


class PointsTW:
    """Fake Twitch for the points farmer: records pings and GQL calls."""
    token = "t"

    def __init__(self, live_logins=None, bc=100):
        self.live_logins = live_logins
        self.bc = bc
        self.pinged, self.claimed, self.gql_ops = [], [], []

    async def streams_info(self, logins, missing=None):
        if missing is not None and "typo_name" in logins:
            missing.add("typo_name")
        return [
            Channel(7, l, self.bc, 1, "Just Chatting") for l in logins
            if l != "typo_name" and (self.live_logins is None or l in self.live_logins)
        ]

    async def watch_points(self, ch):
        self.pinged.append(ch.login)
        return True

    def op(self, key, **vars):
        return {"op": key, "vars": vars}

    async def gql(self, payload):
        self.gql_ops.append(payload["op"])
        if payload["op"] == "ChannelPointsContext":
            return {"data": {"community": {"channel": {"self": {"communityPoints": {
                "balance": 1234, "availableClaim": {"id": "claim-1"}}}}}}}
        self.claimed.append(payload["vars"]["input"])
        return {"data": {"claimCommunityPoints": {"claim": None}}}


async def test_points_claim():
    tw = PointsTW(live_logins={"first_one", "second_one", "third_one"})
    pf = PointsFarmer(tw, Miner(Twitch(), None), ["first_one", "offline_guy", "second_one", "third_one"])
    await pf._round()
    assert tw.pinged == ["first_one", "second_one"], tw.pinged      # offline skipped, only 2 slots
    assert len(tw.claimed) == 3 and tw.claimed[0] == {"channelID": "7", "claimID": "claim-1"}
    assert pf.balance["third_one"] == 1234 and sum(pf.claims.values()) == 3
    assert pf.sent == {"first_one": 1, "second_one": 1}
    pf.sent["first_one"] = 6                                         # 6 rounds x 20 s = 2 minutes
    assert "first_one [Баллы + Серия]: в эфире" in pf.status_text() and "минут отправлено 2" in pf.status_text()
    assert "серия 0" in pf.status_text()

    # points/hour: +100 over 20 minutes = +300/h; short history shows nothing yet
    pf._marks["first_one"] = [(0.0, 1000), (1200.0, 1100)]
    assert 295 <= (pf.hourly_rate("first_one") or 0) <= 305
    pf._marks["second_one"] = [(0.0, 1000), (60.0, 1010)]
    assert pf.hourly_rate("second_one") is None
    assert "≈+300 баллов/ч" in pf.status_text()

    pf.add("typo_name")
    await pf._round()
    assert "typo_name" in pf.missing and "такого канала нет" in pf.status_text()
    assert "Проверено" in pf.status_text()


def test_streak_counts_broadcasts_not_time_segments():
    pf = PointsFarmer(PointsTW(), Miner(Twitch(), None), ["a_streamer"])
    ch = lambda bc: Channel(7, "a_streamer", bc, 1, "x")
    pf._observe(ch(100))
    for _ in range(29):                                  # 29 x 20 s = 9 min 40 s: not enough yet
        pf._credit(ch(100))
    assert pf.streak.get("a_streamer", 0) == 0 and not pf.counted(ch(100))
    pf._credit(ch(100))                                  # 10 minutes: the broadcast counts
    assert pf.streak["a_streamer"] == 1 and pf.counted(ch(100))
    for _ in range(100):                                 # more watching doesn't add to the same broadcast
        pf._credit(ch(100))
    assert pf.streak["a_streamer"] == 1

    pf._observe(ch(101))                                 # next broadcast: counts again after 10 minutes
    assert not pf.counted(ch(101)) and pf.streak["a_streamer"] == 1
    for _ in range(30):
        pf._credit(ch(101))
    assert pf.streak["a_streamer"] == 2

    pf._observe(ch(102))
    for _ in range(5):                                   # only 100 s of this one...
        pf._credit(ch(102))
    pf._observe(ch(103))                                 # ...then the next broadcast starts: streak broken
    assert pf.streak["a_streamer"] == 0

    # the counters survive a restart
    for _ in range(30):
        pf._credit(ch(103))
    saved = pf.export_state()
    pf2 = PointsFarmer(PointsTW(), Miner(Twitch(), None), ["a_streamer"])
    pf2.load_state(saved)
    assert pf2.streak == pf.streak and pf2.counted(ch(103)) and not pf2.counted(ch(104))
    pf2.load_state("garbage{")
    assert pf2.streak == {}


def test_modes_and_slot_scheduling():
    pf = PointsFarmer(PointsTW(), Miner(Twitch(), None), ["p_only", "s_only", "both_ch"])
    pf.set_mode("p_only", "points")
    pf.set_mode("s_only", "streak")
    pf.set_mode("both_ch", "both")
    pf.live = [Channel(i, l, 100 + i, 1, "x") for i, l in enumerate(["p_only", "s_only", "both_ch"], 1)]
    for ch in pf.live:
        pf._observe(ch)
    names = lambda: [c.login for c in pf.pick()]
    # channels that still need streak minutes go first, the points-only one waits for a free slot
    assert names() == ["s_only", "both_ch"]
    for _ in range(30):
        pf._credit(pf.live[1])                           # s_only is done: its slot is freed
    assert names() == ["both_ch", "p_only"]
    for _ in range(30):
        pf._credit(pf.live[2])                           # both_ch counted too, keeps farming points
    assert names() == ["p_only", "both_ch"]              # s_only (streak only, counted) is never watched again

    # modes: cycle, persistence, validation
    assert pf.cycle_mode("p_only") == "streak" and pf.cycle_mode("p_only") == "both" and pf.cycle_mode("p_only") == "points"
    assert pf.cycle_mode("nobody") is None
    assert "Режим" in pf.set_mode("s_only", "bogus") and "нет в списке" in pf.set_mode("nobody", "points")
    pf2 = PointsFarmer(PointsTW(), Miner(Twitch(), None), ["p_only", "s_only", "both_ch"])
    pf2.load_modes(pf.modes_json())
    assert pf2.modes == pf.modes
    pf2.load_modes("not json")
    assert pf2.modes == {} and pf2.mode_of("p_only") == "both"
    pf2.default_mode = "streak"
    assert pf2.mode_of("p_only") == "streak", "old global setting becomes the default"
    pf.remove("s_only")
    assert "s_only" not in pf.modes and "s_only" not in pf.streak


async def test_streak_only_skips_balance_and_chests():
    tw = PointsTW(live_logins={"s_only", "p_only"})
    pf = PointsFarmer(tw, Miner(Twitch(), None), ["s_only", "p_only"])
    pf.set_mode("s_only", "streak")
    pf.set_mode("p_only", "points")
    await pf._round()
    assert sorted(tw.pinged) == ["p_only", "s_only"]
    assert "s_only" not in pf.balance and pf.balance["p_only"] == 1234
    assert [c["channelID"] for c in tw.claimed] == ["7"] and len(tw.claimed) == 1, "only the points channel claims"


def test_points_ui_and_colors():
    import ui
    pf = PointsFarmer(PointsTW(), Miner(Twitch(), None), ["p_only", "s_only", "both_ch"])
    pf.set_mode("p_only", "points")
    pf.set_mode("s_only", "streak")
    pf.live = [Channel(i, l, 100 + i, 1, "x") for i, l in enumerate(["p_only", "s_only", "both_ch"], 1)]
    pf.balance["p_only"] = 4321
    pf.streak["s_only"] = 3
    html = ui.points_text(pf)
    assert "💰 Баланс: <b>4 321</b>" in html and html.count("🔥 Серия:") == 2     # s_only and both_ch
    assert "0/10 мин" in html and "Настоящее число" in html

    rows = ui.points_markup(pf)["inline_keyboard"]
    mode_buttons = {r[0]["callback_data"]: r[0] for r in rows[:3]}
    assert mode_buttons["pts_mode:p_only"]["style"] == "primary"
    assert mode_buttons["pts_mode:s_only"]["style"] == "danger"
    assert mode_buttons["pts_mode:both_ch"]["style"] == "success"
    assert "p_only" in mode_buttons["pts_mode:p_only"]["text"]
    toggle = [b for r in rows for b in r if b["callback_data"] == "pts_toggle"][0]
    assert toggle["style"] == "danger"                       # enabled -> the button turns it off
    pf.set_enabled(False)
    toggle = [b for r in ui.points_markup(pf)["inline_keyboard"] for b in r if b["callback_data"] == "pts_toggle"][0]
    assert toggle["style"] == "success"

    m = Miner(Twitch(), None)
    pause = ui.main_markup(m)["inline_keyboard"][0][0]
    assert pause["callback_data"] == "toggle" and pause["style"] == "danger"
    m.paused = True
    assert ui.main_markup(m)["inline_keyboard"][0][0]["style"] == "success"
    assert all(b.get("style") in (None, "primary", "success", "danger")
               for r in ui.main_markup(m)["inline_keyboard"] for b in r)
    assert "style" not in ui.kb([("plain", "x")])["inline_keyboard"][0][0]
    assert "style" not in ui.kb([("bad", "x", "purple")])["inline_keyboard"][0][0]


async def test_spade_event():
    import base64
    import json as _json
    from twitch_client import SPADE_PATTERN, SETTINGS_PATTERN

    assert SPADE_PATTERN.search('x"spade_url":"https://spade.twitch.tv/track"y').group(1) == "https://spade.twitch.tv/track"
    assert SETTINGS_PATTERN.search('<script src="https://static.twitchcdn.net/config/settings.abc123.js">')
    assert SETTINGS_PATTERN.search('<script src="https://assets.twitch.tv/config/settings.abc123.js">')

    sent = {}

    class Resp:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, url, data=None, headers=None):
            sent["url"], sent["data"], sent["headers"] = url, data, headers
            return Resp()

    tw = Twitch()
    tw.session, tw.user_id, tw._spade_url = FakeSession(), 5, "https://spade.example/x"
    assert await tw.watch_spade(Channel(7, "chan", 70, 99, "Some Game"))
    event = _json.loads(base64.b64decode(sent["data"]["data"]))[0]
    props = event["properties"]
    assert event["event"] == "minute-watched" and sent["url"] == "https://spade.example/x"
    assert props == {"channel_id": "7", "broadcast_id": "70", "player": "site", "user_id": 5, "live": True, "channel": "chan"}
    assert "Mozilla" in sent["headers"]["User-Agent"]
    # a refused event explains itself
    Resp.status = 403
    assert await tw.watch_spade(Channel(7, "chan", 70, 99, "g")) is False
    assert "HTTP 403" in tw.watch_note and tw._spade_url is None
    Resp.status = 204
    tw._spade_url = "https://spade.example/x"

    # the minute-watched event is what Twitch credits; the playlist is best-effort
    calls = []

    async def hls_fail(ch, ua=None, last_only=False):
        calls.append(("hls", last_only))
        return False

    async def spade_ok(ch):
        calls.append(("spade",))
        return True

    tw.watch_once, tw.watch_spade = hls_fail, spade_ok
    assert await tw.watch_points(Channel(7, "chan", 70, 99, "g")) is True
    assert calls == [("hls", True), ("spade",)], calls

    async def hls_ok(ch, ua=None, last_only=False):
        calls.append(("hls", last_only))
        return True

    calls.clear()
    tw.watch_once = hls_ok
    assert await tw.watch_points(Channel(7, "chan", 70, 99, "g")) is True
    assert calls == [("hls", True), ("spade",)], calls

    # a refused event still fails the attempt and keeps its reason
    async def spade_bad(ch):
        tw.watch_note = "событие просмотра: HTTP 403"
        return False

    calls.clear()
    tw.watch_spade = spade_bad
    assert await tw.watch_points(Channel(7, "chan", 70, 99, "g")) is False
    assert "HTTP 403" in tw.watch_note


async def test_telegram_commands():
    import app as app_mod
    app_mod.OWNER_ID = 42
    a = app_mod.App()
    outbox = []

    class FakeTG:
        async def send(self, text, markup=None, html=False):
            outbox.append(text)
            return 1

        async def edit(self, mid, text, markup=None):
            outbox.append(f"<edit {mid}>")

        async def call(self, method, **kw):
            outbox.append(f"<{method}>")

    class FakeStore:
        data = {}
        remote = False

        async def set(self, k, v):
            self.data[k] = v

        async def get(self, k):
            return self.data.get(k)

    a.tg, a.store = FakeTG(), FakeStore()
    a.miner = Miner(a.tw, a.tg.send)
    a.points = PointsFarmer(a.tw, a.miner)
    await a.handle("/pause", 1)
    assert a.miner.paused
    await a.handle("/resume", 2)
    assert not a.miner.paused
    await a.handle("/points add https://twitch.tv/Shroud", 3)
    assert a.points.channels == ["shroud"] and a.store.data["points_channels"] == "shroud"
    await a.handle("/points off", 4)
    assert not a.points.enabled and a.store.data["points_enabled"] == "0"
    await a.handle("/points", 5)
    assert "shroud" in outbox[-1]
    await a.handle("/status", 6)
    assert "Состояние" in outbox[-1] and "Баллы" in outbox[-1]
    await a.handle("/token", 7)            # deletes the message, asks for the value
    assert "<deleteMessage>" in outbox


async def test_panel_and_callbacks():
    import app as app_mod
    import ui
    app_mod.OWNER_ID = 42
    a = app_mod.App()
    calls = {"sent": [], "edits": [], "api": []}

    class FakeTG:
        async def send(self, text, markup=None, html=False):
            calls["sent"].append((text, markup))
            return 1

        async def edit(self, mid, text, markup=None):
            calls["edits"].append((mid, text, markup))

        async def call(self, method, **kw):
            calls["api"].append((method, kw))

    class FakeStore:
        data = {}
        remote = True

        async def set(self, k, v):
            self.data[k] = v

        async def get(self, k):
            return self.data.get(k)

    a.tg, a.store = FakeTG(), FakeStore()
    a.tw.login, a.tw.token = "tester", "tok"
    a.miner = Miner(a.tw, a.tg.send)
    a.points = PointsFarmer(a.tw, a.miner)

    # idle panel
    text = ui.panel_text(a.miner, a.points, a.tw)
    assert "@tester" in text and "Ожидание стримов" in text and "Получено значков: <b>0</b>" in text

    # panel while watching a badge
    camp = parse_campaign(raw_campaign([raw_drop(0)]), {})
    drop = camp.drops["drop0"]
    drop.current = 12
    a.miner.target = (camp, drop)
    a.miner.channel = Channel(1, "Streamer<b>", 10, 999, "Grand Theft Auto VI")
    a.miner.watching = True
    a.miner.stats.update(badges=3, watch_sec=4 * 3600 + 120)
    text = ui.panel_text(a.miner, a.points, a.tw)
    assert "Майнинг значка" in text and "Набрано 12/60 мин" in text and "Chain 0" in text
    assert "4ч 2м" in text and "Streamer&lt;b&gt;" in text, "dynamic text is HTML-escaped"

    # callbacks
    cq = lambda data: {"id": "1", "from": {"id": 42}, "data": data, "message": {"message_id": 7}}
    await a.handle_callback(cq("toggle"))
    assert a.miner.paused and "Запустить" in str(calls["edits"][-1][2])
    await a.handle_callback(cq("toggle"))
    assert not a.miner.paused
    await a.handle_callback(cq("pts_add"))
    assert a.awaiting == ("points_add", 7)
    assert await a.handle_awaited("https://twitch.tv/Shroud", 99) is True
    assert a.points.channels == ["shroud"] and a.store.data["points_channels"] == "shroud"
    assert "Добавил shroud" in calls["sent"][-1][0]
    # a phone link without "https://" is accepted from the panel prompt
    await a.handle_callback(cq("pts_add"))
    assert await a.handle_awaited("m.twitch.tv/second_guy", 98) is True
    assert a.points.channels == ["shroud", "second_guy"]
    # a bad value keeps the prompt open and reports the error in a visible message
    await a.handle_callback(cq("pts_add"))
    assert await a.handle_awaited("это не канал", 97) is True
    assert a.points.channels == ["shroud", "second_guy"]
    assert "Не похоже" in calls["sent"][-1][0] and a.awaiting == ("points_add", 7)
    assert await a.handle_awaited("https://www.twitch.tv/third_guy", 96) is True
    assert a.points.channels == ["shroud", "second_guy", "third_guy"] and a.awaiting is None
    text, markup = a.screen("pts")
    assert "БАЛЛЫ КАНАЛА" in text and "pts_mode:shroud" in str(markup)
    await a.handle_callback(cq("pts_mode:shroud"))          # both -> points -> streak -> both
    assert a.points.mode_of("shroud") == "points" and json.loads(a.store.data["points_modes"]) == {"shroud": "points"}
    await a.handle_callback(cq("pts_mode:shroud"))
    assert a.points.mode_of("shroud") == "streak"
    await a.handle_callback(cq("pts_mode:shroud"))
    assert a.points.mode_of("shroud") == "both"
    await a.handle("/points mode shroud streak", 3)
    assert a.points.mode_of("shroud") == "streak"
    await a.handle("/points mode shroud nonsense", 3)
    assert a.points.mode_of("shroud") == "streak" and "streak" in calls["sent"][-1][0]
    await a.handle_callback(cq("prm:shroud"))
    assert "shroud" not in a.points.channels and a.points.channels == ["second_guy", "third_guy"]
    await a.handle_callback(cq("inv"))
    assert "КАМПАНИИ" in calls["edits"][-1][1]
    await a.handle_callback({"id": "2", "from": {"id": 666}, "data": "toggle", "message": {"message_id": 7}})
    assert not a.miner.paused, "only the owner can press buttons"
    assert await a.handle_awaited("/status", 5) is False
    await a.handle("/menu", 1)
    assert calls["sent"] and "АККАУНТ" in calls["sent"][-1][0]


async def test_menu_commands_and_help():
    import app as app_mod
    app_mod.OWNER_ID = 42
    a = app_mod.App()
    api, sent = [], []

    class FakeTG:
        async def call(self, method, **kw):
            api.append((method, kw))

        async def send(self, text, markup=None, html=False):
            sent.append((text, markup))

    a.tg = FakeTG()
    a.store = type("S", (), {"remote": False})()
    a.miner = Miner(a.tw, a.tg.send)
    a.points = PointsFarmer(a.tw, a.miner)
    await a.setup_commands()
    names = [c["command"] for c in api[0][1]["commands"]]
    assert api[0][0] == "setMyCommands" and names[0] == "menu" and {"status", "points", "login", "help"} <= set(names)
    assert all(1 <= len(c["command"]) <= 32 and 3 <= len(c["description"]) <= 256 for c in api[0][1]["commands"])
    assert api[1] == ("setChatMenuButton", {"menu_button": {"type": "commands"}})
    await a.handle("/help", 1)
    assert "Помощь" in sent[-1][0] and "Команды:" not in sent[-1][0] and sent[-1][1]
    await a.handle("/something_unknown", 2)
    assert "АККАУНТ" in sent[-1][0], "unknown commands open the panel instead of a wall of text"


def main():
    test_parse_and_selection()
    test_misc()
    test_badges_only()
    test_chain_prerequisite_is_mined()
    test_deadline_priority()
    asyncio.run(test_new_badge_announcement())
    asyncio.run(test_announce_badge_message())
    asyncio.run(test_full_cycle())
    asyncio.run(test_sticky_channel())
    asyncio.run(test_leaves_ended_stream())
    asyncio.run(test_stall_survives_refresh())
    asyncio.run(test_skip_campaign_without_streams())
    asyncio.run(test_manual_channels())
    asyncio.run(test_game_watch())
    asyncio.run(test_game_minutes_follow_real_time())
    asyncio.run(test_set_progress())
    asyncio.run(test_badge_days())
    test_campaigns_hide_finished()
    test_why_text()
    test_sub_only_hidden()
    test_points_basics()
    asyncio.run(test_points_claim())
    test_streak_counts_broadcasts_not_time_segments()
    test_modes_and_slot_scheduling()
    asyncio.run(test_streak_only_skips_balance_and_chests())
    test_points_ui_and_colors()
    asyncio.run(test_spade_event())
    asyncio.run(test_telegram_commands())
    asyncio.run(test_panel_and_callbacks())
    asyncio.run(test_badges_menu())
    asyncio.run(test_menu_commands_and_help())
    print("all tests passed")


if __name__ == "__main__":
    main()
