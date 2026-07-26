# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.0.0] - 2026-07-26

### Changed
- **Renamed the bot to "Discord Weather Bot"** across its entire user-facing surface: the `/help` and `/status` titles, every embed footer, the console banner, the startup log, and the outbound identifiers (the User-Agent and the NOAA CO-OPS `application` parameter, now `DiscordWeatherBot`). The `/help` title drops the personal byline. Reflects the project's growth from a single mesh community into a general-purpose bot that works for any US location you configure.
- **BREAKING — environment variables renamed:** `SNJ_BOT_DIR` / `SNJ_BOT_CONFIG` are now `WEATHER_BOT_DIR` / `WEATHER_BOT_CONFIG`. Update any launch or service configuration that sets them. (The `--data-dir` / `--config` command-line flags are unchanged.)
- **BREAKING — button custom IDs renamed** from `snj_weather:*` to `weather_bot:*`. The persistent view now matches the new IDs, so the Refresh/Radar/Alerts/Forecast buttons on a conditions message posted by an older version stop responding after upgrading. Delete the pinned conditions message (or wait for the next automatic repost, at most `conditions_repost_hours` — default 4h) and the bot posts a fresh one with working buttons.
- README rewritten to present the bot as configurable for any US location, with Southern NJ (the seven counties served by NWS Mount Holly) shipped as the example default rather than the sole target.

### Removed
- The last region-specific string literal that was not already config-driven — a hardcoded `region="NJ"` label passed to the sunrise/sunset provider (cosmetic; astral uses only the latitude/longitude).

## [2.9.2] - 2026-07-25

### Fixed
- `/help` now shows the configured radar station and weekly-summary schedule instead of a hardcoded "KDIX" and "Sunday 8 AM ET", so a non-default deployment (different `radar_station`, `weekly_summary_day`/`weekly_summary_hour`) sees text that matches its own config.

### Security
- The optional `discord_webhook` URL — whose last path segment is a token — is now registered with the log redactor. aiohttp includes the full request URL in its exception text, so an HTTP error while deriving the channel ID from a webhook could previously have written the token to `weather-bot.log` in the clear.

## [2.9.1] - 2026-07-25

### Changed
- The Radar, Alerts, and Forecast quick-action buttons on the conditions message now use the muted grey (`secondary`) style to match the Refresh button, instead of the solid blurple (`primary`) that made them look heavy and mismatched. The emoji on each button still gives it a distinct identity.


## [2.9.0] - 2026-07-21

### Added
- The live conditions message now carries **Radar**, **Alerts**, and **Forecast** quick-action buttons alongside Refresh, turning it into a compact dashboard. Each button replies privately (ephemerally) to the person who clicked, so the shared message and the channel stay clean, and reuses the exact logic of the corresponding slash command. The buttons use stable custom IDs, so they keep working across bot restarts.

### Changed
- The `/alerts`, `/forecast`, and `/radar` command bodies were extracted into shared `_respond_*` helpers so the slash commands and the new buttons share a single implementation each rather than duplicating it.


## [2.8.1] - 2026-07-21

### Fixed
- Hardened all Discord embed sends against length limits. A `_clamp_embed()` helper now enforces every documented cap (title 256, description 4096, field name 256, field value 1024, footer 2048, 25 fields, 6000-char aggregate) on every embed, and all embed construction routes through a single `_embed()` helper. Previously a large multi-alert summary (10+ simultaneous alerts) could exceed the 4096-char description limit and fail the send, erroring the alert cycle.
- `LinkButtonView` now caps at Discord's 25-component limit and truncates button labels to 80 characters, so an unusually long button list (e.g. many simultaneous NHC storms) can't fail the send.


## [2.8.0] - 2026-07-21

