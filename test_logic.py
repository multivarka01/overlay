"""Offline tests: parsing, selection, full mining cycle against a fake Twitch.
Run: python test_logic.py
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import miner as miner_mod
from miner import Miner, Settings
from twitch_client import (
    Twitch, parse_campaign, slugify, _looks_like_error, Channel,
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

    # non-badge campaign needs account linking
    c2 = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")]), {})
    assert not c2.eligible
    c3 = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")], linked=True), {})
    assert c3.eligible

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


def test_deadline_priority():
    # A: ends in 3h, needs 60 min. B: ends in 10 days, needs only 30 min.
    a = parse_campaign(raw_campaign([raw_drop(0, end=NOW + timedelta(hours=3))]), {})
    b = parse_campaign(raw_campaign([raw_drop(0, minutes=30)]), {})
    b.id = "camp2"
    m = Miner(Twitch(), None, Settings("badges"))
    assert m.pick([b, a])[0] is a, "must prefer the one that can run out of time"


async def test_deadline_warning():
    sent = []

    async def notify(t):
        sent.append(t)

    late = parse_campaign(raw_campaign([raw_drop(0, end=NOW + timedelta(minutes=20))]), {})
    m = Miner(Twitch(), notify, Settings("badges"))
    await m._warn_deadlines([late])
    await m._warn_deadlines([late])          # only once
    assert len(sent) == 1 and "Не успею" in sent[0]


def test_pick_modes():
    badge = parse_campaign(raw_campaign([raw_drop(0)]), {})
    game = parse_campaign(raw_campaign([raw_drop(0, kind="DIRECT_ENTITLEMENT")], linked=True), {})
    game.id = "camp2"
    m = Miner(Twitch(), None, Settings("badges"))
    assert m.pick([game]) is None
    assert m.pick([game, badge])[0] is badge
    m.settings = Settings("all")
    assert m.pick([game]) is not None
    m.settings = Settings("badges", ["grand theft"])
    assert m.pick([game]) is not None


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

    m = Miner(tw, notify, Settings("badges"))
    for _ in range(12):          # enough cycles to finish all 3 chained drops
        await asyncio.wait_for(m._cycle(), 5)
    claimed = [s for s in sent if s.startswith("🏅")]
    assert len(claimed) == 3, sent
    assert "Chain 0" in claimed[0] and "Chain 2" in claimed[2]
    assert any(s.startswith("▶️") for s in sent)
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

    m = Miner(tw, notify, Settings("badges"))
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

    m = Miner(tw, notify, Settings("badges"))

    async def fake_watch(camp, drop, ch):
        seen.append(camp.id)

    m._watch = fake_watch
    await m._cycle()
    assert seen == ["campB"], seen
    assert m._no_streams["campA"] > 0


def test_campaigns_hide_finished():
    done_raw = raw_campaign([raw_drop(0, claimed=True)])
    done_raw["name"] = "AlreadyDone"
    open_raw = raw_campaign([raw_drop(0)])
    open_raw["name"] = "StillOpen"
    m = Miner(Twitch(), None, Settings("badges"))
    m.campaigns = [parse_campaign(done_raw, {}), parse_campaign(open_raw, {})]
    text = m.campaigns_text()
    assert "StillOpen" in text and "AlreadyDone" not in text, text


async def test_telegram_commands():
    import app as app_mod
    app_mod.OWNER_ID = 42
    a = app_mod.App()
    outbox = []

    class FakeTG:
        async def send(self, text):
            outbox.append(text)

        async def call(self, method, **kw):
            outbox.append(f"<{method}>")

    class FakeStore:
        data = {}

        async def set(self, k, v):
            self.data[k] = v

        async def get(self, k):
            return self.data.get(k)

    a.tg, a.store = FakeTG(), FakeStore()
    a.miner = Miner(a.tw, a.tg.send, a.settings)
    await a.handle("/pause", 1)
    assert a.miner.paused
    await a.handle("/resume", 2)
    assert not a.miner.paused
    await a.handle("/mode all", 3)
    assert a.settings.mode == "all" and a.store.data["mode"] == "all"
    await a.handle("/games Grand Theft Auto, Valorant", 4)
    assert a.settings.extra_games == ["grand theft auto", "valorant"]
    await a.handle("/status", 5)
    assert "Состояние" in outbox[-1]
    await a.handle("/token", 6)            # deletes the message, asks for the value
    assert "<deleteMessage>" in outbox


def main():
    test_parse_and_selection()
    test_misc()
    test_pick_modes()
    test_deadline_priority()
    asyncio.run(test_deadline_warning())
    asyncio.run(test_full_cycle())
    asyncio.run(test_sticky_channel())
    asyncio.run(test_skip_campaign_without_streams())
    test_campaigns_hide_finished()
    asyncio.run(test_telegram_commands())
    print("all tests passed")


if __name__ == "__main__":
    main()
