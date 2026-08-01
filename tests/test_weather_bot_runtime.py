"""
Runtime tests that exercise the REAL weather_bot module (imported via the `wb`
fixture in conftest.py), rather than exec'ing source blocks.

Covers the parts that were previously untested because they need async
execution and/or the module's live wiring:
  * the alert state machine (_task_alerts): new / update / cancel / clear /
    suppress transitions, plus the "fetch failure must not clear" invariant
  * _http_get retry + circuit-breaker behaviour
  * seven pure helpers that had no direct tests

I/O boundaries (_channel, _send, _send_cleared, fetch_alerts, the aiohttp
session) are monkeypatched with fakes; the logic under test is the real thing.
"""
import asyncio
import time
from collections import defaultdict
from types import SimpleNamespace

import aiohttp
import pytest


def run(coro):
    """Drive a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def _fresh_circuit():
    return defaultdict(lambda: {"failures": 0, "until": 0.0})


# ============================================================================
# Pure helpers  (were untested — cheap to lock down)
# ============================================================================

class TestPureHelpers:
    def test_alert_tier(self, wb):
        assert wb._alert_tier("Tornado Warning") == 1
        assert wb._alert_tier("Flood Watch") == 2
        assert wb._alert_tier("Special Weather Statement") == 3
        assert wb._alert_tier("WINTER STORM WARNING") == 1  # case-insensitive

    def test_alert_is_suppressed_by_threshold(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "ALERT_SUPPRESS_TYPES", set())
        monkeypatch.setattr(wb, "ALERT_POST_THRESHOLD", "warning")
        assert wb._alert_is_suppressed("Special Weather Statement") is True
        assert wb._alert_is_suppressed("Tornado Warning") is False
        monkeypatch.setattr(wb, "ALERT_POST_THRESHOLD", "all")
        assert wb._alert_is_suppressed("Special Weather Statement") is False

    def test_alert_is_suppressed_by_type(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "ALERT_POST_THRESHOLD", "all")
        monkeypatch.setattr(wb, "ALERT_SUPPRESS_TYPES", {"Small Craft Advisory"})
        assert wb._alert_is_suppressed("Small Craft Advisory") is True
        assert wb._alert_is_suppressed("Tornado Warning") is False

    def test_storm_category(self, wb):
        assert "Tropical Depression" in wb._storm_category("TD", 30)
        assert "Tropical Storm" in wb._storm_category("TS", 60)
        assert "Category 1" in wb._storm_category("HU", 80)
        assert "Category 3" in wb._storm_category("HU", 120)
        assert "Category 5" in wb._storm_category("HU", 160)

    def test_wind_dir(self, wb):
        assert wb._wind_dir(0) == "N"
        assert wb._wind_dir(90) == "E"
        assert wb._wind_dir(180) == "S"
        assert wb._wind_dir(270) == "W"
        assert wb._wind_dir(360) == "N"

    def test_knots_to_mph(self, wb):
        assert wb._knots_to_mph(0) == 0
        assert wb._knots_to_mph(100) == 115
        assert wb._knots_to_mph("not-a-number") == 0

    def test_condition_emoji(self, wb):
        assert wb._condition_emoji("Thunderstorm") == "⛈️"
        assert wb._condition_emoji("Clear") == "☀️"
        assert wb._condition_emoji("Heavy Snow") == "❄️"
        assert wb._condition_emoji(None) == "🌤️"

    def test_pws_station_url(self, wb, monkeypatch):
        # queried station: derived by id under the /pws/ path, lower-cased
        assert (wb._pws_station_url("KNJMAYSL16")
                == "https://www.pwsweather.com/station/pws/knjmaysl16")
        # default station: derived when no override is configured
        monkeypatch.setattr(wb, "PWS_STATION_URL", None)
        assert wb._pws_station_url() == (
            f"https://www.pwsweather.com/station/pws/{wb.PWS_STATION_ID.lower()}")
        # default station: an explicit pws_station_url override wins
        monkeypatch.setattr(wb, "PWS_STATION_URL",
                            "https://www.pwsweather.com/station/pws/knjmaysl16")
        assert (wb._pws_station_url()
                == "https://www.pwsweather.com/station/pws/knjmaysl16")

    def test_barometric_tendency_collecting(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_state", {"pressure_history": []})
        assert "collecting" in wb._barometric_tendency()

    def test_barometric_tendency_rising(self, wb, monkeypatch):
        now = time.time()
        monkeypatch.setattr(wb, "_state", {"pressure_history": [
            {"ts": now - 3600, "v": 29.90},
            {"ts": now,        "v": 29.96}]})
        assert "rising" in wb._barometric_tendency()

    def test_barometric_tendency_falling(self, wb, monkeypatch):
        now = time.time()
        monkeypatch.setattr(wb, "_state", {"pressure_history": [
            {"ts": now - 3600, "v": 30.10},
            {"ts": now,        "v": 30.02}]})
        assert "falling" in wb._barometric_tendency()


# ============================================================================
# Circuit breaker
# ============================================================================

class TestCircuitBreaker:
    def test_opens_after_five_failures(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        for _ in range(5):
            wb._cb_failure("svc")
        assert wb._cb_ok("svc") is False   # backed off

    def test_stays_closed_below_threshold(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        for _ in range(4):
            wb._cb_failure("svc")
        assert wb._cb_ok("svc") is True    # 4 < 5, not yet backed off

    def test_success_resets(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        for _ in range(5):
            wb._cb_failure("svc")
        wb._cb_success("svc")
        assert wb._cb_ok("svc") is True
        assert wb._circuit["svc"]["failures"] == 0


# ============================================================================
# _http_get  (retry + circuit breaker)
# ============================================================================

class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"unexpected raise_for_status for {self.status}")

    async def json(self, content_type=None):
        return self._json


class _FakeGet:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Yields the next item per .get(); clamps to the last so a single item
    can back every retry attempt."""
    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        item = self._items[min(self.calls, len(self._items) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            return _FakeGet(exc=item)
        return _FakeGet(resp=item)


def _patch_session(wb, monkeypatch, session):
    async def _get():
        return session
    monkeypatch.setattr(wb, "_get_session", _get)


class TestHttpGet:
    def test_success_returns_json_and_resets_cb(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        sess = _FakeSession([_FakeResp(200, {"ok": 1})])
        _patch_session(wb, monkeypatch, sess)
        data = run(wb._http_get("http://x", service="svc", base_delay=0))
        assert data == {"ok": 1}
        assert sess.calls == 1
        assert wb._circuit["svc"]["failures"] == 0

    def test_retries_retryable_status_then_succeeds(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        sess = _FakeSession([_FakeResp(503), _FakeResp(200, {"ok": 2})])
        _patch_session(wb, monkeypatch, sess)
        data = run(wb._http_get("http://x", service="svc",
                                base_delay=0, retries=3))
        assert data == {"ok": 2}
        assert sess.calls == 2     # retried once

    def test_persistent_connection_error_raises_and_trips_cb(self, wb, monkeypatch):
        monkeypatch.setattr(wb, "_circuit", _fresh_circuit())
        sess = _FakeSession([aiohttp.ClientConnectionError("boom")])
        _patch_session(wb, monkeypatch, sess)
        with pytest.raises(aiohttp.ClientConnectionError):
            run(wb._http_get("http://x", service="svc",
                             base_delay=0, retries=3))
        assert sess.calls == 3     # exhausted all attempts
        assert wb._circuit["svc"]["failures"] >= 1

    def test_open_circuit_blocks_without_touching_network(self, wb, monkeypatch):
        circ = _fresh_circuit()
        circ["svc"] = {"failures": 10, "until": time.time() + 9999}
        monkeypatch.setattr(wb, "_circuit", circ)
        touched = {"n": 0}

        async def _get():
            touched["n"] += 1
            return _FakeSession([_FakeResp(200, {})])
        monkeypatch.setattr(wb, "_get_session", _get)
        with pytest.raises(RuntimeError):
            run(wb._http_get("http://x", service="svc"))
        assert touched["n"] == 0   # never opened a session


# ============================================================================
# Alert state machine  (_task_alerts) — the biggest previously-untested risk
# ============================================================================

def _feat(aid, event="Tornado Warning", ugc=("NJC001",), msg_type="Alert",
          refs=None, area="Atlantic, NJ"):
    """A minimal NWS alert feature with an in-coverage (default SNJ) UGC."""
    props = {"event": event, "messageType": msg_type,
             "geocode": {"UGC": list(ugc)}, "areaDesc": area,
             "headline": f"{event} for {area}"}
    if refs is not None:
        props["references"] = refs
    return {"id": aid, "properties": props}


@pytest.fixture
def alerts_env(wb, monkeypatch):
    """Wire _task_alerts to fakes and return handles to inspect what it did."""
    sends, clears, channel_sends = [], [], []

    class FakeMessage:
        def __init__(self, mid):
            self.id = mid

    class FakeChannel:
        def __init__(self):
            self._n = 5000

        async def send(self, **kw):
            self._n += 1
            channel_sends.append(kw)
            return FakeMessage(self._n)

        async def fetch_message(self, mid):
            return FakeMessage(int(mid))

    async def fake_send(embed_dict, reference=None, view=None):
        fake_send.n += 1
        sends.append(embed_dict)
        return FakeMessage(fake_send.n)
    fake_send.n = 9000

    async def fake_send_cleared(message_id, event, area, cancelled=False):
        clears.append({"message_id": message_id, "event": event,
                       "area": area, "cancelled": cancelled})

    state = {"posted_alerts": {}}
    monkeypatch.setattr(wb, "_state", state)
    monkeypatch.setattr(wb, "_channel", FakeChannel())
    monkeypatch.setattr(wb, "_send", fake_send)
    monkeypatch.setattr(wb, "_send_cleared", fake_send_cleared)
    monkeypatch.setattr(wb, "_alert_view", lambda feature: None)
    monkeypatch.setattr(wb, "save_state", lambda s: None)
    monkeypatch.setattr(wb, "_event", lambda *a, **k: None)
    monkeypatch.setattr(wb, "ALERT_POST_THRESHOLD", "all")
    monkeypatch.setattr(wb, "ALERT_SUPPRESS_TYPES", set())

    def set_alerts(features):
        async def _fetch(fast=False):
            return features
        monkeypatch.setattr(wb, "fetch_alerts", _fetch)

    return SimpleNamespace(state=state, sends=sends, clears=clears,
                           channel_sends=channel_sends, set_alerts=set_alerts)


def _preexisting(area="Atlantic, NJ", event="Tornado Warning", mid="1001"):
    return {"ts": time.time(), "message_id": mid, "event": event,
            "area": area, "cleared": False}


class TestTaskAlerts:
    def test_new_alert_posted_and_recorded(self, wb, alerts_env):
        alerts_env.set_alerts([_feat("A", "Tornado Warning")])
        any_new = run(wb._task_alerts())
        assert any_new is True
        assert len(alerts_env.sends) == 1
        assert "Tornado Warning" in alerts_env.sends[0]["title"]
        rec = alerts_env.state["posted_alerts"]["A"]
        assert rec["cleared"] is False and rec["message_id"] is not None

    def test_failed_send_not_recorded_so_it_retries(self, wb, alerts_env, monkeypatch):
        """A failed send must NOT be recorded as posted, or a later cycle would
        emit a phantom CLEARED for an alert nobody saw."""
        async def _fail(embed_dict, reference=None, view=None):
            return None
        monkeypatch.setattr(wb, "_send", _fail)
        alerts_env.set_alerts([_feat("A", "Tornado Warning")])
        run(wb._task_alerts())
        assert "A" not in alerts_env.state["posted_alerts"]   # unrecorded ⇒ retried
        assert alerts_env.clears == []                        # no phantom clear

    def test_below_threshold_alert_suppressed(self, wb, alerts_env, monkeypatch):
        monkeypatch.setattr(wb, "ALERT_POST_THRESHOLD", "warning")
        alerts_env.set_alerts([_feat("S", "Special Weather Statement")])
        run(wb._task_alerts())
        assert alerts_env.sends == []             # nothing posted to channel
        rec = alerts_env.state["posted_alerts"]["S"]
        assert rec.get("suppressed") is True and rec["message_id"] is None

    def test_cancel_clears_original(self, wb, alerts_env):
        alerts_env.state["posted_alerts"]["A"] = _preexisting()
        cancel = _feat("C", "Tornado Warning", msg_type="Cancel",
                       refs=[{"identifier": "A"}])
        alerts_env.set_alerts([cancel])
        run(wb._task_alerts())
        assert len(alerts_env.clears) == 1
        assert alerts_env.clears[0]["message_id"] == "1001"
        assert alerts_env.clears[0]["cancelled"] is True
        a = alerts_env.state["posted_alerts"]["A"]
        assert a["cleared"] is True and a["superseded_by"] == "C"

    def test_active_alert_cleared_when_gone(self, wb, alerts_env):
        alerts_env.state["posted_alerts"]["A"] = _preexisting(event="Flood Warning")
        alerts_env.set_alerts([])                 # A no longer active
        run(wb._task_alerts())
        assert len(alerts_env.clears) == 1
        assert alerts_env.clears[0]["message_id"] == "1001"
        assert alerts_env.clears[0]["cancelled"] is False
        assert alerts_env.state["posted_alerts"]["A"]["cleared"] is True

    def test_fetch_failure_does_not_clear(self, wb, alerts_env):
        """Invariant: an API outage (None) must never be read as 'no alerts'
        and trigger false CLEARED messages for live alerts."""
        alerts_env.state["posted_alerts"]["A"] = _preexisting()
        alerts_env.set_alerts(None)
        result = run(wb._task_alerts())
        assert result is False
        assert alerts_env.clears == []
        assert alerts_env.state["posted_alerts"]["A"]["cleared"] is False

    def test_update_replies_to_original(self, wb, alerts_env):
        alerts_env.state["posted_alerts"]["A"] = _preexisting()
        upd = _feat("B", "Tornado Warning", msg_type="Update",
                    refs=[{"identifier": "A"}])
        alerts_env.set_alerts([upd])
        any_new = run(wb._task_alerts())
        assert any_new is True
        assert len(alerts_env.channel_sends) == 1
        assert "reference" in alerts_env.channel_sends[0]   # threaded reply
        assert alerts_env.state["posted_alerts"]["A"]["superseded_by"] == "B"
        b = alerts_env.state["posted_alerts"]["B"]
        assert b["update_of"] == "A" and b["cleared"] is False

    def test_out_of_area_alert_ignored(self, wb, alerts_env):
        # NJC021 is Mercer — excluded from the default coverage
        alerts_env.set_alerts([_feat("X", "Tornado Warning", ugc=("NJC021",),
                                     area="Mercer, NJ")])
        run(wb._task_alerts())
        assert alerts_env.sends == []
        assert "X" not in alerts_env.state["posted_alerts"]

    def test_old_resolved_entries_pruned(self, wb, alerts_env):
        old = time.time() - 72 * 3600
        alerts_env.state["posted_alerts"]["OLD"] = {
            "ts": old, "message_id": "1", "event": "x", "area": "y",
            "cleared": True, "cleared_ts": old}
        alerts_env.set_alerts([_feat("NEW", "Tornado Warning")])
        run(wb._task_alerts())
        assert "OLD" not in alerts_env.state["posted_alerts"]   # pruned
        assert "NEW" in alerts_env.state["posted_alerts"]       # kept
