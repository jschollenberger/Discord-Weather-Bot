"""
Fixtures that make weather_bot importable for the runtime tests.

tests/test_weather_bot.py deliberately does NOT import weather_bot — it execs
self-contained source blocks, because importing the module loads config and can
sys.exit().  The runtime tests (test_weather_bot_runtime.py) need the real,
wired-up module to exercise the async paths (_task_alerts, _http_get) and the
circuit breaker, so this fixture imports it once against a throwaway data dir
and a minimal valid config (via the WEATHER_BOT_DIR env var).  Importing the
real module also lets pytest-cov measure these code paths.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

_MINIMAL_CONFIG = {
    "pws_station_id": "STN",
    "pws_client_id": "cid12345",
    "pws_client_secret": "sec12345",
    "discord_bot_token": "tok123456",
}


@pytest.fixture(scope="session")
def wb(tmp_path_factory):
    """Import weather_bot once, pointed at a temp data dir + minimal config."""
    data_dir = tmp_path_factory.mktemp("wb_data")
    (data_dir / "config.json").write_text(
        json.dumps(_MINIMAL_CONFIG), encoding="utf-8")
    os.environ["WEATHER_BOT_DIR"] = str(data_dir)
    os.environ.pop("WEATHER_BOT_CONFIG", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("weather_bot", None)
    return importlib.import_module("weather_bot")
