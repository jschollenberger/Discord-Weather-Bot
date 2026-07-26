#!/usr/bin/env python3
"""
Discord Weather Bot — a Discord bot for a configurable NWS coverage area.

Posts live conditions from a personal weather station, NWS alerts, tides, air
quality and Atlantic tropical storm updates to a Discord channel, and answers
slash commands for on-demand lookups.

Usage:
    python weather_bot.py [--data-dir DIR] [--config FILE]

Configuration lives in config.json — see config.example.*.json and README.md.
Release history is in CHANGELOG.md.

Copyright (C) 2026 Jason Schollenberger (KD2QED)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

__version__ = "3.1.0"
__author__  = "Jason Schollenberger KD2QED"
SOURCE_URL  = "https://github.com/jschollenberger/discord-weather-bot"

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

try:
    from astral import LocationInfo as _AstralLocation
    from astral.sun import sun as _astral_sun
    _ASTRAL_OK = True
except ImportError:
    _AstralLocation = _astral_sun = None
    _ASTRAL_OK = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# config.json, state.json and weather-bot.log all live in the data directory,
# which defaults to the directory holding this script.  Overriding it lets one
# checkout serve several instances (e.g. one guild per directory):
#
#   python weather_bot.py --data-dir ~/bots/atlantic
#   WEATHER_BOT_DIR=~/bots/atlantic python weather_bot.py
#
# Precedence: command line > environment > script directory.
def _resolve_paths() -> tuple[Path, Path]:
    ap = argparse.ArgumentParser(
        prog="weather_bot.py",
        description="Discord Weather Bot — a Discord bot for a "
                    "configurable NWS coverage area.")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="directory holding config.json, state.json and the "
                         "log file (default: alongside this script; "
                         "env: WEATHER_BOT_DIR)")
    ap.add_argument("--config", metavar="FILE",
                    help="path to the config file "
                         "(default: <data-dir>/config.json; "
                         "env: WEATHER_BOT_CONFIG)")
    # parse_known_args so an unrecognised flag never hard-fails startup
    args, _unknown = ap.parse_known_args()

    data_dir = Path(args.data_dir or os.environ.get("WEATHER_BOT_DIR")
                    or Path(__file__).parent).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"ERROR: could not create data directory {data_dir}: {e}")

    cfg_file = Path(args.config or os.environ.get("WEATHER_BOT_CONFIG")
                    or data_dir / "config.json").expanduser()
    return cfg_file, data_dir

CONFIG_FILE, BASE_DIR = _resolve_paths()
STATE_FILE  = BASE_DIR / "state.json"
LOG_FILE    = BASE_DIR / "weather-bot.log"

# ---------------------------------------------------------------------------
# Log rotation (keep last 2 runs)
# ---------------------------------------------------------------------------
def _rotate_logs():
    try:
        backup = Path(str(LOG_FILE) + ".1")
        if backup.exists():
            backup.unlink()
        if LOG_FILE.exists():
            LOG_FILE.rename(backup)
    except Exception:
        pass

_rotate_logs()

# ---------------------------------------------------------------------------
# Logging  (file = INFO+, console = ERROR only; notable events use _event())
# ---------------------------------------------------------------------------
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.INFO);  _fh.setFormatter(_fmt)
_ch  = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.ERROR); _ch.setFormatter(_fmt)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_fh)
logging.getLogger().addHandler(_ch)
log = logging.getLogger(__name__)

class _RedactFilter(logging.Filter):
    """
    Strip credentials from every log record.

    aiohttp puts the full request URL in its exception messages, and the
    Aeris/Xweather and AirNow endpoints carry credentials in the query string,
    so a plain `log.error(f"...: {e}")` on an HTTP failure would write
    client_secret / API_KEY into weather-bot.log in the clear.  Filtering at
    the handler level covers every call site, including discord.py's own
    logger, rather than relying on each one to remember.
    """
    _secrets: list[str] = []

    @classmethod
    def set_secrets(cls, *values):
        cls._secrets = sorted(
            {str(v) for v in values if v and len(str(v)) >= 8},
            key=len, reverse=True)   # longest first: avoid partial overlaps

    def _scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        self._scrub(a) if isinstance(a, str) else a
                        for a in record.args)
        return True

_redactor = _RedactFilter()
_fh.addFilter(_redactor)
_ch.addFilter(_redactor)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"ERROR: {CONFIG_FILE} not found.\n"
                 f"       Copy one of the config.example.*.json files to "
                 f"config.json and fill it in.")
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {CONFIG_FILE} is not valid JSON.\n"
                 f"       {e.msg} at line {e.lineno}, column {e.colno}.\n"
                 f"       (A trailing comma or missing quote is the usual cause.)")
    except OSError as e:
        sys.exit(f"ERROR: could not read {CONFIG_FILE}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"ERROR: {CONFIG_FILE} must contain a JSON object, "
                 f"got {type(cfg).__name__}.")
    return cfg

# Alert post threshold tiers.  Defined here (above _validate_config) so the
# validator can check the configured value against the valid set.
_THRESHOLD_TIER = {"warning": 1, "watch": 2, "all": 3}

def _validate_config(cfg: dict) -> list[str]:
    errs: list[str] = []
    for key in ("pws_station_id","pws_client_id","pws_client_secret","discord_bot_token"):
        val = cfg.get(key,"")
        if not val or str(val).startswith("YOUR_"):
            errs.append(f"  x {key}: missing or still a placeholder")
    for key in ("discord_guild_id","discord_channel_id"):
        val = cfg.get(key)
        if val is not None:
            try: int(str(val))
            except ValueError: errs.append(f"  x {key}: must be numeric, got {val!r}")
    lat, lon = cfg.get("forecast_lat"), cfg.get("forecast_lon")
    try:
        if lat is not None and not (-90 <= float(lat) <= 90):
            errs.append(f"  x forecast_lat: {lat} out of range")
    except (TypeError, ValueError):
        errs.append(f"  x forecast_lat: must be a number, got {lat!r}")
    try:
        if lon is not None and not (-180 <= float(lon) <= 180):
            errs.append(f"  x forecast_lon: {lon} out of range")
    except (TypeError, ValueError):
        errs.append(f"  x forecast_lon: must be a number, got {lon!r}")

    def _int_check(key: str, default: int, low: int, high: int, label: str):
        """Validate cfg[key] as an int in [low,high]; append a friendly error
        (never raise) on non-numeric input so one bad value in config.json
        can't crash startup with a raw traceback."""
        raw = cfg.get(key, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            errs.append(f"  x {key}: must be a whole number, got {raw!r}")
            return
        if not (low <= v <= high):
            errs.append(f"  x {key}: {label.format(v=v)}")

    _int_check("alert_interval_secs",    300, 60, 10**9, "minimum is 60")
    _int_check("conditions_update_mins", 30,  5,  10**9, "minimum is 5")
    _int_check("aqi_alert_threshold",    3,   1,  6,     "{v} must be 1-6")
    _int_check("weekly_summary_day",     6,   0,  6,     "must be 0 (Mon) to 6 (Sun)")
    _int_check("weekly_summary_hour",    8,   0,  23,    "must be 0-23")
    # location_name is interpolated into slash-command descriptions, which
    # Discord caps at 100 chars.  The longest template leaves ~50 chars.
    loc = cfg.get("location_name", "")
    if len(str(loc)) > 40:
        errs.append(f"  x location_name: keep under 40 characters "
                    f"(got {len(str(loc))}) — it is used in slash-command "
                    f"descriptions, which Discord caps at 100")

    # An unrecognised threshold silently fell back to "all" pre-v2.7.4, so a
    # typo like "Warning" or "warnings" would quietly post every advisory.
    thr = cfg.get("alert_post_threshold", "all")
    if thr not in _THRESHOLD_TIER:
        errs.append(f"  x alert_post_threshold: must be one of "
                    f"{', '.join(sorted(_THRESHOLD_TIER))} (got {thr!r})")

    # set("Small Craft Advisory") is a set of single CHARACTERS, which silently
    # matches nothing, so a bare string here must be rejected rather than used.
    sup = cfg.get("alert_suppress_types", [])
    if isinstance(sup, str):
        errs.append(f"  x alert_suppress_types: must be a list of event names "
                    f"— write [{sup!r}], not a bare string")
    elif not isinstance(sup, list):
        errs.append(f"  x alert_suppress_types: must be a list, "
                    f"got {type(sup).__name__}")
    elif not all(isinstance(x, str) for x in sup):
        errs.append("  x alert_suppress_types: every entry must be a string")

    # Radar station IDs are 4-letter ICAO-style codes (KDIX, KDOX, ...).
    # A malformed one would 404 on every /radar and silently drop the image.
    rad = cfg.get("radar_station", "KDIX")
    if not (isinstance(rad, str) and re.fullmatch(r"[A-Za-z]{4}", rad)):
        errs.append(f"  x radar_station: must be a 4-letter NWS radar code "
                    f"(e.g. KDIX), got {rad!r}")

    # An unrecognised value would silently fall through to global-only sync.
    csync = str(cfg.get("command_sync", "auto")).lower()
    if csync not in ("auto", "global", "guild"):
        errs.append(f"  x command_sync: must be auto, global, or guild "
                    f"(got {cfg.get('command_sync')!r})")

    # Presence fallback text: Discord caps activity names at 128 chars.
    pf = cfg.get("presence_fallback")
    if pf is not None and not (isinstance(pf, str) and 0 < len(pf) <= 120):
        errs.append("  x presence_fallback: must be a non-empty string under "
                    "120 characters (omit the key to use the default)")

    errs.extend(_validate_coverage(cfg.get("coverage")))
    return errs

def _config_warnings(cfg: dict) -> list[str]:
    """Non-fatal config observations: things that disable a feature or change
    behavior in a way the operator probably wants to know about, but which are
    perfectly valid.  Reported at startup; never block the bot from running."""
    warns: list[str] = []
    if not cfg.get("discord_channel_id"):
        warns.append("  ! discord_channel_id not set — will fall back to the "
                     "channel cached in state.json; on a fresh install the "
                     "bot will have nowhere to post")
    if not cfg.get("discord_guild_id"):
        warns.append("  ! discord_guild_id not set — slash commands sync "
                     "globally, which can take up to an hour to appear")
    if not cfg.get("airnow_api_key"):
        warns.append("  ! airnow_api_key not set — /aqi and AQI alerts disabled")
    if not cfg.get("coverage"):
        warns.append("  ! coverage not set — using the built-in 7-county "
                     "Southern NJ default")
    return warns

# UGC codes are 2-letter state + Z (forecast zone) or C (county) + 3 digits,
# e.g. NJZ022 / NJC001.  Validating the shape catches typos like "NJZ22" that
# would otherwise silently match nothing and quietly narrow coverage.
_UGC_RE = re.compile(r"^[A-Z]{2}[ZC]\d{3}$")

def _validate_coverage(cov) -> list[str]:
    """Validate the optional "coverage" config block.  Absent -> use the
    Southern NJ default.  Never raises; returns friendly error strings."""
    errs: list[str] = []
    if cov is None:
        return errs
    if not isinstance(cov, dict) or not cov:
        return ["  x coverage: must be a non-empty object of "
                "{\"County Name\": {\"county_code\": ..., \"zones\": [...]}}"]
    total_zones = 0
    for name, entry in cov.items():
        if not isinstance(entry, dict):
            errs.append(f"  x coverage[{name!r}]: must be an object")
            continue
        code = entry.get("county_code")
        if code is not None and not (isinstance(code, str) and _UGC_RE.match(code)):
            errs.append(f"  x coverage[{name!r}].county_code: "
                        f"not a valid UGC code, got {code!r}")
        elif isinstance(code, str) and code[2] != "C":
            errs.append(f"  x coverage[{name!r}].county_code: "
                        f"{code} is a zone code (expected a C code, e.g. NJC001)")
        zones = entry.get("zones", [])
        if not isinstance(zones, list):
            errs.append(f"  x coverage[{name!r}].zones: must be a list")
            continue
        total_zones += len(zones)
        for z in zones:
            if not (isinstance(z, str) and _UGC_RE.match(z)):
                errs.append(f"  x coverage[{name!r}].zones: "
                            f"not a valid UGC code, got {z!r}")
            elif z[2] != "Z":
                errs.append(f"  x coverage[{name!r}].zones: "
                            f"{z} is a county code (expected a Z code, e.g. NJZ022)")
    if not errs and not total_zones and not any(
            e.get("county_code") for e in cov.values() if isinstance(e, dict)):
        errs.append("  x coverage: no zones or county codes defined — "
                    "no alerts would ever match")
    return errs

_cfg = _load_config()

# Register credentials with the log redactor before anything can log them.
# discord_webhook is an optional URL whose last path segment is a token; aiohttp
# puts the full URL in its exception text, so it must be redacted too.
_RedactFilter.set_secrets(
    _cfg.get("pws_client_id"), _cfg.get("pws_client_secret"),
    _cfg.get("discord_bot_token"), _cfg.get("airnow_api_key"),
    _cfg.get("discord_webhook"))

_errs = _validate_config(_cfg)
if _errs:
    print("\nconfig.json validation failed:\n" + "\n".join(_errs) + "\n")
    sys.exit(1)

_warns = _config_warnings(_cfg)
if _warns:
    print("\nconfig.json warnings (not fatal):\n" + "\n".join(_warns) + "\n")
    for _w in _warns:
        log.warning(f"config: {_w.lstrip(' !')}")

PWS_STATION_ID          = _cfg["pws_station_id"].upper()
PWS_CLIENT_ID           = _cfg["pws_client_id"]
PWS_CLIENT_SECRET       = _cfg["pws_client_secret"]
DISCORD_BOT_TOKEN       = _cfg["discord_bot_token"]
DISCORD_GUILD_ID        = _cfg.get("discord_guild_id")
LOCATION_NAME           = _cfg.get("location_name","Southern NJ")
ALERT_INTERVAL_SECS     = int(_cfg.get("alert_interval_secs",300))
CONDITIONS_UPDATE_MINS  = int(_cfg.get("conditions_update_mins",30))
CONDITIONS_REPOST_HOURS = int(_cfg.get("conditions_repost_hours",4))
PIN_CONDITIONS          = bool(_cfg.get("pin_conditions_message",True))
FORECAST_LAT            = float(_cfg.get("forecast_lat",39.455))
FORECAST_LON            = float(_cfg.get("forecast_lon",-74.722))
TIDE_STATION_ID         = _cfg.get("tide_station_id","8534720")
TIDE_STATION_NAME       = _cfg.get("tide_station_name","Atlantic City, NJ")
RADAR_STATION           = str(_cfg.get("radar_station","KDIX")).upper()
RADAR_STATION_NAME      = _cfg.get("radar_station_name","Fort Dix, NJ")
RADAR_REGION            = _cfg.get("radar_region","northeast")
RADAR_ATTACH_IMAGE      = bool(_cfg.get("radar_attach_image",True))
COMMAND_SYNC            = str(_cfg.get("command_sync","auto")).lower()
PRESENCE_ENABLED        = bool(_cfg.get("presence_enabled",True))
PRESENCE_ROTATE_SECS    = max(60, int(_cfg.get("presence_rotate_secs",120)))
# Shown before the first observation/alert check completes, or if data is
# briefly unavailable.  Kept region-neutral by using location_name.
PRESENCE_FALLBACK       = str(_cfg.get("presence_fallback",
                                       f"the skies over {LOCATION_NAME}"))[:128]
AIRNOW_KEY              = _cfg.get("airnow_api_key")
AQI_THRESHOLD           = int(_cfg.get("aqi_alert_threshold",3))
WEEKLY_DAY              = int(_cfg.get("weekly_summary_day",6))
WEEKLY_HOUR             = int(_cfg.get("weekly_summary_hour",8))

# Alert post threshold: "all" (default) | "watch" | "warning"
# "all"     -> post every alert including advisories and statements
# "watch"   -> post watches and warnings only (skip advisories/statements)
# "warning" -> post only confirmed warnings (highest confidence)
# Suppressed alerts still appear in /alerts and are tracked for deduplication.
ALERT_POST_THRESHOLD = _cfg.get("alert_post_threshold", "all")
ALERT_SUPPRESS_TYPES = set(_cfg.get("alert_suppress_types", []))

def _alert_tier(event: str) -> int:
    """
    Classify an NWS event by urgency tier.
    1 = Warning  — imminent / high confidence (e.g. Tornado Warning)
    2 = Watch    — conditions favorable, lower confidence (e.g. Flood Watch)
    3 = Advisory / Statement / other (e.g. Special Weather Statement)
    """
    e = event.lower()
    if "warning" in e: return 1
    if "watch" in e:   return 2
    return 3

_TIER_DOT   = {1: "🔴", 2: "🟡", 3: "🔵"}
_TIER_LABEL = {1: "Warning", 2: "Watch", 3: "Advisory/Statement"}

def _alert_is_suppressed(event: str) -> bool:
    """Return True when this event should not be auto-posted based on config."""
    if event in ALERT_SUPPRESS_TYPES:
        return True
    return _alert_tier(event) > _THRESHOLD_TIER.get(ALERT_POST_THRESHOLD, 3)

# ---------------------------------------------------------------------------
# API URLs
# ---------------------------------------------------------------------------
PWS_API_URL         = (f"https://api.aerisapi.com/observations/PWS_{PWS_STATION_ID}"
                       f"?client_id={PWS_CLIENT_ID}&client_secret={PWS_CLIENT_SECRET}")
NWS_ALERTS_URL      = "https://api.weather.gov/alerts/active?area=NJ"
NWS_POINTS_URL      = f"https://api.weather.gov/points/{FORECAST_LAT},{FORECAST_LON}"
NOAA_TIDES_BASE     = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
AIRNOW_OBS_URL      = "https://www.airnowapi.org/aq/observation/latLong/current/"
# NWS RIDGE II "standard" products: <base>/<STATION>_loop.gif (animated) and
# <STATION>_0.gif (latest single frame).  Station list: https://radar.weather.gov/
RADAR_IMAGE_BASE    = "https://radar.weather.gov/ridge/standard"
_RADAR_MAX_BYTES    = 8 * 1024 * 1024   # stay under Discord's attachment limit
AIRNOW_FORECAST_URL = "https://www.airnowapi.org/aq/forecast/latLong/"
NHC_STORMS_URL      = "https://www.nhc.noaa.gov/CurrentStorms.json"
USER_AGENT          = f"DiscordWeatherBot/{__version__}"

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
_TZ = ZoneInfo("America/New_York")

def _fmt_time(dt_str) -> str:
    if not dt_str:
        return datetime.now(_TZ).strftime("%b %d, %Y  %I:%M %p %Z")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_TZ).strftime("%b %d, %Y  %I:%M %p %Z")
    except Exception:
        return dt_str