### Added
- Live bot presence: the bot shows a "Watching …" status under its name that reflects current conditions — sky condition, temperature, and alert count — joined with dot separators, e.g. `Partly Cloudy · 72°F · No Alerts` or `Thunderstorm · 68°F · ⚠️ 2 alerts`. Each segment appears only when its data is available, and the alert segment ("No Alerts" or a count) shows once the first alert check has completed. Refreshes every `presence_rotate_secs` (default 120), only issues a gateway update when the text actually changes, and can be disabled with `presence_enabled: false`. `presence_fallback` sets the text shown before the first observation.

## [2.7.6] - 2026-07-21

### Added
- `command_sync` config key (`auto` | `global` | `guild`). `auto` uses guild scope when `discord_guild_id` is set (registers instantly) and global scope otherwise (works in DMs, up to ~1h to propagate).

### Fixed
- Every slash command appeared **twice** in the command picker whenever `discord_guild_id` was set. Discord stores global and guild command sets separately and displays both; the bot registered commands globally *and* copied the same set into the guild. Commands are now synced to exactly one scope and an empty set is pushed to the other, which also clears duplicates left by earlier versions on the first run after upgrading.

## [2.7.5] - 2026-07-21

### Added
- `--data-dir` and `--config` command-line flags (env: `SNJ_BOT_DIR`, `SNJ_BOT_CONFIG`) so a single checkout can run several instances, each with its own `config.json`, `state.json`, and log file in a separate directory.
- Configurable radar site via `radar_station`, `radar_station_name`, and `radar_region`, replacing the hardcoded KDIX.
- `/radar` attaches the live NWS RIDGE loop image instead of only linking to it. The image is fetched and uploaded rather than hotlinked, because Discord's CDN caches embed image URLs and would serve a stale frame. Falls back to link-only if the fetch fails; disable with `radar_attach_image: false`.

### Changed
- `/status` shows the configured coverage area and radar station, and includes `location_name` in its title, so multiple instances are distinguishable.

## [2.7.4] - 2026-07-21

### Security
- Credentials could be written to `weather-bot.log` in the clear. aiohttp includes the full request URL in its exception text, and the Aeris and AirNow endpoints carry credentials in the query string, so any HTTP error from those services logged `client_secret` / `API_KEY` verbatim. A logging filter now redacts all known secrets from every log record.

### Added
- Config warnings for skippable settings (no channel ID, no guild ID, no AirNow key, no coverage block), reported at startup without blocking.
- Channel resolution now retries with backoff instead of leaving the bot permanently idle after one transient failure at startup.

### Fixed
- `on_ready` is re-dispatched by discord.py whenever a session cannot be RESUMED, which started a **second** scheduler and double-posted every conditions update and every alert. One-time setup now runs exactly once, while channel resolution stays retryable so a reconnect can recover a startup that failed to resolve the channel.
- An unrecognised `alert_post_threshold` (for example `"Warning"` or `"warnings"`) silently fell back to `"all"`, quietly posting every advisory. Now a startup error.
- `alert_suppress_types` given as a bare string became a set of single characters and silently matched nothing. Now a startup error.
- Malformed or non-object `config.json` raised a raw `JSONDecodeError` traceback; it now reports the line and column of the syntax error.
- An invalid bot token, missing intents, Discord outage, or network failure at startup raised a raw traceback; each now exits with an actionable message, with the full traceback written to the log.

## [2.7.3] - 2026-07-21

### Added
- Coverage area is configurable via the `coverage` key in `config.json`, so one codebase can serve multiple guilds with different scopes (for example all of Southern NJ versus Atlantic County only) without forking. Omitting the key keeps the previous seven-county behavior.
- Coverage config is validated at startup: malformed UGC codes, zone codes used where county codes belong (and vice versa), and empty coverage are reported as normal config errors instead of silently matching nothing.

### Changed
- The zone list, county-code list, county-name list, and zone-to-county table are all derived from a single mapping, so they cannot drift apart. The hand-maintained zone-to-county table had been wrong for 9 of 10 zones in 2.7.
- Region-specific display strings follow `location_name` and `coverage` rather than being hardcoded to "Southern NJ" and the seven-county list.
- Renamed for region-neutrality: `_affects_southern_nj` to `_in_coverage`, `_snj_area_str` to `_coverage_area_str`, `_is_snj_zone` to `_is_coverage_zone`, `_SNJ_*` to `_COVERAGE_*`, and `_NJZ_COUNTY_FALLBACK` to `_ZONE_TO_COUNTY`.

