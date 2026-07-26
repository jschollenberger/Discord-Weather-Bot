# Discord Weather Bot

[![CI](https://github.com/jschollenberger/discord-weather-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/jschollenberger/discord-weather-bot/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

A Discord bot that posts live weather conditions, NWS alerts, tides, air quality, and hurricane tracking for any US area you configure. Every location detail — the alert coverage zones, forecast point, and tide and radar stations — lives in `config.json`, so it runs anywhere in the United States. It ships configured for Southern New Jersey (the seven counties served by NWS Mount Holly) as a ready-to-run example.

Conditions are pulled from a personal weather station (PWS) via the Aeris/Xweather API. Alerts, forecasts, tides, air quality, and tropical storm data come from NWS, NOAA CO-OPS, EPA AirNow, and the National Hurricane Center.

## Features

- **Live conditions** that update in place on a single pinned message — temperature, feels-like, humidity/dewpoint, wind, barometric trend, rain, UV, and sunrise/sunset — with Refresh, Radar, Alerts, and Forecast buttons that reply privately to whoever clicks
- **NWS alerts** posted automatically as they're issued, updated, or cancelled, filtered to your configured area by an explicit set of NWS forecast zones and county codes (see below) — not a heuristic — with a configurable severity threshold and a per-type suppression list
- **Weekly outlook** auto-posted on a configurable day/time — forecast, tides, air quality, and active alert count
- **Slash commands** for on-demand conditions, alerts, forecasts, tides, AQI, hurricane status, live radar imagery, and bot health
- **Live status** — the bot shows a "Watching …" presence under its name reflecting current conditions, e.g. `Partly Cloudy · 72°F · No Alerts` or `Thunderstorm · 68°F · ⚠️ 2 alerts`
- **Built to stay up** — per-service circuit breakers and exponential-backoff retries mean one flaky upstream API degrades gracefully instead of taking the bot down, state is written atomically so a crash mid-write can't corrupt it, and an NWS outage is never mistaken for "no active alerts" (so it can't trigger false all-clears)

## Coverage area

The bot works for any US area — pick your NWS forecast zones and county codes and set them under `coverage` in `config.json`. Alerts are matched by NWS UGC code against those. The built-in default (used when the key is omitted) is the seven Southern NJ counties served by NWS Mount Holly, which also doubles as a worked example:

| Zone | County | Zone | County |
|---|---|---|---|
| NJZ016 | Salem | NJZ022 | Atlantic |
| NJZ017 | Gloucester | NJZ023 | Cape May |
| NJZ018 | Camden | NJZ024 | Atlantic Coastal Cape May |
| NJZ019 | NW Burlington | NJZ025 | Coastal Atlantic |
| NJZ021 | Cumberland | NJZ027 | SE Burlington |

Ocean, Mercer, Middlesex, Monmouth, and every other NJ county are excluded by default. An alert only needs to touch *one* configured zone or county to post — a storm spanning both Southern and Central NJ will still show up.

To narrow or change the area, set `coverage` explicitly. Atlantic County only:

```json
"coverage": {
    "Atlantic": { "county_code": "NJC001", "zones": ["NJZ022", "NJZ025"] }
}
```

Each entry needs a `county_code` (a `C` UGC code, matched against warnings issued by county) and its `zones` (`Z` UGC codes, matched against zone-based alerts). Both lists are validated at startup, so a typo like `NJZ22` is reported as a config error rather than silently shrinking your coverage. The zone→county mapping used for NWS link buttons is derived from this same block, so it can't fall out of sync.

> **Match zones by code, not by name.** `NJZ024` is called *Atlantic Coastal Cape May* but belongs to **Cape May** County, not Atlantic.

Two ready-to-edit examples ship with the repo: `config.example.southern-nj.json` (all 7 counties) and `config.example.atlantic.json` (Atlantic only).

## Running more than one guild

The bot serves one channel in one guild per process. To cover a second guild with a different area, run a **second instance of the same code** with its own config.

Point each instance at its own data directory with `--data-dir` (or the `WEATHER_BOT_DIR` environment variable). `config.json`, `state.json`, and the log file all live there, so one checkout serves any number of instances:

```bash
mkdir -p ~/bots/southern-nj ~/bots/atlantic
cp config.example.southern-nj.json ~/bots/southern-nj/config.json
cp config.example.atlantic.json    ~/bots/atlantic/config.json

python weather_bot.py --data-dir ~/bots/southern-nj
python weather_bot.py --data-dir ~/bots/atlantic
```

`--config FILE` overrides just the config path if you'd rather keep it elsewhere. With no flags, everything resolves next to `weather_bot.py` as before, so existing single-instance setups need no changes.

`/status` shows the coverage area, radar station, and `location_name` in its title, so it's obvious which instance you're talking to.

Two things to get right:

- **Use a separate Discord application and bot token per instance.** Sharing one token means both processes receive the same slash-command interactions; one wins and the other errors on an already-acknowledged interaction, which users see as flaky commands.
- **Upstream calls scale with instances.** Two instances double the NWS, AirNow, and PWS requests. That's comfortably within NWS and AirNow limits at default intervals, but check your Xweather plan's request quota.

## Commands

| Command | Description |
|---|---|
| `/conditions [station_id]` | Latest PWS reading; optionally query any Xweather station |
| `/alerts` | Active NWS alerts for your configured coverage area |
| `/forecast [zipcode]` | NWS 7-day forecast; optionally for any US zip code |
| `/tides [station_id]` | High/low tide schedule; optionally any NOAA CO-OPS station |
| `/aqi` | Current + forecast EPA AirNow air quality (needs an API key) |
| `/hurricane` | NHC Atlantic tropical storm / hurricane status |
| `/radar` | Live NWS radar image, plus links to the interactive viewer |
| `/status` | Bot uptime, last update times, circuit-breaker health |
| `/help` | Command list |

## Requirements

- Python 3.10+
- A Discord application with a bot user ([discord.com/developers/applications](https://discord.com/developers/applications)), invited with the `bot` and `applications.commands` scopes
- An Aeris/Xweather account for your PWS station ([xweather.com](https://www.xweather.com/))
- Optional: an EPA AirNow API key for `/aqi` — free at [docs.airnowapi.org](https://docs.airnowapi.org/)
- Optional: `astral` for sunrise/sunset times

No privileged Gateway Intents are needed — the bot runs on `discord.Intents.default()`. See [Creating the Discord bot](#creating-the-discord-bot) for the exact scopes and permissions.

## Creating the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Name it whatever you like — this name is what shows up in the command picker.
2. Open the **Bot** tab and click **Reset Token** to reveal a token. Copy it into `discord_bot_token` in your config. Treat it like a password; anyone with it controls the bot. If you ever leak it, reset it here and the old one stops working.
3. No **Privileged Gateway Intents** are needed. The bot runs on default intents — leave Presence, Server Members, and Message Content off.
4. Open **OAuth2 → URL Generator** and tick:

   **Scopes**
   - `bot`
   - `applications.commands` — required for slash commands; without it none of the `/` commands appear

   **Bot Permissions**
   - View Channels
   - Send Messages
   - Embed Links — all output is embeds, so nothing renders without this
   - Attach Files — for the live radar image in `/radar`
   - Read Message History — needed to find and edit the pinned conditions message after a restart
   - Manage Messages — only for pinning the conditions message; skip it if you set `pin_conditions_message: false`
   - Use External Emoji *(optional)* — only if your server has custom emoji you want in alerts

5. Copy the generated URL at the bottom, open it, and pick the server to add the bot to. You need Manage Server permission on that server.
6. Make sure the bot can actually see the target channel. Channel-level overrides beat server-level permissions, so a private channel needs the bot (or a role it has) added explicitly.

To get `discord_channel_id` and `discord_guild_id`: enable **Settings → Advanced → Developer Mode** in Discord, then right-click the channel or server and choose **Copy ID**.

**Running alongside an existing bot?** Create a separate application for this one rather than reusing an existing bot's token. Two processes sharing a token fight over the same slash-command interactions, and this bot's startup sync would overwrite the other bot's registered commands.

## Setup

```bash
git clone https://github.com/jschollenberger/discord-weather-bot.git
cd discord-weather-bot
pip install -r requirements.txt
cp config.example.southern-nj.json config.json
```

Fill in `config.json` with your credentials (see reference below), then run:

```bash
python weather_bot.py
```

Startup validates `config.json` and exits with a specific, readable error for anything missing or malformed, so a bad config fails fast instead of failing quietly later.

## Configuration reference

| Key | Default | Notes |
|---|---|---|
| `pws_station_id` | — | **Required.** Your Aeris/Xweather PWS station ID |
| `pws_client_id` / `pws_client_secret` | — | **Required.** Aeris/Xweather API credentials |
| `pws_station_url` | (derived) | Public PWSweather page linked from `/conditions` and `/status`. Defaults to `pwsweather.com/station/pws/<pws_station_id>`; set explicitly if your station's public page differs |
| `discord_bot_token` | — | **Required.** From the Discord Developer Portal |
| `discord_channel_id` | — | Channel the bot posts conditions and alerts to |
| `discord_guild_id` | none | Optional — enables near-instant slash-command sync for one server instead of the ~1 hour global sync |
| `command_sync` | "auto" | `auto` / `global` / `guild` — which scope slash commands register in. `auto` uses guild scope when `discord_guild_id` is set, global otherwise |
| `presence_enabled` | true | Show a live "Watching …" status (sky · temp · alerts) under the bot's name |
| `presence_rotate_secs` | 120 | Seconds between presence refreshes (minimum 60) |
| `presence_fallback` | "the skies over <location>" | Status shown before the first observation is fetched |
| `location_name` | "Southern NJ" | Display name used in embeds and slash-command descriptions (keep under 40 chars) |
| `coverage` | 7 Southern NJ counties | Counties and NWS zones to match alerts against — see [Coverage area](#coverage-area) |
| `conditions_update_mins` | 30 | How often the conditions message refreshes |
| `conditions_repost_hours` | 4 | Repost as a new message (instead of editing) after this long |
| `pin_conditions_message` | true | Pin the conditions message |
| `alert_interval_secs` | 300 | How often NWS alerts are polled |
| `alert_post_threshold` | "all" | `all` / `watch` / `warning` — minimum severity auto-posted to the channel |
| `alert_suppress_types` | [] | Specific event names to never auto-post, e.g. `"Small Craft Advisory"` |
| `forecast_lat` / `forecast_lon` | 39.455 / -74.722 | Default forecast location |
| `tide_station_id` / `tide_station_name` | 8534720 / "Atlantic City, NJ" | Default NOAA CO-OPS tide station |
| `radar_station` / `radar_station_name` | KDIX / "Fort Dix, NJ" | NWS radar site used by `/radar` — [station list](https://radar.weather.gov/) |
| `radar_region` | "northeast" | Regional radar view linked from `/radar` |
| `radar_attach_image` | true | Attach the live radar loop image; set false to link only |
| `airnow_api_key` | none | Optional — enables `/aqi` and AQI threshold alerts |
| `aqi_alert_threshold` | 3 | AQI category (1–6) that triggers an alert |
| `weekly_summary_day` / `weekly_summary_hour` | 6 / 8 | When the weekly outlook posts (0=Mon … 6=Sun, hour in ET) |

Suppressed or below-threshold alerts still show up in `/alerts` — they're just not auto-posted to the channel.

## Running continuously

This is a single long-running process with no built-in daemonization. Run it under `systemd`, `tmux`/`screen`, `pm2`, or your process supervisor of choice so it restarts if it ever exits. Logs go to `weather-bot.log`, with the previous run kept as `weather-bot.log.1`. `state.json` self-trims resolved alert entries older than 48 hours, so it won't grow without bound.

## Development

CI (GitHub Actions) runs lint, a compile check, and the test suite on Python 3.10–3.12 for every push and PR. To run the same checks locally:

```bash
pip install ruff pytest
ruff check weather_bot.py tests/
pytest
```

When bumping the version, update `__version__` in `weather_bot.py` and add a matching entry at the top of `CHANGELOG.md` — a test asserts the two agree.

The tests in `tests/` are regression tests for logic that has actually failed in the past — the coverage-area geography filter, the zone→county fallback table, update-chain reference resolution, and state pruning. If you touch any of that, run them.

## Data sources

- Conditions: [Aeris/Xweather](https://www.xweather.com/)
- Alerts & forecasts: [National Weather Service](https://www.weather.gov/)
- Tides: [NOAA CO-OPS](https://tidesandcurrents.noaa.gov/)
- Air quality: [EPA AirNow](https://www.airnow.gov/)
- Tropical systems: [National Hurricane Center](https://www.nhc.noaa.gov/)

Independent project — not affiliated with or endorsed by NOAA, NWS, EPA, or NHC.

## Changelog

Release history is in [CHANGELOG.md](CHANGELOG.md), following [Keep a Changelog](https://keepachangelog.com/) and [Semantic Versioning](https://semver.org/).

## License

[GNU General Public License v3.0](LICENSE) or later. You're free to run, study, modify, and share this. If you distribute a modified version (share the code, publish a fork, etc.), it must also be GPLv3 with source available. Note that GPLv3 doesn't require this for running a modified copy privately as a hosted bot without distributing the code — only AGPLv3 covers that case, and this project doesn't use it.

---

*Built by Jason Schollenberger KD2QED*