def _now_et() -> datetime:
    return datetime.now(_TZ)

# ---------------------------------------------------------------------------
# Console event + command logging
# ---------------------------------------------------------------------------
def _event(msg: str, detail: str = ""):
    ts = _now_et().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    if detail: log.info(f"{msg} | {detail}")
    else:       log.info(msg)

def _log_cmd(interaction: discord.Interaction, name: str, extra: str = ""):
    in_dm = isinstance(interaction.channel, discord.DMChannel)
    where = "DM" if in_dm else f"#{getattr(interaction.channel,'name',interaction.channel_id)}"
    summary = f"/{name} -> {interaction.user} ({where})"
    detail  = (f"user_id={interaction.user.id}, channel={interaction.channel_id}, "
               f"guild={interaction.guild_id or 'DM'}" + (f", {extra}" if extra else ""))
    _event(summary, detail)

# ---------------------------------------------------------------------------
# Discord embed limits
# ---------------------------------------------------------------------------
# Hard limits Discord enforces on message embeds (a 400 on send otherwise):
#   title 256, description 4096, field name 256, field value 1024,
#   footer 2048, author name 256, at most 25 fields, and a 6000-char total
#   across all textual parts of one embed.
# _clamp_embed() applies all of these to a finished embed dict so individual
# builders can't overflow a limit and fail the send.  Belt-and-suspenders:
# builders still truncate their own long fields where it produces nicer text
# (e.g. an ellipsis mid-sentence), but this guarantees correctness centrally.
_D_TITLE, _D_DESC, _D_FNAME, _D_FVAL, _D_FOOT = 256, 4096, 256, 1024, 2048
_D_MAX_FIELDS, _D_TOTAL = 25, 6000

def _clamp_embed(embed: dict) -> dict:
    """Truncate an embed dict in place to Discord's limits; returns it."""
    def cut(s, n):
        s = "" if s is None else str(s)
        return s if len(s) <= n else s[: n - 1] + "…"

    if "title" in embed:       embed["title"]       = cut(embed["title"], _D_TITLE)
    if "description" in embed: embed["description"] = cut(embed["description"], _D_DESC)
    if isinstance(embed.get("footer"), dict) and "text" in embed["footer"]:
        embed["footer"]["text"] = cut(embed["footer"]["text"], _D_FOOT)
    if isinstance(embed.get("author"), dict) and "name" in embed["author"]:
        embed["author"]["name"] = cut(embed["author"]["name"], _D_TITLE)

    fields = embed.get("fields")
    if isinstance(fields, list):
        if len(fields) > _D_MAX_FIELDS:
            log.warning(f"Embed had {len(fields)} fields; truncated to "
                        f"{_D_MAX_FIELDS}")
            fields = fields[:_D_MAX_FIELDS]
        for fld in fields:
            fld["name"]  = cut(fld.get("name", "\u200b") or "\u200b", _D_FNAME)
            fld["value"] = cut(fld.get("value", "\u200b") or "\u200b", _D_FVAL)
        embed["fields"] = fields

    # Enforce the 6000-char aggregate by trimming the description if needed.
    def total(e):
        t = len(e.get("title", "")) + len(e.get("description", ""))
        t += len(e.get("footer", {}).get("text", "")) if isinstance(e.get("footer"), dict) else 0
        for fld in e.get("fields", []):
            t += len(fld.get("name", "")) + len(fld.get("value", ""))
        return t
    over = total(embed) - _D_TOTAL
    if over > 0 and embed.get("description"):
        keep = max(0, len(embed["description"]) - over - 1)
        embed["description"] = embed["description"][:keep] + "…"
        log.warning("Embed exceeded 6000 chars; description trimmed")
    return embed

def _embed(embed_dict: dict) -> "discord.Embed":
    """Clamp a dict to Discord's limits, then build the Embed.  All embed
    construction goes through here so no send can 400 on a length limit."""
    return discord.Embed.from_dict(_clamp_embed(embed_dict))


# ---------------------------------------------------------------------------
# State  (atomic writes via temp file)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    base = {
        "posted_alerts":             {},
        "conditions_message_id":     None,
        "conditions_message_ts":     0.0,
        "last_conditions_update_ts": 0,
        "last_alert_check_ts":       0,
        "last_aqi_check_ts":         0,
        "last_aqi_category":         1,
        "channel_id":                None,
        "forecast_url":              None,
        "weekly_posted":             [],
        "pressure_history":          [],
    }
    try:
        if STATE_FILE.exists():
            stored = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for old, new in [("webhook_channel_id","channel_id")]:
                if old in stored:
                    stored.setdefault(new, stored.pop(old))
            base.update(stored)
    except Exception as e:
        log.error(f"Could not load state: {type(e).__name__}: {e}")
    return base