## [2.7.2] - 2026-07-19

### Fixed
- `_find_ref_in_posted()` matched the first reference found in an update's `references` list, not necessarily the immediate predecessor. NWS often lists the entire update chain rather than just the last hop, so on a second or later update this could latch onto an older ancestor still in `posted`, misdirecting reply-threading and leaving the true immediate predecessor to fall through to the natural-expiry clear path. It now matches by most recent timestamp among all candidates, independent of list order.

## [2.7.1] - 2026-07-16

### Fixed
- Alert geography: UGC codes are matched against explicit, verified NWS zone and county sets instead of a numeric 15–27 range. The old range misread county codes (`NJC021`/`023`/`025` are Mercer, Middlesex, and Monmouth) and included zones `NJZ015`/`020`/`026` (Mercer, Ocean, Coastal Ocean), so out-of-area alerts were posting. Ocean County is now excluded.
- The zone-to-county fallback table was shifted by one zone, so "Active Alert" link buttons were built with the wrong county for most zones.
- Unbounded `state.json` growth: superseded and suppressed alert entries are now pruned after 48 hours. Previously only cleared entries were pruned, and superseded or suppressed entries never became cleared.
- An NWS alerts API outage no longer looks like "zero active alerts", which could post false CLEARED messages for every live alert. Fetch failures now skip the cycle entirely.
- `_validate_config()` catches non-numeric `forecast_lat`/`forecast_lon` and the interval, threshold, day, and hour settings, reporting them as normal config errors instead of crashing at startup with a raw traceback.
- Exception log lines include the exception type name, so failures whose `str()` is empty (such as a bare `TimeoutError`) no longer log a blank, useless line.
- `ConditionsRefreshView`'s per-user cooldown dictionary sweeps stale entries instead of growing for the life of the process.

## [2.7] - 2026-07-09

### Added
- Per-service circuit breaker: after 5 consecutive failures a service is skipped for 30 minutes, after 10 for 2 hours, resetting on success.
- `/status` command reporting uptime, last fetch times, active alerts, AQI category, and circuit-breaker state — ephemeral, and works in DMs.
- `_fetch_and_build_conditions()` helper, a single coroutine used by the scheduler, `/conditions`, and the Refresh button, replacing three call sites that had to be kept in sync.

### Changed
- aiohttp replaces requests: all fetches are native async coroutines, `asyncio.to_thread()` is eliminated entirely, and a shared `ClientSession` is reused across all calls and closed cleanly on shutdown.
- `_http_get` is async, so retries use `asyncio.sleep` and never block the event loop.
- `state.json` is written to a temporary file and renamed, so a crash mid-write cannot corrupt it.
- The scheduler body is wrapped in try/except, so an uncaught exception no longer silently kills the loop.

### Fixed
- `fetch_forecast` uses `_http_get`; it had been left using raw requests.

[Unreleased]: https://github.com/jschollenberger/discord-weather-bot/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.9.2...v3.0.0
[2.9.2]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.9.1...v2.9.2
[2.9.1]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.9.0...v2.9.1
[2.9.0]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.8.1...v2.9.0
[2.8.1]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.8.0...v2.8.1
[2.8.0]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7.6...v2.8.0
[2.7.6]: https://github.com/jschollenberger/discord-weather-bot/releases/tag/v2.7.6
[2.7.5]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7.4...v2.7.5
[2.7.4]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7.3...v2.7.4
[2.7.3]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7.2...v2.7.3
[2.7.2]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7.1...v2.7.2
[2.7.1]: https://github.com/jschollenberger/discord-weather-bot/compare/v2.7...v2.7.1
[2.7]: https://github.com/jschollenberger/discord-weather-bot/releases/tag/v2.7