def save_state(s: dict):
    """Write to a temp file then atomically rename to avoid corruption on crash."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error(f"Could not save state: {type(e).__name__}: {e}")
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

_state: dict = {}
_start_time: float = 0.0   # set in on_ready

# ---------------------------------------------------------------------------
# aiohttp session
# ---------------------------------------------------------------------------
_session: aiohttp.ClientSession | None = None

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    return _session

async def _close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        log.info('aiohttp session closed')

# Subclass Client so close() always tears down the aiohttp session cleanly
class _BotClient(discord.Client):
    async def close(self):
        await _close_session()
        await super().close()

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_CB_THRESHOLDS   = {5: 1800, 10: 7200}   # failures -> backoff seconds
_circuit: dict   = defaultdict(lambda: {"failures": 0, "until": 0.0})

def _cb_ok(service: str) -> bool:
    """Return True when the service is NOT currently backed off."""
    return _circuit[service]["until"] <= time.time()

def _cb_success(service: str):
    if _circuit[service]["failures"] > 0:
        log.info(f"CB: {service} recovered after {_circuit[service]['failures']} failures")
    _circuit[service] = {"failures": 0, "until": 0.0}

def _cb_failure(service: str):
    s = _circuit[service]
    s["failures"] += 1
    n = s["failures"]
    for threshold in sorted(_CB_THRESHOLDS, reverse=True):
        if n >= threshold:
            s["until"] = time.time() + _CB_THRESHOLDS[threshold]
            log.warning(f"CB: {service} backed off {_CB_THRESHOLDS[threshold]//60} min "
                        f"after {n} consecutive failures")
            break

# ---------------------------------------------------------------------------
# Async HTTP helper with retry + circuit breaker
# ---------------------------------------------------------------------------
async def _http_get(url: str, *, service: str = "unknown", retries: int = 3,
                    base_delay: float = 5.0, params=None, timeout: int = 15) -> dict | list:
    """
    Async GET with exponential-backoff retry. Returns parsed JSON.
    4xx errors (except retryable ones) raise immediately.
    Integrates with the per-service circuit breaker.
    """
    if not _cb_ok(service):
        remaining = int(_circuit[service]["until"] - time.time()) // 60
        raise RuntimeError(f"{service} is backed off for ~{remaining} more min")

    to       = aiohttp.ClientTimeout(total=timeout)
    last_exc: Exception | None = None

    for attempt in range(retries):
        try:
            sess = await _get_session()
            async with sess.get(url, params=params, timeout=to) as r:
                if r.status in _RETRYABLE_CODES and attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"_http_get ({service}): HTTP {r.status} — "
                                f"retry {attempt+1}/{retries-1} in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                r.raise_for_status()
                data = await r.json(content_type=None)
                _cb_success(service)
                return data
        except aiohttp.ClientResponseError as e:
            if e.status in _RETRYABLE_CODES and attempt < retries - 1:
                last_exc = e
                delay = base_delay * (2 ** attempt)
                log.warning(f"_http_get ({service}): HTTP {e.status} — "
                             f"retry {attempt+1}/{retries-1} in {delay:.0f}s")
                await asyncio.sleep(delay)
            else:
                _cb_failure(service)
                raise
        except (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError) as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(f"_http_get ({service}): connection error — "
                             f"retry {attempt+1}/{retries-1} in {delay:.0f}s")
                await asyncio.sleep(delay)
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(f"_http_get ({service}): timeout — "
                             f"retry {attempt+1}/{retries-1} in {delay:.0f}s")
                await asyncio.sleep(delay)

    _cb_failure(service)
    raise last_exc or RuntimeError(f"_http_get exhausted retries for {service}")

# ---------------------------------------------------------------------------
# Channel access
# ---------------------------------------------------------------------------
_channel: discord.TextChannel | None = None

async def _resolve_channel():
    global _channel
    channel_id = _cfg.get("discord_channel_id") or _state.get("channel_id")

    if not channel_id and _cfg.get("discord_webhook"):
        try:
            data = await _http_get(_cfg["discord_webhook"], service="webhook", retries=2)
            channel_id = int(data["channel_id"])
            log.info(f"Channel ID {channel_id} derived from webhook URL")
        except Exception as e:
            log.error(f"Could not derive channel ID from webhook URL: {type(e).__name__}: {e}")

    if not channel_id:
        log.error("No channel ID — set discord_channel_id in config.json")
        return

    _state["channel_id"] = int(channel_id)
    try:
        _channel = (bot.get_channel(int(channel_id))
                    or await bot.fetch_channel(int(channel_id)))
    except Exception as e:
        log.error(f"Could not access channel {channel_id}: {type(e).__name__}: {e}")

async def _send(embed_dict: dict,
                reference: discord.Message | None = None,
                view: discord.ui.View | None = None) -> discord.Message | None:
    if _channel is None:
        log.error("No channel resolved; cannot send"); return None
    try:
        kw: dict = {"embed": _embed(embed_dict)}
        if reference: kw["reference"] = reference; kw["mention_author"] = False
        if view:      kw["view"] = view
        return await _channel.send(**kw)
    except discord.Forbidden as e: log.error(f"channel.send() forbidden: {type(e).__name__}: {e}")
    except discord.HTTPException as e: log.error(f"channel.send() HTTP error: {type(e).__name__}: {e}")
    except Exception as e: log.error(f"channel.send() failed: {type(e).__name__}: {e}")
    return None

# ---------------------------------------------------------------------------
# Discord UI
# ---------------------------------------------------------------------------
class LinkButtonView(discord.ui.View):
    def __init__(self, buttons: list[tuple[str,str,str]]):
        super().__init__(timeout=None)
        # Discord allows at most 25 components (5 rows x 5) per view, and
        # button labels are capped at 80 chars.  Guard both so an unusually
        # long button list (e.g. many NHC storms) can't fail the send.
        valid = [(l, e, u) for (l, e, u) in buttons if u]
        if len(valid) > 25:
            log.warning(f"LinkButtonView given {len(valid)} buttons; "
                        f"showing first 25")
            valid = valid[:25]
        for label, emoji, url in valid:
            self.add_item(discord.ui.Button(
                label=label[:80], emoji=emoji, url=url,
                style=discord.ButtonStyle.link))

class ConditionsRefreshView(discord.ui.View):
    # Per-user cooldown: user_id -> last_refresh timestamp
    _cooldowns: dict[int, float] = {}
    _COOLDOWN_SECS = 60

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Refresh", emoji="🔄",
                       style=discord.ButtonStyle.secondary,
                       custom_id="weather_bot:conditions_refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        _log_cmd(interaction, "refresh_button")
        uid     = interaction.user.id
        now_ts  = time.time()
        last    = ConditionsRefreshView._cooldowns.get(uid, 0.0)
        remaining = self._COOLDOWN_SECS - (now_ts - last)
        if remaining > 0:
            await interaction.response.send_message(
                f"⏳  Please wait {int(remaining)}s before refreshing again.",
                ephemeral=True)
            return
        ConditionsRefreshView._cooldowns[uid] = now_ts
        # Opportunistic sweep: drop anyone whose cooldown has long since
        # expired so this dict doesn't grow for the life of the process.
        stale = [u for u,t in ConditionsRefreshView._cooldowns.items()
                 if now_ts - t > ConditionsRefreshView._COOLDOWN_SECS * 10]
        for u in stale: del ConditionsRefreshView._cooldowns[u]
        await interaction.response.defer(ephemeral=True)
        embed_dict = await _fetch_and_build_conditions()
        if embed_dict and interaction.message:
            await interaction.message.edit(embed=_embed(embed_dict))
            save_state(_state)   # persist pressure_history from this refresh
            await interaction.followup.send("Conditions refreshed!", ephemeral=True)
        else:
            await interaction.followup.send(
                "Could not reach the weather station right now.", ephemeral=True)

    # Quick-action buttons: each replies ephemerally (visible only to the
    # clicker) so the shared conditions message and the channel stay clean,
    # and reuses the same responder as the corresponding slash command.
    @discord.ui.button(label="Radar", emoji="📡",
                       style=discord.ButtonStyle.secondary,
                       custom_id="weather_bot:radar")
    async def radar_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _log_cmd(interaction, "radar_button")
        await interaction.response.defer(ephemeral=True)
        await _respond_radar(interaction, ephemeral=True)

    @discord.ui.button(label="Alerts", emoji="⚠️",
                       style=discord.ButtonStyle.secondary,
                       custom_id="weather_bot:alerts")
    async def alerts_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _log_cmd(interaction, "alerts_button")
        await interaction.response.defer(ephemeral=True)
        await _respond_alerts(interaction, ephemeral=True)

    @discord.ui.button(label="Forecast", emoji="📅",
                       style=discord.ButtonStyle.secondary,
                       custom_id="weather_bot:forecast")
    async def forecast_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _log_cmd(interaction, "forecast_button")
        await interaction.response.defer(ephemeral=True)
        await _respond_forecast(interaction, ephemeral=True)

class _AlertSelect(discord.ui.Select):
    def __init__(self, alerts: list):
        self._alerts = alerts
        options = []
        for i, feature in enumerate(alerts[:25]):
            props   = feature.get("properties",{})
            event   = props.get("event","Alert")
            area    = _coverage_area_str(props.get("areaDesc",""))[:50]
            exp     = (_fmt_time(props.get("expires"))[:16]
                       if props.get("expires") else "N/A")
            options.append(discord.SelectOption(
                label=event[:100],
                description=f"{area} · Exp {exp}"[:100],
                emoji=_ALERT_EMOJIS.get(event,"⚠️"),
                value=str(i),
            ))
        super().__init__(placeholder="Select an alert for full details…", options=options)

    async def callback(self, interaction: discord.Interaction):
        feature = self._alerts[int(self.values[0])]
        view    = _alert_view(feature)
        await interaction.response.send_message(
            embed=_embed(build_alert_embed(feature)),
            view=view, ephemeral=True)

class AlertSelectView(discord.ui.View):
    def __init__(self, alerts: list):
        super().__init__(timeout=300)
        self.add_item(_AlertSelect(alerts))

    async def on_timeout(self):
        """Disable the select menu after 5 min so users get a clear signal."""
        for item in self.children:
            item.disabled = True
        # We can't edit the message here without a reference, but discord.py
        # cleans up the view from memory; the disabled state is cosmetic only.
        # To fully grey it out, the caller would need to store the message ref.

# ---------------------------------------------------------------------------
# Weather conditions
# ---------------------------------------------------------------------------
def _condition_emoji(weather) -> str:
    if not weather: return "🌤️"
    w = weather.lower()
    if "thunder" in w or "t-storm" in w:               return "⛈️"
    if "blizzard" in w:                                return "🌨️"
    if "heavy snow" in w:                              return "❄️"
    if "snow" in w:                                    return "🌨️"
    if "sleet" in w or "ice pellet" in w:              return "🌧️"
    if "freezing" in w:                                return "🌧️"
    if "heavy rain" in w:                              return "🌧️"
    if "rain" in w or "shower" in w or "drizzle" in w: return "🌦️"
    if "fog" in w or "mist" in w or "haze" in w:      return "🌫️"
    if "smoke" in w or "dust" in w:                    return "🌫️"
    if "overcast" in w:                                return "☁️"
    if "mostly cloudy" in w:                           return "🌥️"
    if "cloudy" in w:                                  return "☁️"
    if "partly" in w or "scattered" in w:              return "⛅"
    if "mostly clear" in w or "mostly sunny" in w:     return "🌤️"
    if "clear" in w or "sunny" in w:                   return "☀️"
    if "wind" in w:                                    return "💨"
    return "🌤️"

def _wind_dir(deg: float) -> str:
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]

def _pws_station_url(station_id: str | None = None) -> str:
    """Public PWSweather.com page for a PWS — the Aeris/Xweather PWS network's
    public site.  Uses the queried station if given, else the default."""
    return f"https://www.pwsweather.com/station/mid/{(station_id or PWS_STATION_ID).lower()}"

async def fetch_conditions(station_id: str | None = None,
                           fast: bool = False) -> dict | None:
    """fast=True uses 1s/3s retry delays for interactive slash commands."""
    if station_id:
        url   = (f"https://api.aerisapi.com/observations/PWS_{station_id.upper()}"
                 f"?client_id={PWS_CLIENT_ID}&client_secret={PWS_CLIENT_SECRET}")
        label = station_id.upper()
    else:
        url   = PWS_API_URL
        label = PWS_STATION_ID
    try:
        data = await _http_get(url, service="pws", base_delay=1.0 if fast else 5.0)
        if not data.get("success"):
            log.error(f"PWS error ({label}): {data.get('error',{}).get('description','?')}")
            return None
        obs = data["response"]["ob"]
        return {
            "station_id":   label,
            "temp":         obs.get("tempF"),
            "feels_like":   obs.get("feelslikeF"),
            "humidity":     obs.get("humidity"),
            "dewpoint":     obs.get("dewpointF"),
            "pressure":     obs.get("pressureIN"),
            "wind_speed":   obs.get("windSpeedMPH"),
            "wind_gust":    obs.get("windGustMPH"),
            "wind_dir":     obs.get("windDirDEG"),
            "precip_rate":  obs.get("precipRateIN"),
            "precip_today": obs.get("precipIN"),
            "uv":           obs.get("uvi"),
            "weather":      obs.get("weather") or obs.get("weatherPrimary"),
            "obs_time":     obs.get("dateTimeISO"),
            "temp_min":     obs.get("tempMinF"),
            "temp_max":     obs.get("tempMaxF"),
        }
    except RuntimeError as e:
        log.warning(f"PWS skipped: {e}")
    except aiohttp.ClientResponseError as e:
        log.error(f"PWS fetch failed ({label}): HTTP {e.status}")
    except Exception as e:
        log.error(f"PWS fetch failed ({label}): {type(e).__name__}: {e}")
    return None

async def geocode_zip(zipcode: str) -> tuple[float,float] | None:
    try:
        data = await _http_get(f"https://api.zippopotam.us/us/{zipcode}",
                               service="geocode", retries=2)
        places = data.get("places",[])
        if places:
            return float(places[0]["latitude"]), float(places[0]["longitude"])
    except aiohttp.ClientResponseError as e:
        if e.status == 404: return None
        log.error(f"Zip geocode failed ({zipcode}): HTTP {e.status}")
    except Exception as e:
        log.error(f"Zip geocode failed ({zipcode}): {type(e).__name__}: {e}")
    return None

def build_conditions_embed(c: dict, aqi_data: list | None = None,
                           tendency: str = "", sun: dict | None = None) -> dict:
    station   = c.get("station_id", PWS_STATION_ID)
    emoji     = _condition_emoji(c.get("weather"))
    condition = c.get("weather") or "—"
    time_str  = _fmt_time(c.get("obs_time"))

    title = (f"{emoji}  {LOCATION_NAME} · {station}"
             if station == PWS_STATION_ID
             else f"{emoji}  Station {station}")

    if aqi_data:
        best     = max(aqi_data, key=lambda x: x.get("AQI",0))
        cat      = best.get("Category",{})
        aqi_part = (f" · {_aqi_dot(cat.get('Number',1))} AQI "
                    f"**{best.get('AQI','—')}** ({cat.get('Name','')})")
    else:
        aqi_part = ""
    # Daily min/max (only when using the default station and data is available)
    minmax = ""
    if c.get("temp_min") is not None and c.get("temp_max") is not None:
        minmax = f" · ↓{c['temp_min']}° ↑{c['temp_max']}°"
    line0 = f"**{condition}**{aqi_part}{minmax}"

    def _b(val, fmt="{}"):
        return f"**{fmt.format(val)}**" if val is not None else "**—**"

    if c.get("wind_dir") is not None and c.get("wind_speed") is not None:
        wind = f"**{_wind_dir(c['wind_dir'])} {c['wind_speed']} mph**"
        if c.get("wind_gust") and c["wind_gust"] > (c.get("wind_speed") or 0):
            wind += f" (gusts {c['wind_gust']})"
    else:
        wind = "**—**"

    temp_part = f"🌡️ {_b(c.get('temp'),'{}°F')}"
    if c.get("feels_like") is not None:
        temp_part += f" / feels {_b(c['feels_like'],'{}°F')}"
    hum_part = f"💧 {_b(c.get('humidity'),'{}%')}"
    if c.get("dewpoint") is not None:
        hum_part += f" / dew {_b(c['dewpoint'],'{}°F')}"
    line1 = f"{temp_part}  ·  {hum_part}  ·  🌬️ {wind}"

    pr = c.get("precip_rate"); pt = c.get("precip_today")
    rain = (f"🌧️ Rain **{pr:.2f}\"/hr** · **{pt:.2f}\"** today"
            if pr is not None and pt is not None else "🌧️ Rain **—**")
    press = f"🔵 {_b(c.get('pressure'),'{} inHg')}"
    if tendency: press += f" *{tendency}*"
    line2 = f"{press}  ·  {rain}  ·  ☀️ UV {_b(c.get('uv'))}"

    desc_lines = [line0, line1, line2]
    if sun:
        desc_lines.append(f"🌅 {sun['sunrise']}  ·  🌇 {sun['sunset']}")

    return {
        "title":       title,
        "description": "\n".join(desc_lines),
        "color":       0x1E90FF,
        "footer":      {"text": f"Last observed: {time_str}"},
    }

# ---------------------------------------------------------------------------
# Barometric tendency
# ---------------------------------------------------------------------------
def _record_pressure(pressure: float | None):
    if pressure is None: return
    buf: list = _state.setdefault("pressure_history",[])
    buf.append({"ts": time.time(), "v": float(pressure)})
    cutoff = time.time() - 3 * 3600
    _state["pressure_history"] = [h for h in buf if h["ts"] >= cutoff]

def _barometric_tendency() -> str:
    """
    Returns a pressure-trend string, or a '(collecting data)' note during
    the first ~25 minutes of operation before a comparison point exists.
    """
    buf = _state.get("pressure_history",[])
    if len(buf) < 2: return "*(collecting data)*"
    now_ts  = time.time(); current = buf[-1]["v"]
    older   = [h for h in buf[:-1] if now_ts - h["ts"] >= 25*60]
    if not older: return "*(collecting data)*"
    ref         = min(older, key=lambda h: abs(h["ts"]-(now_ts-3600)))
    rate        = (current-ref["v"]) / max((now_ts-ref["ts"])/3600, 1e-6)
    if abs(rate) < 0.01:  return "→ steady"
    if rate >=  0.06:     return "↑↑ rising rapidly"
    if rate >=  0.02:     return "↑ rising"
    if rate >=  0.01:     return "↑ rising slowly"
    if rate <= -0.06:     return "↓↓ falling rapidly"
    if rate <= -0.02:     return "↓ falling"
    return                        "↓ falling slowly"

# ---------------------------------------------------------------------------
# Sunrise / sunset
# ---------------------------------------------------------------------------
def _sun_times() -> dict | None:
    if not _ASTRAL_OK: return None
    try:
        loc = _AstralLocation(name=LOCATION_NAME, region="US",
                              timezone="America/New_York",
                              latitude=FORECAST_LAT, longitude=FORECAST_LON)
        s   = _astral_sun(loc.observer, date=_now_et().date(), tzinfo=_TZ)
        def fmt(dt): return dt.strftime("%I:%M %p").lstrip("0")
        return {"sunrise": fmt(s["sunrise"]), "sunset": fmt(s["sunset"])}
    except Exception as e:
        log.warning(f"Sun times failed: {e}"); return None

# ---------------------------------------------------------------------------
# Combined fetch + build helper
# ---------------------------------------------------------------------------
async def _fetch_and_build_conditions(station_id: str | None = None,
                                      fast: bool = False) -> dict | None:
    """
    Single coroutine used by the scheduler, /conditions slash command, and
    the Refresh button.  fast=True uses shorter retry delays for interactive use.
    Records pressure and computes tendency only when querying the default station.
    """
    aqi_data = await fetch_aqi() if AIRNOW_KEY else None
    c        = await fetch_conditions(station_id, fast=fast)
    if not c: return None
    if station_id is None:
        _record_pressure(c.get("pressure"))
        # Cache latest observation for the rotating presence status
        t = c.get("temp")
        if isinstance(t, (int, float)):
            _state["last_temp_f"] = t
        sky = c.get("weather")
        if isinstance(sky, str) and sky:
            _state["last_sky"] = sky
    return build_conditions_embed(
        c, aqi_data,
        tendency=_barometric_tendency() if station_id is None else "",
        sun=_sun_times(),
    )

# ---------------------------------------------------------------------------
# NWS Alerts
# ---------------------------------------------------------------------------
_ALERT_COLORS = {
    "Tornado Warning":0xFF0000,"Tornado Watch":0xFF4500,
    "Severe Thunderstorm Warning":0xFF6600,"Severe Thunderstorm Watch":0xFF8C00,
    "Flash Flood Warning":0x228B22,"Flash Flood Watch":0x2E8B57,
    "Flood Warning":0x228B22,"Flood Watch":0x00FF7F,
    "Winter Storm Warning":0xFF69B4,"Winter Storm Watch":0x4169E1,
    "Blizzard Warning":0xFF1493,"Ice Storm Warning":0x9400D3,
    "Winter Weather Advisory":0x7B68EE,"High Wind Warning":0xDAA520,
    "Wind Advisory":0xD2691E,"Excessive Heat Warning":0xFF4500,
    "Heat Advisory":0xFF6347,"Frost Advisory":0x00CED1,
    "Freeze Warning":0x00BFFF,"Dense Fog Advisory":0x708090,
    "Dense Smoke Advisory":0x708090,"Special Weather Statement":0xFFD700,
    "Tropical Storm Watch":0xFFB347,"Tropical Storm Warning":0xFF8C00,
    "Hurricane Watch":0xFF4500,"Hurricane Warning":0xFF0000,
    "Extreme Wind Warning":0xFF0000,"Storm Surge Watch":0x8B0000,
    "Storm Surge Warning":0x8B0000,
}
_ALERT_EMOJIS = {
    "Tornado Warning":"🌪️","Tornado Watch":"🌪️",
    "Severe Thunderstorm Warning":"⛈️","Severe Thunderstorm Watch":"⛈️",
    "Flash Flood Warning":"🌊","Flash Flood Watch":"🌊",
    "Flash Flood Statement":"🌊","Flood Warning":"🌊","Flood Watch":"🌊","Flood Advisory":"🌊",
    "Winter Storm Warning":"❄️","Winter Storm Watch":"❄️",
    "Winter Weather Advisory":"🌨️","Ice Storm Warning":"🧊","Blizzard Warning":"🌨️",
    "High Wind Warning":"💨","Wind Advisory":"💨",
    "Excessive Heat Warning":"🔥","Heat Advisory":"🌡️",
    "Frost Advisory":"🧊","Freeze Warning":"🧊",
    "Dense Fog Advisory":"🌫️","Dense Smoke Advisory":"🌫️",
    "Special Weather Statement":"📢","Hazardous Weather Outlook":"📋",
    "Beach Hazards Statement":"🏖️","Rip Current Statement":"🌊",
    "Small Craft Advisory":"⛵","Gale Warning":"💨",
    "Tropical Storm Watch":"🌀","Tropical Storm Warning":"🌀",
    "Hurricane Watch":"🌀","Hurricane Warning":"🌀",
    "Extreme Wind Warning":"💨","Storm Surge Watch":"🌊","Storm Surge Warning":"🌊",
}

# ---------------------------------------------------------------------------
# Coverage area  (config-driven; see DEFAULT_COVERAGE / config.json "coverage")
# ---------------------------------------------------------------------------
# All three lookup sets below are DERIVED from the single COVERAGE mapping, so
# the zone list, county-code list, county-name list, and the zone->county table
# can never drift out of sync with each other.  (Pre-v2.7.3 the zone->county
# table was maintained by hand separately, and was wrong for 9 of 10 zones.)
#
# NWS public forecast zones (WFO Mount Holly) for the 7 Southern NJ counties:
#   NJZ016 Salem            NJZ022 Atlantic
#   NJZ017 Gloucester       NJZ023 Cape May
#   NJZ018 Camden           NJZ024 Atlantic Coastal Cape May  (Cape May Co.!)
#   NJZ019 NW Burlington    NJZ025 Coastal Atlantic
#   NJZ021 Cumberland       NJZ027 SE Burlington
# Deliberately EXCLUDED: NJZ015 (Mercer), NJZ020 (Ocean), NJZ026 (Coastal Ocean).
#
# NOTE: "Atlantic Coastal Cape May" (NJZ024) is a CAPE MAY county zone despite
# the name.  Matching zones by name rather than code is how it gets mis-filed.
DEFAULT_COVERAGE: dict[str, dict] = {
    "Salem":      {"county_code": "NJC033", "zones": ["NJZ016"]},
    "Gloucester": {"county_code": "NJC015", "zones": ["NJZ017"]},
    "Camden":     {"county_code": "NJC007", "zones": ["NJZ018"]},
    "Burlington": {"county_code": "NJC005", "zones": ["NJZ019", "NJZ027"]},
    "Cumberland": {"county_code": "NJC011", "zones": ["NJZ021"]},
    "Atlantic":   {"county_code": "NJC001", "zones": ["NJZ022", "NJZ025"]},
    "Cape May":   {"county_code": "NJC009", "zones": ["NJZ023", "NJZ024"]},
}

def _derive_coverage(cov: dict) -> tuple[frozenset, frozenset, frozenset, dict]:
    """
    Derive the four lookup structures from a coverage mapping:
      (county names, county codes, zone codes, zone -> county code)
    Kept as a function so tests can exercise any coverage config, and so the
    four stay consistent by construction.
    """
    names        = frozenset(cov)
    county_codes = frozenset(e["county_code"] for e in cov.values()
                             if e.get("county_code"))
    zones        = frozenset(z for e in cov.values() for z in e.get("zones", []))
    # Zone -> county code.  Used to build NWS link URLs when the alert's UGC
    # list carries zone codes but no county code (common for coastal/marine
    # alert types).  A zone with no county mapping simply yields no NWS link
    # button; nothing else breaks.
    zone_to_county = {z: e["county_code"]
                      for e in cov.values() if e.get("county_code")
                      for z in e.get("zones", [])}
    return names, county_codes, zones, zone_to_county

COVERAGE: dict[str, dict] = _cfg.get("coverage") or DEFAULT_COVERAGE
(_COVERAGE_NAMES, _COVERAGE_COUNTY_CODES,
 _COVERAGE_ZONES, _ZONE_TO_COUNTY) = _derive_coverage(COVERAGE)

def _coverage_label() -> str:
    """Human-readable county list for embeds, derived from COVERAGE."""
    names = sorted(_COVERAGE_NAMES)
    if not names:
        return LOCATION_NAME
    noun = "County" if len(names) == 1 else "counties"
    return f"{', '.join(names)} {noun}"

def _is_coverage_zone(code: str) -> bool:
    return code in _COVERAGE_ZONES

def _in_coverage(feature: dict) -> bool:
    """
    UGC geocodes are authoritative when present: in scope only if the alert
    lists a configured forecast zone or county code.  County-name matching
    against areaDesc/headline is a fallback for the rare alert that carries
    no UGC list.
    (Pre-v2.7.1 this accepted ANY NJZ/NJC code numbered 15-27.  That range is
    wrong for county codes — NJC021/023/025 are Mercer/Middlesex/Monmouth —
    and also swept in zones NJZ015/020/026 (Mercer, Ocean, Coastal Ocean),
    causing out-of-area alerts to post.)
    """
    props = feature.get("properties",{})
    ugc   = props.get("geocode",{}).get("UGC",[])
    if ugc:
        return any(c in _COVERAGE_ZONES or c in _COVERAGE_COUNTY_CODES for c in ugc)
    combined = props.get("areaDesc","") + " " + props.get("headline","")
    return any(c in combined for c in _COVERAGE_NAMES)

def _coverage_area_str(area_desc: str) -> str:
    parts = [p.strip() for p in area_desc.split(";")
             if any(c in p for c in _COVERAGE_NAMES)]
    return "; ".join(parts) if parts else area_desc[:300]

def _nws_human_url(feature: dict) -> str:
    """
    Build a forecast.weather.gov/showsigwx.php URL for an active alert.
    Requires both warnzone and warncounty — county is pulled from the UGC
    list first, then from the static fallback table if not present.
    """
    ugc  = feature.get("properties",{}).get("geocode",{}).get("UGC",[])
    zone = next((c for c in ugc if _is_coverage_zone(c)), "")
    if not zone:
        return ""
    # Prefer explicit county code from UGC; fall back to static table
    county = (next((c for c in ugc if c in _COVERAGE_COUNTY_CODES), "")
              or _ZONE_TO_COUNTY.get(zone, ""))
    if not county:
        # No county resolved — URL would be incomplete; skip NWS link entirely
        return ""
    return (f"https://forecast.weather.gov/showsigwx.php"
            f"?warnzone={zone}&warncounty={county}")

def _iem_vtec_url(feature: dict) -> str:
    try:
        vtec_raw = feature.get("properties",{}).get("parameters",{}).get("VTEC",[])
        if not vtec_raw: return ""
        vtec  = (vtec_raw[0] if isinstance(vtec_raw,list) else vtec_raw).strip().strip("/")
        parts = vtec.split(".")
        if len(parts) < 6: return ""
        time_part = parts[6] if len(parts) > 6 else ""
        year = f"20{time_part[:2]}" if len(time_part) >= 2 else str(_now_et().year)
        return (f"https://mesonet.agron.iastate.edu/vtec/event/"
                f"{year}-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}-{parts[4]}-{parts[5]}/")
    except Exception: return ""

def _alert_view(feature: dict) -> discord.ui.View | None:
    nws = _nws_human_url(feature); iem = _iem_vtec_url(feature)
    buttons = []
    if nws: buttons.append(("Active Alert","🔗",nws))
    if iem: buttons.append(("IEM Archive","📚",iem))
    return LinkButtonView(buttons) if buttons else None

async def fetch_alerts(fast: bool = False) -> list | None:
    """
    fast=True uses shorter retry delays for interactive slash commands.
    Returns None on fetch failure (vs. [] for a successful fetch with no
    alerts) so callers can tell an API outage apart from a quiet day —
    treating an outage as "no active alerts" would falsely CLEAR everything.
    """
    try:
        data = await _http_get(NWS_ALERTS_URL, service="nws_alerts",
                               base_delay=1.0 if fast else 5.0)
        return data.get("features",[])
    except RuntimeError as e:
        log.warning(f"Alerts skipped: {e}")
    except Exception as e:
        log.error(f"NWS alerts fetch failed: {type(e).__name__}: {e}")
    return None

def build_alert_embed(feature: dict) -> dict:
    props = feature.get("properties",{})
    event = props.get("event","Weather Alert")
    area  = _coverage_area_str(props.get("areaDesc","")) or props.get("areaDesc","")[:300]
    exp   = _fmt_time(props.get("expires")) if props.get("expires") else "N/A"
    return {
        "title":       f"{_ALERT_EMOJIS.get(event,'⚠️')}  {event}",
        "color":       _ALERT_COLORS.get(event,0xFFA500),
        "description": props.get("headline",""),
        "fields":      [{"name":"📍 Areas","value":area[:1024],"inline":False},
                        {"name":"⏰ Expires","value":exp,"inline":True}],
        "footer":      {"text":"Discord Weather Bot | Click a button for full details"},
    }

def build_update_embed(feature: dict) -> dict:
    props = feature.get("properties",{})
    event = props.get("event","Weather Alert")
    area  = _coverage_area_str(props.get("areaDesc","")) or props.get("areaDesc","")[:300]
    exp   = _fmt_time(props.get("expires")) if props.get("expires") else "N/A"
    return {
        "title":       f"🔄  UPDATED — {_ALERT_EMOJIS.get(event,'⚠️')} {event}",
        "color":       _ALERT_COLORS.get(event,0xFFA500),
        "description": props.get("headline",""),
        "fields":      [{"name":"📍 Areas","value":area[:1024],"inline":False},
                        {"name":"⏰ New Expiry","value":exp,"inline":True}],
        "footer":      {"text":"This alert has been extended or modified • NWS"},
    }

def build_alerts_summary_embed(alerts: list,
                               suppressed_events: set | None = None) -> dict:
    """
    Summary embed for 2+ active alerts.
    suppressed_events: set of event names that are below the post threshold
    (shown with a grey dot and a note so users understand why they were silent).
    """
    lines = []
    for f in alerts:
        props   = f.get("properties",{})
        event   = props.get("event","Alert")
        tier    = _alert_tier(event)
        area    = _coverage_area_str(props.get("areaDesc",""))[:80]
        expires = _fmt_time(props.get("expires")) if props.get("expires") else "N/A"
        upd_tag = " *(Updated)*" if props.get("messageType","Alert")=="Update" else ""
        sup_tag = " *(not auto-posted — below threshold)*" \
                  if suppressed_events and event in suppressed_events else ""
        tier_dot = _TIER_DOT.get(tier, "⚪")
        lines.append(f"{tier_dot} {_ALERT_EMOJIS.get(event,'⚠️')} **{event}**{upd_tag}{sup_tag}\n"
                     f"↳ {area} · Exp. {expires}")
    return {
        "title":       f"⚠️  {len(alerts)} Active Alert{'s' if len(alerts)!=1 else ''} — {LOCATION_NAME}",
        "description": "\n\n".join(lines),
        "color":       0xFF6600,
        "footer":      {"text":"Use the dropdown below for full details on each alert"},
    }

async def _send_cleared(message_id, event: str, area: str, cancelled: bool = False):
    emoji  = _ALERT_EMOJIS.get(event,"⚠️")
    action = "been explicitly cancelled" if cancelled else "expired or been cancelled"
    ed     = {"title":f"✅  CLEARED — {emoji} {event}",
              "description":f"The **{event}** for **{area}** has {action}.",
              "color":0x57F287,"footer":{"text":"NWS | Discord Weather Bot"}}
    sent   = False
    if _channel and message_id:
        try:
            orig = await _channel.fetch_message(int(message_id))
            await _channel.send(embed=_embed(ed),
                                reference=orig, mention_author=False)
            sent = True
        except discord.NotFound:
            log.warning(f"Original alert msg {message_id} not found; posting standalone")
        except Exception as e:
            log.error(f"Cleared reply failed: {type(e).__name__}: {e}")
    if not sent:
        await _send(ed)
    _event(f"✅  CLEARED: {event} — {area}",
           f"cancelled={cancelled}, original_msg={message_id}")

def _find_ref_in_posted(refs: list, posted: dict):
    """
    NWS often lists the *entire* update chain in `references`, not just the
    immediate predecessor, and older links may still be present in `posted`.
    Returning the first list match (pre-v2.7.2 behavior) could latch onto an
    older ancestor instead of the immediate one, misdirecting reply-threading
    and `superseded_by` bookkeeping.  Matching by latest `ts` instead always
    picks the most recent still-tracked ancestor, regardless of the order
    NWS lists references in.
    """
    candidates = []
    for ref in refs:
        for c in (ref.get("identifier",""), ref.get("@id","")):
            if not c: continue
            if c in posted: candidates.append(c)
            full = f"https://api.weather.gov/alerts/{c}"
            if full in posted: candidates.append(full)
    if not candidates: return None
    return max(candidates, key=lambda c: posted[c].get("ts",0))

# ---------------------------------------------------------------------------
# NOAA Tides
# ---------------------------------------------------------------------------
async def fetch_radar_image() -> bytes | None:
    """
    Fetch the current NWS RIDGE loop image for the configured station.

    Returns None on any failure — the caller falls back to link buttons, so a
    radar outage or an unexpected URL change degrades to the previous
    behavior rather than breaking /radar.
    """
    url = f"{RADAR_IMAGE_BASE}/{RADAR_STATION}_loop.gif"
    try:
        sess = await _get_session()
        to   = aiohttp.ClientTimeout(total=20)
        async with sess.get(url, timeout=to) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "image" not in ctype:
                log.warning(f"Radar image: unexpected Content-Type {ctype!r} "
                            f"for {RADAR_STATION}")
                return None
            data = await r.read()
    except Exception as e:
        log.warning(f"Radar image fetch failed ({RADAR_STATION}): "
                    f"{type(e).__name__}: {e}")
        return None
    # Discord rejects oversized attachments; bail out rather than fail the send
    if len(data) > _RADAR_MAX_BYTES:
        log.warning(f"Radar image too large ({len(data)//1024} KB) — "
                    f"falling back to links")
        return None
    log.info(f"Radar image fetched: {RADAR_STATION} ({len(data)//1024} KB)")
    return data

async def fetch_tides(days: int = 2, station_id: str | None = None) -> list | None:
    sid    = station_id or TIDE_STATION_ID
    now_et = _now_et()
    try:
        data = await _http_get(NOAA_TIDES_BASE, service="noaa_tides", params={
            "station":    sid, "product":"predictions", "datum":"MLLW",
            "time_zone":  "lst_ldt", "interval":"hilo", "units":"english",
            "application":"DiscordWeatherBot", "format":"json",
            "begin_date": now_et.strftime("%Y%m%d"),
            "end_date":   (now_et+timedelta(days=max(days,1))).strftime("%Y%m%d"),
        })
        if "error" in data:
            log.error(f"NOAA Tides error: {data['error'].get('message')}"); return None
        return data.get("predictions",[])
    except RuntimeError as e:
        log.warning(f"Tides skipped: {e}")
    except Exception as e:
        log.error(f"NOAA tides fetch failed: {type(e).__name__}: {e}")
    return None

def _fmt_tide_entry(p: dict) -> str:
    icon = "🔺" if p.get("type")=="H" else "🔻"
    try:
        t_str = datetime.strptime(p["t"],"%Y-%m-%d %H:%M").strftime("%I:%M %p").lstrip("0")
    except Exception: t_str = p.get("t","?")
    return f"{icon} {t_str}  {float(p.get('v',0)):.1f} ft"

def build_tides_embed(predictions: list, station_id: str | None = None,
                      station_name: str | None = None) -> dict:
    sid   = station_id or TIDE_STATION_ID
    sname = station_name or TIDE_STATION_NAME
    by_date: dict[str,list] = defaultdict(list)
    for p in predictions: by_date[p["t"][:10]].append(p)
    fields = []
    today  = _now_et().date()
    for date_str in sorted(by_date)[:3]:
        try:
            d     = datetime.strptime(date_str,"%Y-%m-%d").date()
            label = "Today" if d==today else datetime.strptime(date_str,"%Y-%m-%d").strftime("%a %b %d")
        except Exception: label = date_str
        fields.append({"name":f"📅 {label}",
                       "value":"\n".join(_fmt_tide_entry(p) for p in by_date[date_str]) or "No data",
                       "inline":True})
    return {"title":f"🌊  Tides — {sname}",
            "description":f"NOAA Station `{sid}` · All times local (ET)",
            "color":0x006994,"fields":fields,
            "footer":{"text":"NOAA CO-OPS | Discord Weather Bot"}}

# ---------------------------------------------------------------------------
# EPA AirNow AQI
# ---------------------------------------------------------------------------
_AQI_DOT   = {1:"🟢",2:"🟡",3:"🟠",4:"🔴",5:"🟣",6:"⚫"}
_AQI_LABEL = {1:"Good",2:"Moderate",3:"Unhealthy for Sensitive Groups",
               4:"Unhealthy",5:"Very Unhealthy",6:"Hazardous"}
_AQI_COLOR = [0x00E400,0xFFFF00,0xFF7E00,0xFF0000,0x8F3F97,0x7E0023]
_AQI_HEALTH= {
    1:"Air quality is satisfactory with little or no risk.",
    2:"Acceptable, but a concern for a small number of sensitive people.",
    3:"Sensitive groups may experience effects. General public less likely affected.",
    4:"Some members of the general public may experience effects.",
    5:"Health alert — risk of health effects is increased for everyone.",
    6:"Health warning — everyone is more likely to be affected.",
}
def _aqi_dot(cat: int) -> str:   return _AQI_DOT.get(cat,"⚪")
def _aqi_color(cat: int) -> int: return _AQI_COLOR[cat-1] if 1<=cat<=6 else 0x808080

_aqi_obs_cache: dict = {"data":None,"ts":0.0}
AQI_CACHE_SECS = 3600

async def fetch_aqi() -> list | None:
    if not AIRNOW_KEY: return None
    if time.time()-_aqi_obs_cache["ts"] < AQI_CACHE_SECS and _aqi_obs_cache["data"] is not None:
        return _aqi_obs_cache["data"]
    try:
        data = await _http_get(AIRNOW_OBS_URL, service="airnow", params={
            "format":"application/json","latitude":FORECAST_LAT,
            "longitude":FORECAST_LON,"distance":25,"API_KEY":AIRNOW_KEY,
        })
        _aqi_obs_cache["data"] = data; _aqi_obs_cache["ts"] = time.time()
        return data
    except RuntimeError as e:
        log.warning(f"AQI skipped: {e}")
    except aiohttp.ClientResponseError as e:
        if e.status >= 500:
            log.warning(f"AirNow obs: HTTP {e.status} (transient)")
        else:
            log.error(f"AirNow obs: HTTP {e.status} — check API key")
    except Exception as e:
        log.error(f"AirNow obs fetch failed: {type(e).__name__}: {e}")
    return None

async def fetch_aqi_forecast() -> list | None:
    if not AIRNOW_KEY: return None
    try:
        return await _http_get(AIRNOW_FORECAST_URL, service="airnow", params={
            "format":"application/json","latitude":FORECAST_LAT,
            "longitude":FORECAST_LON,"date":_now_et().strftime("%Y-%m-%d"),
            "distance":25,"API_KEY":AIRNOW_KEY,
        })
    except RuntimeError as e:
        log.warning(f"AQI forecast skipped: {e}")
    except Exception as e:
        log.error(f"AirNow forecast fetch failed: {type(e).__name__}: {e}")
    return None

def build_aqi_embed(obs: list, forecast: list | None = None) -> dict:
    overall  = max(obs, key=lambda x: x.get("AQI",0)) if obs else {}
    best_num = overall.get("Category",{}).get("Number",1)
    area     = overall.get("ReportingArea",LOCATION_NAME)
    obs_fields = [{"name":f"{_aqi_dot(i.get('Category',{}).get('Number',1))} {i.get('ParameterName','?')}",
                   "value":f"AQI **{i.get('AQI','—')}** — {i.get('Category',{}).get('Name','?')}",
                   "inline":True} for i in obs]
    fcst_by_date: dict[str,list] = defaultdict(list)
    for item in (forecast or []):
        date_raw = item.get("DateForecast","").strip()
        if date_raw: fcst_by_date[date_raw].append(item)
    fcst_fields = []
    today_s    = _now_et().strftime("%Y-%m-%d")
    tomorrow_s = (_now_et()+timedelta(days=1)).strftime("%Y-%m-%d")
    for date_str,label in [(today_s,"Today's Forecast"),(tomorrow_s,"Tomorrow's Forecast")]:
        items = fcst_by_date.get(date_str,[])
        if items:
            best_f  = max(items,key=lambda x:x.get("AQI",0))
            action  = "  ⚠️ Action Day" if best_f.get("ActionDay") else ""
            lines   = "\n".join(
                f"{_aqi_dot(i.get('Category',{}).get('Number',1))} "
                f"{i.get('ParameterName','?')}: {i.get('Category',{}).get('Name','?')} ({i.get('AQI','—')})"
                for i in items)
            fcst_fields.append({"name":f"📅 {label}{action}","value":lines,"inline":False})
    health = [{"name":"💡 Health Guidance","value":_AQI_HEALTH.get(best_num,""),"inline":False}] \
             if best_num >= 2 else []
    return {"title":f"{_aqi_dot(best_num)}  Air Quality — {area}",
            "description":f"Overall: **{_AQI_LABEL.get(best_num,'Unknown')}** (AQI {overall.get('AQI','—')})",
            "color":_aqi_color(best_num),"fields":obs_fields+fcst_fields+health,
            "footer":{"text":"EPA AirNow | Discord Weather Bot"}}

def build_aqi_alert_embed(data: list, improving: bool = False) -> dict:
    overall = max(data,key=lambda x:x.get("AQI",0))
    num     = overall.get("Category",{}).get("Number",1)
    area    = overall.get("ReportingArea",LOCATION_NAME)
    if improving:
        return {"title":"🟢  Air Quality Improved",
                "description":f"AQI returned to **{_AQI_LABEL.get(num,'acceptable')}** "
                              f"(AQI {overall.get('AQI','—')}) in {area}.",
                "color":0x57F287,"footer":{"text":"EPA AirNow | Discord Weather Bot"}}
    return {"title":f"{_aqi_dot(num)}  AIR QUALITY ALERT — {_AQI_LABEL.get(num,'Unhealthy')}",
            "description":f"AQI has reached **{overall.get('AQI','—')}** in {area}.\n{_AQI_HEALTH.get(num,'')}",
            "color":_aqi_color(num),
            "fields":[{"name":"Area","value":area,"inline":True},
                      {"name":"AQI","value":str(overall.get("AQI","—")),"inline":True}],
            "footer":{"text":"EPA AirNow | Discord Weather Bot"}}

# ---------------------------------------------------------------------------
# NHC Hurricane
# ---------------------------------------------------------------------------
def _knots_to_mph(k) -> int:
    try: return round(int(k)*1.15078)
    except Exception: return 0

def _storm_category(cl: str, mph: int) -> str:
    c = cl.upper()
    if c in ("TD","SD","LO","WV","DB"): return f"Tropical Depression ({mph} mph)"
    if c in ("TS","SS") or mph < 74:   return f"Tropical Storm ({mph} mph)"
    if mph < 96:  return f"Category 1 Hurricane ({mph} mph)"
    if mph < 111: return f"Category 2 Hurricane ({mph} mph)"
    if mph < 130: return f"Category 3 Hurricane ({mph} mph)"
    if mph < 157: return f"Category 4 Hurricane ({mph} mph)"
    return              f"Category 5 Hurricane ({mph} mph)"

def _is_hurricane_season() -> bool: return 6 <= _now_et().month <= 11

async def fetch_nhc_storms() -> list | None:
    try:
        data = await _http_get(NHC_STORMS_URL, service="nhc")
        return [s for s in data.get("activeStorms",[]) if s.get("id","").startswith("al")]
    except RuntimeError as e:
        log.warning(f"NHC skipped: {e}")
    except Exception as e:
        log.error(f"NHC fetch failed: {type(e).__name__}: {e}")
    return None

def build_hurricane_embed(storms: list | None) -> dict:
    season = ("🌀 **Atlantic hurricane season is currently active.**"
              if _is_hurricane_season()
              else "🌀 Atlantic hurricane season runs **June 1 – November 30**.")
    if storms is None:
        return {"title":"🌀  Atlantic Hurricane Center",
                "description":"Could not reach the NHC at this time.",
                "color":0x708090,"footer":{"text":"NHC | nhc.noaa.gov"}}
    if not storms:
        return {"title":"🌀  Atlantic Hurricane Center",
                "description":f"No active Atlantic tropical systems.\n{season}",
                "color":0x2ECC71,"footer":{"text":"NHC | nhc.noaa.gov"}}
    fields = []
    for s in storms:
        mph = _knots_to_mph(s.get("intensity",0))
        fields.append({"name":f"🌀 {s.get('name','Unknown')} ({s.get('id','').upper()})",
                       "value":(f"**{_storm_category(s.get('classification',''),mph)}**\n"
                                f"Movement: {s.get('movement','Unknown')}\n"
                                +(f"*{s.get('headline','')}*\n" if s.get("headline") else "")
                                +f"Last update: {s.get('lastUpdate','')}"),
                       "inline":False})
    max_mph = max(_knots_to_mph(s.get("intensity",0)) for s in storms)
    return {"title":"🌀  Active Atlantic Tropical Systems","description":season,
            "color":0xFF4500 if max_mph>=74 else 0xFFB347,"fields":fields,
            "footer":{"text":"NHC | nhc.noaa.gov | Discord Weather Bot"}}

# ---------------------------------------------------------------------------
# NWS 7-day Forecast
# ---------------------------------------------------------------------------
# Per-zip forecast URL cache: {(lat_2dp, lon_2dp): {"url": str, "ts": float}}
_zip_forecast_cache: dict[tuple[float,float], dict] = {}
_ZIP_FORECAST_TTL = 1800   # 30 minutes — NWS updates forecasts ~hourly

async def fetch_forecast(lat: float | None = None, lon: float | None = None,
                         fast: bool = False) -> list | None:
    """fast=True uses shorter retry delays for interactive slash commands."""
    use_lat     = lat if lat is not None else FORECAST_LAT
    use_lon     = lon if lon is not None else FORECAST_LON
    use_default = (lat is None and lon is None)
    bd          = 1.0 if fast else 5.0

    # Resolve forecast URL — default location uses state.json, custom zips
    # use the in-process per-zip cache keyed by (lat, lon) rounded to 2 dp.
    cache_key    = (round(use_lat, 2), round(use_lon, 2))
    forecast_url = None
    if use_default:
        forecast_url = _state.get("forecast_url")
    else:
        entry = _zip_forecast_cache.get(cache_key)
        if entry and time.time() - entry["ts"] < _ZIP_FORECAST_TTL:
            forecast_url = entry["url"]
    try:
        if not forecast_url:
            pts  = await _http_get(f"https://api.weather.gov/points/{use_lat},{use_lon}",
                                   service="nws_forecast", base_delay=bd)
            forecast_url = pts["properties"]["forecast"]
            if use_default:
                _state["forecast_url"] = forecast_url
            else:
                _zip_forecast_cache[cache_key] = {"url": forecast_url, "ts": time.time()}
                log.info(f"Zip forecast URL cached for {cache_key}")
        data = await _http_get(forecast_url, service="nws_forecast", base_delay=bd)
        return data["properties"]["periods"]
    except RuntimeError as e:
        log.warning(f"Forecast skipped: {e}")
    except Exception as e:
        log.error(f"NWS forecast fetch failed (lat={use_lat},lon={use_lon}): {type(e).__name__}: {e}")
        if use_default:
            _state["forecast_url"] = None   # only reset cache for configured location
        else:
            _zip_forecast_cache.pop(cache_key, None)   # drop bad zip cache entry
    return None

def build_forecast_embed(periods: list, title_override: str | None = None) -> dict:
    fields = []
    for p in periods[:14]:
        short  = p.get("shortForecast","")
        prefix = "☀️" if p.get("isDaytime",True) else "🌙"
        fields.append({"name":f"{prefix} {p.get('name','')}",
                       "value":f"{_condition_emoji(short)} {short} · {p.get('temperature','?')}°F",
                       "inline":True})
    return {"title":title_override or f"📅  7-Day Forecast — {LOCATION_NAME}",
            "color":0x5865F2,"fields":fields,
            "footer":{"text":"NWS via api.weather.gov | Discord Weather Bot"}}

# ---------------------------------------------------------------------------
# Weekly Summary
# ---------------------------------------------------------------------------
def build_weekly_summary_embed(periods, aqi_data, tides, active_alerts: int) -> dict:
    now_et = _now_et()
    fields = []
    if periods:
        for p in [p for p in periods if p.get("isDaytime",True)][:7]:
            short = p.get("shortForecast","")
            fields.append({"name":p.get("name",""),
                           "value":f"{_condition_emoji(short)} {short} · {p.get('temperature','?')}°F",
                           "inline":True})
    else:
        fields.append({"name":"Forecast","value":"Unavailable","inline":False})
    if tides:
        by_date: dict[str,list] = defaultdict(list)
        for p in tides:
            if p.get("type")=="H": by_date[p["t"][:10]].append(p)
        tide_lines = []
        for date_str in sorted(by_date)[:7]:
            try: label = datetime.strptime(date_str,"%Y-%m-%d").strftime("%a %b %d")
            except Exception: label = date_str
            highs = ", ".join(
                datetime.strptime(h["t"],"%Y-%m-%d %H:%M").strftime("%I:%M %p").lstrip("0")
                +f" ({float(h['v']):.1f} ft)" for h in by_date[date_str])
            tide_lines.append(f"**{label}**: {highs}")
        if tide_lines:
            fields.append({"name":f"🌊 High Tides — {TIDE_STATION_NAME}",
                           "value":"\n".join(tide_lines),"inline":False})
    if aqi_data:
        best = max(aqi_data,key=lambda x:x.get("AQI",0))
        cat  = best.get("Category",{})
        fields.append({"name":"🌫️ Current Air Quality",
                       "value":f"{_aqi_dot(cat.get('Number',1))} {cat.get('Name','?')} (AQI {best.get('AQI','—')})",
                       "inline":True})
    fields.append({"name":"⚠️ Active NWS Alerts",
                   "value":f"{active_alerts} alert{'s' if active_alerts!=1 else ''} for {LOCATION_NAME}",
                   "inline":True})
    return {"title":f"📊  Weekly Outlook — {LOCATION_NAME}",
            "description":f"*{now_et.strftime('%b %d')} – {(now_et+timedelta(days=6)).strftime('%b %d, %Y')}*",
            "color":0x5865F2,"fields":fields,
            "footer":{"text":"NWS · NOAA Tides · EPA AirNow | Discord Weather Bot"}}

# ---------------------------------------------------------------------------
# Async tasks
# ---------------------------------------------------------------------------
async def _update_conditions():
    embed_dict = await _fetch_and_build_conditions()
    if not embed_dict: return

    msg_id = _state.get("conditions_message_id")
    msg_ts = _state.get("conditions_message_ts",0.0)
    repost = (time.time()-msg_ts) >= (CONDITIONS_REPOST_HOURS*3600)

    if msg_id and _channel and not repost:
        try:
            msg = await _channel.fetch_message(int(msg_id))
            await msg.edit(embed=_embed(embed_dict))
            log.info("Conditions message updated (edit)")
            return
        except discord.NotFound:
            log.info("Conditions message deleted; posting fresh")
            _state["conditions_message_id"] = None
        except Exception as e:
            log.error(f"Conditions edit failed: {type(e).__name__}: {e}")

    if msg_id and repost and _channel:
        try:
            old = await _channel.fetch_message(int(msg_id))
            try: await old.unpin()
            except Exception: pass
            await old.delete()
            log.info("Old conditions message deleted for repost")
        except Exception: pass

    msg = await _send(embed_dict, view=ConditionsRefreshView())
    if msg:
        _state["conditions_message_id"] = str(msg.id)
        _state["conditions_message_ts"] = time.time()
        if PIN_CONDITIONS:
            try:
                await msg.pin()
                log.info(f"Conditions message pinned (msg_id={msg.id})")
            except discord.Forbidden:
                log.warning("Could not pin: missing Manage Messages permission")
            except Exception as e:
                log.warning(f"Could not pin: {e}")
        save_state(_state)

async def _check_aqi_alert():
    if not AIRNOW_KEY: return
    if time.time()-_state.get("last_aqi_check_ts",0) < AQI_CACHE_SECS: return
    _state["last_aqi_check_ts"] = int(time.time())
    data = await fetch_aqi()
    if not data: return
    max_cat  = max(i.get("Category",{}).get("Number",1) for i in data)
    last_cat = _state.get("last_aqi_category",1)
    if max_cat >= AQI_THRESHOLD and last_cat < AQI_THRESHOLD:
        await _send(build_aqi_alert_embed(data,improving=False))
        _event(f"🟠  AQI ALERT: category {max_cat} ({_AQI_LABEL.get(max_cat,'?')})",
               f"threshold={AQI_THRESHOLD}, prev={last_cat}")
    elif max_cat < AQI_THRESHOLD and last_cat >= AQI_THRESHOLD:
        await _send(build_aqi_alert_embed(data,improving=True))
        _event(f"🟢  AQI improved: category {max_cat} ({_AQI_LABEL.get(max_cat,'?')})",
               f"threshold={AQI_THRESHOLD}, prev={last_cat}")
    _state["last_aqi_category"] = max_cat

async def _task_alerts() -> bool:
    if _channel is None: log.error("No channel; skipping alerts"); return False
    log.info("Checking NWS alerts")
    all_alerts = await fetch_alerts()
    if all_alerts is None:
        log.warning("Alert fetch failed — skipping this cycle (no clears/prunes)")
        return False
    southern   = [f for f in all_alerts if _in_coverage(f)]
    active_ids = {f.get("id","") for f in southern}
    log.info(f"  {len(all_alerts)} state total -> {len(southern)} in coverage active")

    posted  = _state.setdefault("posted_alerts",{})
    any_new = False

    for feature in southern:
        aid      = feature.get("id","")
        props    = feature.get("properties",{})
        if not aid or aid in posted: continue
        msg_type = props.get("messageType","Alert")
        refs     = props.get("references",[])
        area     = _coverage_area_str(props.get("areaDesc","")) or props.get("areaDesc","")[:200]
        view     = _alert_view(feature)
        event    = props.get("event","Weather Alert")
        expires  = _fmt_time(props.get("expires")) if props.get("expires") else "N/A"

        if msg_type == "Cancel":
            old_aid = _find_ref_in_posted(refs,posted)
            if old_aid and not posted[old_aid].get("cleared"):
                posted[old_aid]["superseded_by"] = aid
                if not posted[old_aid].get("suppressed"):  # no clear notif for suppressed
                    await _send_cleared(posted[old_aid].get("message_id"),
                                        posted[old_aid].get("event","Weather Alert"),
                                        posted[old_aid].get("area",LOCATION_NAME),cancelled=True)
                posted[old_aid]["cleared"] = True; posted[old_aid]["cleared_ts"] = time.time()
            posted[aid] = {"ts":time.time(),"cleared":True,"event":"_cancel"}
            save_state(_state)
            continue

        if msg_type == "Update":
            old_aid     = _find_ref_in_posted(refs,posted)
            orig_msg_id = posted[old_aid].get("message_id") if old_aid else None
            if old_aid: posted[old_aid]["superseded_by"] = aid
            # Apply suppression to updates too; a watch upgrade to warning breaks through
            if _alert_is_suppressed(event):
                posted[aid] = {"ts":time.time(),"message_id":None,"event":event,
                               "area":area,"cleared":False,"suppressed":True,"update_of":old_aid}
                log.info(f"  Alert update suppressed ({_TIER_LABEL.get(_alert_tier(event))}): {event}")
                continue
            embed = build_update_embed(feature); msg = None
            if orig_msg_id:
                try:
                    orig = await _channel.fetch_message(int(orig_msg_id))
                    kw = {"embed":_embed(embed),"reference":orig,"mention_author":False}
                    if view: kw["view"] = view
                    msg = await _channel.send(**kw)
                except Exception as e: log.error(f"Update reply failed: {type(e).__name__}: {e}")
            if msg is None: msg = await _send(embed,view=view)
            posted[aid] = {"ts":time.time(),"message_id":str(msg.id) if msg else orig_msg_id,
                           "event":event,"area":area,"cleared":False,"update_of":old_aid}
            _event(f"🔄  ALERT UPDATED: {event} — {area}",
                   f"expires={expires}, msg_id={posted[aid]['message_id']}")
            any_new = True; continue

        # New alert — check suppression threshold before posting
        if _alert_is_suppressed(event):
            posted[aid] = {"ts":time.time(),"message_id":None,"event":event,
                           "area":area,"cleared":False,"suppressed":True}
            _event(f"🔇  ALERT SUPPRESSED ({_TIER_LABEL.get(_alert_tier(event))}): {event} — {area}",
                   f"threshold={ALERT_POST_THRESHOLD}, id={aid}")
            continue

        msg = await _send(build_alert_embed(feature),view=view)
        posted[aid] = {"ts":time.time(),"message_id":str(msg.id) if msg else None,
                       "event":event,"area":area,"cleared":False}
        _event(f"⚠️  NEW ALERT: {event} — {area}",
               f"expires={expires}, msg_id={posted[aid]['message_id']}, id={aid}")
        log.info(f"    Headline: {props.get('headline','')}")
        any_new = True

    cleared_any = False
    for aid, info in list(posted.items()):
        if (not info.get("cleared") and not info.get("suppressed")
                and not info.get("superseded_by") and aid not in active_ids):
            await _send_cleared(info.get("message_id"),info.get("event","Weather Alert"),
                                info.get("area",LOCATION_NAME))
            posted[aid]["cleared"] = True; posted[aid]["cleared_ts"] = time.time()
            cleared_any = True

    # Prune resolved entries (cleared, superseded, OR suppressed) older than
    # 48 h, keeping anything NWS still lists as active.  Pre-v2.7.1 only
    # cleared=True entries were pruned, but superseded and suppressed entries
    # never get cleared, so state.json grew without bound.
    cutoff = time.time()-48*3600
    before = len(_state["posted_alerts"])
    _state["posted_alerts"] = {
        aid: info for aid,info in posted.items()
        if aid in active_ids
        or not ((info.get("cleared") or info.get("superseded_by")
                 or info.get("suppressed"))
                and info.get("ts",0) < cutoff)
    }
    if cleared_any or len(_state["posted_alerts"]) != before:
        save_state(_state)   # persist clears and prunes immediately
    return any_new

async def _post_weekly_summary():
    log.info("Posting weekly weather summary")
    periods    = await fetch_forecast()
    aqi_data   = await fetch_aqi() if AIRNOW_KEY else None
    tides      = await fetch_tides(7)
    all_alerts = await fetch_alerts() or []
    active_local = sum(1 for f in all_alerts if _in_coverage(f))
    await _send(build_weekly_summary_embed(periods,aqi_data,tides,active_local))
    _event("📊  Weekly summary posted")

# ---------------------------------------------------------------------------
# Scheduler  (self-healing: exceptions logged but loop continues)
# ---------------------------------------------------------------------------
def _presence_text() -> str:
    """
    Build the single composite status shown under the bot's name, e.g.
        "Partly Cloudy · 72°F · 2 alerts"
        "58°F · No Alerts"
        "⚠️ 1 alert · watching the skies"
    Each segment is included only when its data is available, joined with a
    dot separator.  Alerts are always represented (count, or "No Alerts") once
    the bot has run an alert check, since that is the operationally useful bit.
    """
    parts: list[str] = []

    sky = _state.get("last_sky")
    if isinstance(sky, str) and sky:
        parts.append(sky)

    temp = _state.get("last_temp_f")
    if isinstance(temp, (int, float)):
        parts.append(f"{round(temp)}°F")

    # Alert segment: only once at least one alert check has completed, so we
    # don't claim "No Alerts" before we actually know.
    if _state.get("last_alert_check_ts"):
        active = sum(1 for a in _state.get("posted_alerts", {}).values()
                     if not (a.get("cleared") or a.get("superseded_by")))
        if active:
            parts.append(f"⚠️ {active} alert{'s' if active != 1 else ''}")
        else:
            parts.append("No Alerts")

    if not parts:
        # Nothing fetched yet (very first moments after startup)
        return PRESENCE_FALLBACK
    text = " · ".join(parts)
    # Discord caps activity text at 128 chars; "Watching " is client-side.
    return text[:128]

async def _rotate_presence():
    """
    Keep the bot's 'Watching …' status in sync with current conditions.

    Runs independently of the weather scheduler so a presence hiccup can never
    disturb posting, and vice versa.  Refreshes every PRESENCE_ROTATE_SECS and
    also immediately whenever the composite text changes (so a new alert shows
    up without waiting for the next tick would require event hooks; here we
    simply poll on a short-enough interval).  A failure is non-fatal and just
    logged.
    """
    log.info(f"Presence updates started (every {PRESENCE_ROTATE_SECS}s)")
    last = None
    while True:
        try:
            text = _presence_text()
            if text != last:
                await bot.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.watching, name=text))
                last = text
        except Exception as e:
            log.warning(f"Presence update failed: {type(e).__name__}: {e}")
        await asyncio.sleep(PRESENCE_ROTATE_SECS)

async def _scheduler():
    log.info("Scheduler started")
    cond_interval = CONDITIONS_UPDATE_MINS*60

    await _update_conditions(); _state["last_conditions_update_ts"] = int(time.time())
    await _task_alerts();       _state["last_alert_check_ts"]       = int(time.time())
    save_state(_state)

    while True:
        await asyncio.sleep(60)
        try:
            now_ts = time.time(); now_et = _now_et()

            if now_ts-_state.get("last_conditions_update_ts",0) >= cond_interval:
                await _update_conditions()
                await _check_aqi_alert()
                _state["last_conditions_update_ts"] = int(now_ts)
                save_state(_state)

            if now_ts-_state.get("last_alert_check_ts",0) >= ALERT_INTERVAL_SECS:
                changed = await _task_alerts()
                _state["last_alert_check_ts"] = int(now_ts)
                if changed: save_state(_state)

            if now_et.weekday()==WEEKLY_DAY and now_et.hour==WEEKLY_HOUR:
                week_key = f"weekly:{now_et.strftime('%Y-%W')}"
                posted_w = _state.setdefault("weekly_posted",[])
                if week_key not in posted_w:
                    await _post_weekly_summary()
                    posted_w.append(week_key)
                    _state["weekly_posted"] = posted_w[-10:]
                    save_state(_state)

        except Exception as e:
            log.error(f"Scheduler loop error (continuing): {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot     = _BotClient(intents=intents)
tree    = app_commands.CommandTree(bot)

@tree.error
async def _on_app_command_error(interaction: discord.Interaction,
                                error: app_commands.AppCommandError):
    """Catch all unhandled slash-command errors and return a visible reply."""
    log.error(f"Unhandled command error in /{interaction.command and interaction.command.name}: "
              f"{error}", exc_info=True)
    msg = "❌  Something went wrong processing that command — please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass   # interaction may have already expired

def _fmt_weekly_when() -> str:
    """Human 'Sunday 8 AM ET' string for the configured weekly-summary slot, so
    /help reflects weekly_summary_day/hour instead of a hardcoded schedule."""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ampm = "AM" if WEEKLY_HOUR < 12 else "PM"
    h12  = WEEKLY_HOUR % 12 or 12
    return f"{days[WEEKLY_DAY]} {h12} {ampm} ET"

_HELP_EMBED = {
    "title": "🌤️  Discord Weather Bot",
    "description": (
        f"Live conditions from **{PWS_STATION_ID}** in **{LOCATION_NAME}**.\n"
        f"Conditions updated every **{CONDITIONS_UPDATE_MINS} min** · "
        f"Alerts polled every **{ALERT_INTERVAL_SECS//60} min**\n"
    ),
    "color": 0x1E90FF,
    "fields": [
        {"name":"/conditions","value":"Latest PWS reading. Optional: `station_id` for any Xweather station.","inline":False},
        {"name":"/alerts",    "value":f"Active NWS alerts for {LOCATION_NAME}.","inline":False},
        {"name":"/forecast",  "value":"NWS 7-day forecast. Optional: `zipcode` for any US zip.","inline":False},
        {"name":"/tides",     "value":f"High/low tides — {TIDE_STATION_NAME}. Optional: `station_id` for any NOAA CO-OPS station.","inline":False},
        {"name":"/aqi",       "value":"EPA AirNow air quality report." +
                               ("" if AIRNOW_KEY else " *(API key not configured)*"),"inline":False},
        {"name":"/hurricane", "value":"NHC Atlantic tropical storm status.","inline":False},
        {"name":"/radar",     "value":f"Live NWS {RADAR_STATION} radar for {LOCATION_NAME} (opens in browser).","inline":False},
        {"name":"/status",    "value":"Bot operational status, last update times, circuit-breaker health.","inline":False},
        {"name":"/help",      "value":"Show this message.","inline":False},
        {"name":"ℹ️ About",    "value":f"Discord Weather Bot v{__version__} by {__author__} · [Source on GitHub]({SOURCE_URL})","inline":False},
    ],
    "footer":{"text":f"Weekly outlook auto-posts {_fmt_weekly_when()}"},
}


_ready_once = False
_scheduler_started = False

async def _sync_commands() -> None:
    """
    Register slash commands in exactly ONE scope.

    Discord stores global and guild command sets separately and shows BOTH in
    the command picker, so syncing the same commands to both scopes makes
    every command appear twice — the pre-v2.7.6 behavior, which registered
    globally and then copied the same set into the guild.  We now pick one
    scope and push an EMPTY set to the other, which also cleans up duplicates
    left behind by an earlier version on the first run after upgrading.

    guild scope : registers instantly, but commands do NOT work in DMs.
    global scope: works everywhere including DMs, up to ~1h to propagate.
    """
    mode = COMMAND_SYNC
    if mode == "auto":
        mode = "guild" if DISCORD_GUILD_ID else "global"

    if mode == "guild" and DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        # Drop global registrations so they cannot show up as duplicates
        tree.clear_commands(guild=None)
        await tree.sync()
        log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}: "
                 f"{len(synced)} commands (instant)")
        log.info("Global command set cleared (prevents duplicate entries); "
                 "note guild-scoped commands are not available in DMs")
    else:
        if DISCORD_GUILD_ID:
            # Clear stale guild copies left by a previous guild-scoped sync
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)
            log.info(f"Guild {DISCORD_GUILD_ID} command set cleared "
                     f"(prevents duplicate entries)")
        synced = await tree.sync()
        log.info(f"Slash commands synced globally: {len(synced)} commands "
                 f"(DM access enabled; may take up to ~1h to appear)")

async def _resolve_channel_with_retry(attempts: int = 4) -> None:
    """
    Resolve the posting channel, retrying a few times with backoff.
    A single transient API hiccup at startup previously left _channel None
    for the life of the process, silently disabling the bot until someone
    noticed and restarted it.
    """
    for i in range(attempts):
        await _resolve_channel()
        if _channel is not None:
            if i:
                log.info(f"Channel resolved on attempt {i+1}")
            return
        if i < attempts - 1:
            delay = 5 * (2 ** i)
            log.warning(f"Channel unresolved — retry {i+1}/{attempts-1} in {delay}s")
            await asyncio.sleep(delay)

@bot.event
async def on_ready():
    global _state, _start_time, _ready_once, _scheduler_started

    # discord.py re-dispatches READY whenever a session cannot be RESUMED, so
    # on_ready may run several times in one process.  One-time setup (state
    # load, persistent view, command sync) must not repeat — re-running it
    # previously spawned a SECOND scheduler, double-posting every conditions
    # update and every alert.  Channel resolution is deliberately left
    # retryable so a reconnect can recover a startup that failed to resolve.
    if _scheduler_started:
        log.info("Reconnected (READY re-dispatched); scheduler already running")
        return

    if not _ready_once:
        _ready_once = True
        _state      = load_state()
        _start_time = time.time()
        bot.add_view(ConditionsRefreshView())
        await _sync_commands()
        if PRESENCE_ENABLED:
            asyncio.create_task(_rotate_presence())
    else:
        log.info("READY re-dispatched; retrying channel resolution")

    await _resolve_channel_with_retry()
    save_state(_state)

    log.info(f"Bot online: {bot.user} (ID: {bot.user.id})")
    if _channel is None:
        print("\n⚠️  WARNING: posting channel could not be resolved — "
              "scheduler will NOT start.\n"
              "   Set discord_channel_id in config.json and restart.\n")
        log.error("Channel unresolved; scheduler not started")
    else:
        _scheduler_started = True
        asyncio.create_task(_scheduler())

    ch_name   = f"#{_channel.name}" if _channel else "unresolved"
    astral_ok = "enabled" if _ASTRAL_OK else "disabled (pip install astral)"
    print(f"\n{'─'*62}")
    print(f"  Discord Weather Bot v{__version__}|  {LOCATION_NAME}")
    print(f"  Station   : {PWS_STATION_ID}")
    print(f"  Channel   : {ch_name}")
    print(f"  Guild     : {DISCORD_GUILD_ID or 'global sync'}")
    print(f"  Cond.     : edit every {CONDITIONS_UPDATE_MINS} min, "
          f"repost every {CONDITIONS_REPOST_HOURS} h"
          +(" (pinned + 🔄 button)" if PIN_CONDITIONS else " (🔄 button)"))
    print(f"  Alerts    : poll every {ALERT_INTERVAL_SECS//60} min")
    print(f"  Coverage  : {_coverage_label()} "
          f"({len(_COVERAGE_ZONES)} zone{'s' if len(_COVERAGE_ZONES)!=1 else ''})")
    print(f"  Tides     : NOAA {TIDE_STATION_ID} ({TIDE_STATION_NAME})")
    print(f"  AQI       : {'AirNow enabled (threshold cat >= '+str(AQI_THRESHOLD)+')' if AIRNOW_KEY else 'AirNow disabled (no API key)'}")
    print(f"  Sunrise   : {astral_ok}")
    print(f"  Weekly    : {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][WEEKLY_DAY]} {WEEKLY_HOUR:02d}:00 ET")
    print(f"  Logs      : {LOG_FILE}  (prev: {str(LOG_FILE)+'.1'})")
    print(f"{'─'*62}\n")


@tree.command(name="help", description="Show available weather bot commands")
async def slash_help(interaction: discord.Interaction):
    _log_cmd(interaction, "help")
    await interaction.response.send_message(
        embed=_embed(_HELP_EMBED),
        view=LinkButtonView([
            ("View on GitHub",  "🐙", SOURCE_URL),
            ("Report an issue", "🐞", f"{SOURCE_URL}/issues"),
        ]),
        ephemeral=True)


@tree.command(name="conditions", description="Get weather conditions from a PWS station")
@app_commands.describe(station_id="Optional PWS station ID — defaults to configured station")
async def slash_conditions(interaction: discord.Interaction,
                           station_id: str | None = None):
    _log_cmd(interaction, "conditions", f"station_id={station_id or 'default'}")
    await interaction.response.defer()
    embed_dict = await _fetch_and_build_conditions(station_id, fast=True)
    if embed_dict:
        await interaction.followup.send(
            embed=_embed(embed_dict),
            view=LinkButtonView([("View on PWSweather", "📡", _pws_station_url(station_id))]))
    else:
        hint = f" (station `{station_id.upper()}`)" if station_id else ""
        await interaction.followup.send(
            f"❌  Could not reach the weather station{hint} — check the ID and try again.")


# ---------------------------------------------------------------------------
# Shared responders — used by both slash commands and the conditions-message
# dashboard buttons, so each has exactly one implementation.  `ephemeral`
# controls visibility: button clicks reply only to the clicker (keeping the
# channel clean), while slash commands reply publicly.
# ---------------------------------------------------------------------------
async def _respond_alerts(interaction: discord.Interaction, *, ephemeral: bool):
    all_alerts = await fetch_alerts(fast=True)
    if all_alerts is None:
        await interaction.followup.send(
            "❌  Could not reach the NWS alerts API — try again in a moment.",
            ephemeral=ephemeral)
        return
    southern = [f for f in all_alerts if _in_coverage(f)]
    if not southern:
        await interaction.followup.send(
            f"✅  No active NWS alerts for {LOCATION_NAME} right now.",
            ephemeral=ephemeral)
        return
    suppressed = {f.get("properties",{}).get("event","") for f in southern
                  if _alert_is_suppressed(f.get("properties",{}).get("event",""))}
    if len(southern) == 1:
        feature = southern[0]
        event   = feature.get("properties",{}).get("event","")
        note    = (f"\n*Note: this {_TIER_LABEL.get(_alert_tier(event),'alert')} is "
                   f"below your `alert_post_threshold` ('{ALERT_POST_THRESHOLD}') "
                   f"and was not auto-posted to the channel.*"
                   if event in suppressed else "")
        embed_d = build_alert_embed(feature)
        if note: embed_d["description"] = (embed_d.get("description","") + note)[:4096]
        await interaction.followup.send(
            embed=_embed(embed_d), view=_alert_view(feature), ephemeral=ephemeral)
    else:
        pfx = (f"*Showing all {len(southern)} alerts — select one for full details.*"
               if len(southern) > 5 else None)
        footer_note = (f" | {len(suppressed)} below '{ALERT_POST_THRESHOLD}' threshold — not auto-posted"
                       if suppressed else "")
        embed_d = build_alerts_summary_embed(southern, suppressed)
        embed_d["footer"] = {"text": embed_d.get("footer",{}).get("text","") + footer_note}
        await interaction.followup.send(
            content=pfx, embed=_embed(embed_d),
            view=AlertSelectView(southern), ephemeral=ephemeral)

async def _respond_forecast(interaction: discord.Interaction, *, ephemeral: bool,
                            zipcode: str | None = None):
    lat = lon = None
    location_label = LOCATION_NAME
    if zipcode:
        coords = await geocode_zip(zipcode.strip())
        if coords is None:
            await interaction.followup.send(
                f"❌  Could not look up zip code `{zipcode}` — verify it's a valid US zip.",
                ephemeral=ephemeral)
            return
        lat, lon = coords; location_label = f"Zip {zipcode}"
    periods = await fetch_forecast(lat, lon, fast=True)
    if periods:
        title = f"📅  7-Day Forecast — {location_label}" if zipcode else None
        gov_lat = lat if lat is not None else FORECAST_LAT
        gov_lon = lon if lon is not None else FORECAST_LON
        gov_url = f"https://forecast.weather.gov/MapClick.php?lat={gov_lat}&lon={gov_lon}"
        await interaction.followup.send(
            embed=_embed(build_forecast_embed(periods, title)),
            view=LinkButtonView([("Full forecast on weather.gov", "🌐", gov_url)]),
            ephemeral=ephemeral)
    else:
        await interaction.followup.send(
            "❌  Could not fetch the NWS forecast — try again in a moment.",
            ephemeral=ephemeral)

async def _respond_radar(interaction: discord.Interaction, *, ephemeral: bool):
    buttons = [
        (f"{RADAR_STATION} Standard", "🌧️",
         f"https://radar.weather.gov/station/{RADAR_STATION}/standard"),
        (f"{RADAR_STATION} Loop",     "🔄",
         f"https://radar.weather.gov/station/{RADAR_STATION}/loop"),
        ("Regional",                  "🗺️",
         f"https://radar.weather.gov/region/{RADAR_REGION}/standard"),
    ]
    embed = {
        "title":       f"📡  NWS Radar — {LOCATION_NAME}",
        "description": (f"**{RADAR_STATION}** ({RADAR_STATION_NAME}) is the "
                        f"primary radar covering this area.\n"
                        f"Use the buttons for the interactive viewer."),
        "color":       0x1E90FF,
        "fields": [{"name": "Coverage", "value": _coverage_label(), "inline": False}],
        "footer": {"text": "NWS radar.weather.gov | Discord Weather Bot"},
    }
    img = await fetch_radar_image() if RADAR_ATTACH_IMAGE else None
    if img:
        fname = f"radar_{RADAR_STATION}.gif"
        embed["image"] = {"url": f"attachment://{fname}"}
        await interaction.followup.send(
            embed=_embed(embed),
            file=discord.File(io.BytesIO(img), filename=fname),
            view=LinkButtonView(buttons), ephemeral=ephemeral)
        return
    if RADAR_ATTACH_IMAGE:
        embed["footer"] = {"text": "NWS radar.weather.gov | live image "
                                   "unavailable — use the buttons below"}
    await interaction.followup.send(
        embed=_embed(embed), view=LinkButtonView(buttons), ephemeral=ephemeral)


@tree.command(name="alerts", description=f"Check active NWS alerts for {LOCATION_NAME}")
async def slash_alerts(interaction: discord.Interaction):
    _log_cmd(interaction, "alerts")
    await interaction.response.defer()
    await _respond_alerts(interaction, ephemeral=False)


@tree.command(name="forecast",
              description=f"NWS 7-day forecast — defaults to {LOCATION_NAME}, or enter a US zip code")
@app_commands.describe(zipcode="Optional US zip code (e.g. 08330)")
async def slash_forecast(interaction: discord.Interaction,
                         zipcode: str | None = None):
    _log_cmd(interaction, "forecast", f"zip={zipcode or 'default'}")
    await interaction.response.defer()
    await _respond_forecast(interaction, ephemeral=False, zipcode=zipcode)


@tree.command(name="tides",
              description=f"High/low tide schedule — defaults to {TIDE_STATION_NAME}")
@app_commands.describe(station_id="Optional NOAA CO-OPS station ID (e.g. 8536110 for Cape May)")
async def slash_tides(interaction: discord.Interaction,
                      station_id: str | None = None):
    _log_cmd(interaction, "tides", f"station_id={station_id or 'default'}")
    await interaction.response.defer()
    sid   = station_id.strip() if station_id else None
    preds = await fetch_tides(3, sid)   # 3 days: today + 2 more
    if preds:
        sname = f"Station {sid}" if sid else None
        embed = build_tides_embed(preds, sid, sname)
        view  = LinkButtonView([("NOAA Tide Station","📡",
                                 f"https://tidesandcurrents.noaa.gov/stationhome.html?id={sid or TIDE_STATION_ID}")])
        await interaction.followup.send(embed=_embed(embed), view=view)
    else:
        hint = f" for station `{sid}`" if sid else ""
        await interaction.followup.send(
            f"❌  Could not fetch tide data{hint}. "
            "Find NOAA station IDs at <https://tidesandcurrents.noaa.gov/map/>.")


@tree.command(name="aqi", description="Current EPA AirNow air quality report with forecast")
async def slash_aqi(interaction: discord.Interaction):
    _log_cmd(interaction, "aqi")
    await interaction.response.defer()
    if not AIRNOW_KEY:
        await interaction.followup.send(
            "❌  AirNow API key not configured. "
            "Register free at <https://docs.airnowapi.org/> and add `airnow_api_key` to config.json.")
        return
    _aqi_obs_cache["ts"] = 0.0   # force fresh fetch
    obs      = await fetch_aqi()
    forecast = await fetch_aqi_forecast()
    if obs:
        await interaction.followup.send(
            embed=_embed(build_aqi_embed(obs,forecast)),
            view=LinkButtonView([("AirNow Website","🌫️","https://www.airnow.gov/")]))
    else:
        await interaction.followup.send("❌  Could not fetch AQI data — try again in a moment.")


@tree.command(name="hurricane", description="NHC Atlantic tropical storm and hurricane status")
async def slash_hurricane(interaction: discord.Interaction):
    _log_cmd(interaction, "hurricane")
    await interaction.response.defer()
    storms  = await fetch_nhc_storms()
    buttons = [("NHC Website","🌀","https://www.nhc.noaa.gov/")]
    for s in (storms or [])[:2]:
        name = s.get("name","Storm")
        adv  = s.get("publicAdvisory",{}).get("url","")
        disc = s.get("discussion",{}).get("url","")
        fcst = s.get("forecast",{}).get("url","")
        if adv:  buttons.append((f"{name} Advisory",  "📋", adv))
        if disc: buttons.append((f"{name} Discussion","📝", disc))
        if fcst: buttons.append((f"{name} Track",     "🗺️", fcst))
    await interaction.followup.send(
        embed=_embed(build_hurricane_embed(storms)),
        view=LinkButtonView(buttons))



@tree.command(name="radar",
              description=f"NWS radar for {LOCATION_NAME} — live image and browser links")
async def slash_radar(interaction: discord.Interaction):
    _log_cmd(interaction, "radar")
    await interaction.response.defer()
    await _respond_radar(interaction, ephemeral=False)

@tree.command(name="status", description="Bot operational status and service health")
async def slash_status(interaction: discord.Interaction):
    _log_cmd(interaction, "status")
    await interaction.response.defer(ephemeral=True)

    now_ts     = time.time()
    uptime     = timedelta(seconds=int(now_ts-_start_time))
    last_cond  = _state.get("last_conditions_update_ts",0)
    last_alrt  = _state.get("last_alert_check_ts",0)
    cond_msg   = _state.get("conditions_message_id")
    active_alt = [i for i in _state.get("posted_alerts",{}).values() if not i.get("cleared")]
    cond_next  = max(0, CONDITIONS_UPDATE_MINS*60 - (now_ts-last_cond))
    alrt_next  = max(0, ALERT_INTERVAL_SECS      - (now_ts-last_alrt))
    aqi_cat    = _state.get("last_aqi_category",1)

    def _ago(ts):
        if not ts: return "never"
        s = int(now_ts-ts)
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        return       f"{s//3600}h {(s%3600)//60}m ago"

    cb_lines = []
    for svc, st in sorted(_circuit.items()):
        if st["failures"] > 0:
            if st["until"] > now_ts:
                rem = int(st["until"]-now_ts)//60
                cb_lines.append(f"⚠️ **{svc}**: {st['failures']} failures, {rem} min backoff remaining")
            else:
                cb_lines.append(f"⚡ **{svc}**: {st['failures']} failures (not yet recovered)")

    start_str = (datetime.fromtimestamp(_start_time, _TZ).strftime("%b %d %H:%M %Z")
                 if _start_time else "unknown")

    # Clickable jump link to pinned conditions message
    guild_id   = DISCORD_GUILD_ID or (interaction.guild_id or "@me")
    ch_id      = _state.get("channel_id","")
    if cond_msg and ch_id:
        msg_link = f"https://discord.com/channels/{guild_id}/{ch_id}/{cond_msg}"
        cond_msg_val = f"[Jump to message]({msg_link})"
    else:
        cond_msg_val = "None"

    # Active alert list (name + area, one per line)
    if active_alt:
        alert_lines = [
            f"{_ALERT_EMOJIS.get(i.get('event',''),'⚠️')} **{i.get('event','Alert')}**\n"
            f"  ↳ {i.get('area',LOCATION_NAME)[:60]}"
            for i in active_alt
        ]
        alert_val = "\n".join(alert_lines)[:1024]
    else:
        alert_val = "None"

    fields = [
        {"name":"⏱️ Uptime",          "value":str(uptime),                                "inline":True},
        {"name":"📍 Channel",         "value":f"<#{ch_id}>",                              "inline":True},
        {"name":"📌 Conditions msg",  "value":cond_msg_val,                               "inline":True},
        {"name":"🗺️ Coverage",        "value":f"{_coverage_label()} · "
                                              f"{len(_COVERAGE_ZONES)} zone"
                                              f"{'s' if len(_COVERAGE_ZONES)!=1 else ''} · "
                                              f"{RADAR_STATION} radar",     "inline":False},
        {"name":"🌡️ Conditions",      "value":f"{_ago(last_cond)}\nNext ~{int(cond_next//60)}m","inline":True},
        {"name":"⚠️ Alert check",     "value":f"{_ago(last_alrt)}\nNext ~{int(alrt_next//60)}m","inline":True},
        {"name":"🚨 Active alerts",   "value":alert_val,                                  "inline":False},
        {"name":"🌫️ Last AQI",        "value":f"{_aqi_dot(aqi_cat)} {_AQI_LABEL.get(aqi_cat,'?')}","inline":True},
        {"name":"🌅 Sunrise/sunset",  "value":"Enabled" if _ASTRAL_OK else "Disabled",    "inline":True},
        {"name":"🌐 HTTP (aiohttp)",  "value":"Native async",                             "inline":True},
        {"name":"📶 Circuit breakers","value":"\n".join(cb_lines) if cb_lines else "✅ All services nominal","inline":False},
    ]
    embed = {"title":f"📊  Discord Weather Bot — Status · {LOCATION_NAME}",
             "color":0x57F287 if not cb_lines else 0xFFD700,
             "fields":fields,
             "footer":{"text":f"v{__version__} | Started {start_str}"}}
    await interaction.followup.send(embed=_embed(embed), ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if DISCORD_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        sys.exit("ERROR: discord_bot_token not set in config.json.")
    log.info(f"Discord Weather Bot v{__version__} starting")
    try:
        bot.run(DISCORD_BOT_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord login failed: token rejected")
        sys.exit("\nERROR: Discord rejected the bot token.\n"
                 "       Check discord_bot_token in config.json — it must be a\n"
                 "       BOT token from the Developer Portal's Bot tab (not a\n"
                 "       client secret, and not an OAuth code). Regenerating the\n"
                 "       token invalidates the old one, so update config.json too.")
    except discord.PrivilegedIntentsRequired:
        log.error("Discord login failed: privileged intents required")
        sys.exit("\nERROR: Discord requires privileged intents this bot has not\n"
                 "       been granted. Enable them on the Bot tab of the\n"
                 "       Developer Portal, or run an unmodified copy of this bot\n"
                 "       (it only needs default intents).")
    except aiohttp.ClientConnectorError as e:
        log.error(f"Could not reach Discord at startup: {type(e).__name__}: {e}")
        sys.exit(f"\nERROR: could not connect to Discord ({e}).\n"
                 f"       Check this host's network connection and retry.")
    except discord.HTTPException as e:
        log.error(f"Discord HTTP error at startup: {e.status} {e.text}")
        sys.exit(f"\nERROR: Discord returned HTTP {e.status} during login.\n"
                 f"       {e.text or 'No detail provided.'}\n"
                 f"       If this is a 5xx, Discord may be having an outage — "
                 f"retry shortly.")
    except KeyboardInterrupt:
        log.info("Shutdown requested (KeyboardInterrupt)")
    except Exception as e:
        # Last resort: full traceback to the log file, one clean line to the
        # console, so an unexpected startup failure is diagnosable without
        # dumping a stack trace at whoever is running the bot.
        log.exception("Fatal error during startup")
        sys.exit(f"\nERROR: unexpected startup failure: {type(e).__name__}: {e}\n"
                 f"       Full traceback written to {LOG_FILE}")