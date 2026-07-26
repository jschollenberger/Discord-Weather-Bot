"""
Regression tests for Discord Weather Bot.

These cover the pure logic that has actually bitten in production:
  * the coverage-area filter        (v2.7 posted out-of-area alerts)
  * the zone->county derivation     (v2.7's hand-kept table was shifted)
  * update-chain reference matching (v2.7.1 latched onto old ancestors)
  * the posted_alerts prune         (v2.7 grew state.json without bound)
  * coverage config validation      (v2.7.3)

weather_bot.py loads config (and may sys.exit) at import time, so rather than
importing it we exec the relevant self-contained source blocks.
Run from the repo root:  pytest
"""
import re
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent / "weather_bot.py").read_text(encoding="utf-8")
CHANGELOG = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")


class TestVersion:
    """__version__ is the single source of truth for the version string and
    must match the newest released entry in CHANGELOG.md."""

    def test_version_declared_once(self):
        assert len(re.findall(r'^__version__ = "', SRC, re.M)) == 1

    def test_version_matches_latest_changelog_entry(self):
        declared = re.search(r'^__version__ = "([^"]+)"', SRC, re.M).group(1)
        # first "## [x.y.z]" heading that isn't [Unreleased]
        released = re.findall(r"^## \[(?!Unreleased)([^\]]+)\]", CHANGELOG, re.M)
        assert released, "CHANGELOG.md has no released version headings"
        assert declared == released[0], (
            f"__version__ is {declared} but newest CHANGELOG entry is "
            f"{released[0]}")

    def test_changelog_versions_are_descending(self):
        def key(v):
            return tuple(int(p) for p in v.split("."))
        released = re.findall(r"^## \[(?!Unreleased)([^\]]+)\]", CHANGELOG, re.M)
        assert released == sorted(released, key=key, reverse=True)

    def test_runtime_strings_interpolate_version(self):
        """The banner, status footer, UA and startup log must use
        __version__ rather than a baked-in literal."""
        for anchor in ("DiscordWeatherBot/{__version__}",
                       "Weather Bot v{__version__}",
                       'f"v{__version__} | Started'):
            assert anchor in SRC, anchor


def _exec_block(start_marker: str, end_marker: str, ns: dict | None = None) -> dict:
    start = SRC.index(start_marker)
    end   = SRC.index(end_marker, start)
    ns = {} if ns is None else ns
    exec(SRC[start:end], ns)  # noqa: S102 - executing our own source under test
    return ns


# --- Coverage block ----------------------------------------------------------
# `_cfg` is stubbed empty so COVERAGE falls back to DEFAULT_COVERAGE.
geo = _exec_block("DEFAULT_COVERAGE: dict", "def _nws_human_url", {"_cfg": {}})
DEFAULT_COVERAGE       = geo["DEFAULT_COVERAGE"]
_derive_coverage       = geo["_derive_coverage"]
_in_coverage           = geo["_in_coverage"]
_is_coverage_zone      = geo["_is_coverage_zone"]
_COVERAGE_ZONES        = geo["_COVERAGE_ZONES"]
_COVERAGE_COUNTY_CODES = geo["_COVERAGE_COUNTY_CODES"]
_ZONE_TO_COUNTY        = geo["_ZONE_TO_COUNTY"]

# --- Config validation -------------------------------------------------------
val = _exec_block("_THRESHOLD_TIER = ", "_cfg = _load_config()", {"re": re})
_validate_coverage = val["_validate_coverage"]
_validate_config   = val["_validate_config"]
_config_warnings   = val["_config_warnings"]

MINIMAL_OK = {
    "pws_station_id": "STN", "pws_client_id": "cid",
    "pws_client_secret": "sec", "discord_bot_token": "tok",
}


def cfg(**over):
    c = dict(MINIMAL_OK)
    c.update(over)
    return c

# --- Reference-chain matching ------------------------------------------------
ref = _exec_block("def _find_ref_in_posted", "\nasync def fetch_tides")
_find_ref_in_posted = ref["_find_ref_in_posted"]


ATLANTIC_ONLY = {"Atlantic": {"county_code": "NJC001",
                              "zones": ["NJZ022", "NJZ025"]}}


def feat(ugc, area="", headline=""):
    return {"properties": {"geocode": {"UGC": ugc},
                           "areaDesc": area, "headline": headline}}


def make_filter(cov):
    """Build an _in_coverage bound to an arbitrary coverage config."""
    names, counties, zones, _ = _derive_coverage(cov)
    ns = dict(_COVERAGE_NAMES=names, _COVERAGE_COUNTY_CODES=counties,
              _COVERAGE_ZONES=zones)
    return _exec_block("def _in_coverage", "def _coverage_area_str",
                       ns)["_in_coverage"]


# ============================================================================
# Coverage filter - default (7-county Southern NJ)
# ============================================================================

class TestDefaultCoverage:
    # --- regressions: the exact alert shapes that leaked in v2.7 ---
    def test_mercer_middlesex_monmouth_county_codes_excluded(self):
        f = feat(["NJC021", "NJC023", "NJC025"],
                 "Mercer, NJ; Middlesex, NJ; Monmouth, NJ")
        assert _in_coverage(f) is False

    def test_hunterdon_somerset_flash_flood_excluded(self):
        f = feat(["NJC019", "NJC021", "NJC023", "NJC035"],
                 "Hunterdon, NJ; Mercer, NJ; Middlesex, NJ; Somerset, NJ")
        assert _in_coverage(f) is False

    @pytest.mark.parametrize("zone,label", [
        ("NJZ026", "Coastal Ocean"), ("NJZ020", "Ocean"), ("NJZ015", "Mercer")])
    def test_out_of_area_zones_excluded(self, zone, label):
        assert _in_coverage(feat([zone], label)) is False

    def test_monmouth_plus_coastal_ocean_combo_excluded(self):
        f = feat(["NJZ014", "NJZ026"], "Eastern Monmouth; Coastal Ocean")
        assert _in_coverage(f) is False

    # --- in-scope alerts must still post ---
    def test_atlantic_county_code_included(self):
        assert _in_coverage(feat(["NJC001"], "Atlantic, NJ")) is True

    def test_all_ten_southern_zones_included(self):
        f = feat(sorted(_COVERAGE_ZONES),
                 "Salem; Gloucester; Camden; Northwestern Burlington; "
                 "Cumberland; Atlantic; Cape May; Atlantic Coastal Cape May; "
                 "Coastal Atlantic; Southeastern Burlington")
        assert _in_coverage(f) is True

    def test_multicounty_storm_clipping_burlington_included(self):
        f = feat(["NJC021", "NJC005"], "Mercer, NJ; Burlington, NJ")
        assert _in_coverage(f) is True

    def test_every_configured_zone_included_individually(self):
        for z in _COVERAGE_ZONES:
            assert _in_coverage(feat([z])) is True, z

    def test_every_configured_county_included_individually(self):
        for c in _COVERAGE_COUNTY_CODES:
            assert _in_coverage(feat([c])) is True, c

    # --- fallback path (no UGC) ---
    def test_no_ugc_falls_back_to_county_names(self):
        assert _in_coverage(feat([], "Cape May; Cumberland")) is True

    def test_no_ugc_out_of_area(self):
        assert _in_coverage(feat([], "Bergen; Passaic")) is False


class TestDefaultCoverageShape:
    def test_ten_zones_seven_counties(self):
        assert len(_COVERAGE_ZONES) == 10
        assert len(_COVERAGE_COUNTY_CODES) == 7
        assert len(DEFAULT_COVERAGE) == 7

    @pytest.mark.parametrize("zone", ["NJZ015", "NJZ020", "NJZ026"])
    def test_excluded_zones_not_in_set(self, zone):
        assert not _is_coverage_zone(zone)

    def test_zone_to_county_covers_every_zone(self):
        assert set(_ZONE_TO_COUNTY) == set(_COVERAGE_ZONES)
        assert set(_ZONE_TO_COUNTY.values()) <= set(_COVERAGE_COUNTY_CODES)

    def test_njz024_maps_to_cape_may_not_atlantic(self):
        """'Atlantic Coastal Cape May' is a Cape May zone despite the name."""
        assert _ZONE_TO_COUNTY["NJZ024"] == "NJC009"
        assert _ZONE_TO_COUNTY["NJZ025"] == "NJC001"


# ============================================================================
# Coverage filter - Atlantic County only  (second-guild deployment)
# ============================================================================

class TestAtlanticOnlyCoverage:
    filt = staticmethod(make_filter(ATLANTIC_ONLY))

    def test_atlantic_zones_included(self):
        for z in ("NJZ022", "NJZ025"):
            assert self.filt(feat([z])) is True, z

    def test_atlantic_county_code_included(self):
        assert self.filt(feat(["NJC001"], "Atlantic, NJ")) is True

    @pytest.mark.parametrize("zone", [
        "NJZ016", "NJZ017", "NJZ018", "NJZ019", "NJZ021", "NJZ023", "NJZ027"])
    def test_other_southern_nj_zones_excluded(self, zone):
        assert self.filt(feat([zone])) is False

    def test_njz024_excluded_despite_atlantic_in_its_name(self):
        """NJZ024 'Atlantic Coastal Cape May' is Cape May - must NOT match."""
        assert self.filt(feat(["NJZ024"], "Atlantic Coastal Cape May")) is False

    def test_storm_spanning_atlantic_and_cape_may_still_posts(self):
        assert self.filt(feat(["NJC001", "NJC009"],
                              "Atlantic, NJ; Cape May, NJ")) is True

    def test_cape_may_only_storm_excluded(self):
        assert self.filt(feat(["NJC009"], "Cape May, NJ")) is False

    def test_derived_sets(self):
        names, counties, zones, z2c = _derive_coverage(ATLANTIC_ONLY)
        assert names == {"Atlantic"}
        assert counties == {"NJC001"}
        assert zones == {"NJZ022", "NJZ025"}
        assert z2c == {"NJZ022": "NJC001", "NJZ025": "NJC001"}


# ============================================================================
# Coverage config validation
# ============================================================================

class TestValidateCoverage:
    def test_absent_coverage_is_valid(self):
        assert _validate_coverage(None) == []

    def test_default_coverage_is_valid(self):
        assert _validate_coverage(DEFAULT_COVERAGE) == []

    def test_atlantic_only_is_valid(self):
        assert _validate_coverage(ATLANTIC_ONLY) == []

    def test_empty_object_rejected(self):
        assert _validate_coverage({}) != []

    def test_wrong_type_rejected(self):
        assert _validate_coverage(["NJZ022"]) != []

    def test_typo_zone_code_rejected(self):
        cov = {"Atlantic": {"county_code": "NJC001", "zones": ["NJZ22"]}}
        assert any("NJZ22" in e for e in _validate_coverage(cov))

    def test_county_code_in_zones_rejected(self):
        cov = {"Atlantic": {"county_code": "NJC001", "zones": ["NJC001"]}}
        assert any("county code" in e for e in _validate_coverage(cov))

    def test_zone_code_as_county_code_rejected(self):
        cov = {"Atlantic": {"county_code": "NJZ022", "zones": ["NJZ022"]}}
        assert any("zone code" in e for e in _validate_coverage(cov))

    def test_entry_with_no_zones_or_county_rejected(self):
        assert _validate_coverage({"Nowhere": {}}) != []

    def test_zones_not_a_list_rejected(self):
        cov = {"Atlantic": {"county_code": "NJC001", "zones": "NJZ022"}}
        assert _validate_coverage(cov) != []


# ============================================================================
# Config validation - required fields and hard errors
# ============================================================================

class TestValidateConfig:
    def test_minimal_valid_config_passes(self):
        assert _validate_config(cfg()) == []

    @pytest.mark.parametrize("key", [
        "pws_station_id", "pws_client_id", "pws_client_secret", "discord_bot_token"])
    def test_missing_required_field_is_error(self, key):
        c = cfg()
        del c[key]
        assert any(key in e for e in _validate_config(c))

    @pytest.mark.parametrize("key", [
        "pws_station_id", "pws_client_id", "pws_client_secret", "discord_bot_token"])
    def test_unreplaced_placeholder_is_error(self, key):
        assert any(key in e for e in _validate_config(cfg(**{key: "YOUR_TOKEN"})))

    # --- settings that used to misconfigure SILENTLY ---
    @pytest.mark.parametrize("bad", ["Warning", "warnings", "WARNING", "none", ""])
    def test_bad_alert_post_threshold_is_error(self, bad):
        """An unrecognised threshold silently meant 'all' before v2.7.4,
        quietly posting every advisory."""
        errs = _validate_config(cfg(alert_post_threshold=bad))
        assert any("alert_post_threshold" in e for e in errs)

    @pytest.mark.parametrize("good", ["all", "watch", "warning"])
    def test_valid_alert_post_threshold_accepted(self, good):
        assert _validate_config(cfg(alert_post_threshold=good)) == []

    def test_string_suppress_types_is_error(self):
        """set('Small Craft Advisory') is a set of CHARACTERS - matches nothing."""
        errs = _validate_config(cfg(alert_suppress_types="Small Craft Advisory"))
        assert any("alert_suppress_types" in e for e in errs)

    def test_non_string_entries_in_suppress_types_is_error(self):
        errs = _validate_config(cfg(alert_suppress_types=["ok", 5]))
        assert any("alert_suppress_types" in e for e in errs)

    def test_empty_suppress_list_is_fine(self):
        assert _validate_config(cfg(alert_suppress_types=[])) == []

    # --- numeric guards ---
    @pytest.mark.parametrize("key,bad", [
        ("forecast_lat", "abc"), ("forecast_lon", "xyz"),
        ("alert_interval_secs", "soon"), ("conditions_update_mins", "half"),
        ("aqi_alert_threshold", "high"), ("weekly_summary_day", "Sunday"),
        ("weekly_summary_hour", "8am")])
    def test_non_numeric_values_error_not_crash(self, key, bad):
        errs = _validate_config(cfg(**{key: bad}))
        assert any(key in e for e in errs)

    @pytest.mark.parametrize("key", ["forecast_lat", "forecast_lon"])
    def test_null_latlon_means_unset_and_is_accepted(self, key):
        """Explicit null falls back to the built-in default, so it is valid."""
        assert _validate_config(cfg(**{key: None})) == []

    @pytest.mark.parametrize("key,bad", [
        ("forecast_lat", 91), ("forecast_lon", -181),
        ("alert_interval_secs", 30), ("conditions_update_mins", 1),
        ("aqi_alert_threshold", 7), ("weekly_summary_day", 9),
        ("weekly_summary_hour", 24)])
    def test_out_of_range_values_error(self, key, bad):
        errs = _validate_config(cfg(**{key: bad}))
        assert any(key in e for e in errs)

    def test_long_location_name_is_error(self):
        """Interpolated into slash-command descriptions, capped at 100 by Discord."""
        errs = _validate_config(cfg(location_name="X" * 60))
        assert any("location_name" in e for e in errs)

    def test_non_numeric_channel_id_is_error(self):
        errs = _validate_config(cfg(discord_channel_id="not-an-id"))
        assert any("discord_channel_id" in e for e in errs)

    def test_bad_coverage_surfaces_through_validate_config(self):
        c = cfg(coverage={"Atlantic": {"county_code": "NJC001", "zones": ["NJZ22"]}})
        assert any("coverage" in e for e in _validate_config(c))

    # --- radar station ---
    @pytest.mark.parametrize("good", ["KDIX", "KDOX", "kdix"])
    def test_valid_radar_station_accepted(self, good):
        assert _validate_config(cfg(radar_station=good)) == []

    @pytest.mark.parametrize("bad", ["KDI", "KDIXX", "K1IX", "", 42, None])
    def test_bad_radar_station_is_error(self, bad):
        errs = _validate_config(cfg(radar_station=bad))
        assert any("radar_station" in e for e in errs)

    def test_radar_station_defaults_when_absent(self):
        assert _validate_config(cfg()) == []

    # --- command sync scope ---
    @pytest.mark.parametrize("good", ["auto", "global", "guild", "GUILD"])
    def test_valid_command_sync_accepted(self, good):
        assert _validate_config(cfg(command_sync=good)) == []

    @pytest.mark.parametrize("bad", ["both", "all", "", "instant"])
    def test_bad_command_sync_is_error(self, bad):
        errs = _validate_config(cfg(command_sync=bad))
        assert any("command_sync" in e for e in errs)


class TestSyncScopeResolution:
    """Syncing the same commands to BOTH scopes makes every command appear
    twice in Discord's picker — the v2.7.5 duplicate-commands bug.  Exactly
    one scope must win."""

    @staticmethod
    def resolve(mode, guild_id):
        """Mirror of the scope choice in _sync_commands()."""
        if mode == "auto":
            return "guild" if guild_id else "global"
        return mode

    def test_auto_with_guild_id_uses_guild_only(self):
        assert self.resolve("auto", "123") == "guild"

    def test_auto_without_guild_id_uses_global_only(self):
        assert self.resolve("auto", None) == "global"

    def test_explicit_global_overrides_guild_id(self):
        """Forcing global keeps commands working in DMs."""
        assert self.resolve("global", "123") == "global"

    def test_explicit_guild_honoured(self):
        assert self.resolve("guild", "123") == "guild"

    def test_never_returns_both(self):
        for mode in ("auto", "global", "guild"):
            for gid in (None, "123"):
                assert self.resolve(mode, gid) in ("global", "guild")

    def test_source_syncs_exactly_one_scope(self):
        """Guard against a future edit reintroducing the dual sync."""
        block = SRC[SRC.index("async def _sync_commands"):
                    SRC.index("@bot.event", SRC.index("async def _sync_commands"))]
        # each branch must clear the opposite scope before/after syncing
        assert "tree.clear_commands(guild=None)" in block
        assert "tree.clear_commands(guild=guild)" in block


# ============================================================================
# Config warnings - skippable settings must warn, never block
# ============================================================================

class TestConfigWarnings:
    def test_bare_config_warns_about_each_optional_feature(self):
        w = " ".join(_config_warnings(cfg()))
        for key in ("discord_channel_id", "discord_guild_id",
                    "airnow_api_key", "coverage"):
            assert key in w, key

    def test_fully_specified_config_has_no_warnings(self):
        c = cfg(discord_channel_id="123", discord_guild_id="456",
                airnow_api_key="key", coverage=ATLANTIC_ONLY)
        assert _config_warnings(c) == []

    def test_warnings_are_not_errors(self):
        """A config that only trips warnings must still validate cleanly."""
        assert _validate_config(cfg()) == []
        assert _config_warnings(cfg()) != []

    def test_airnow_key_present_suppresses_that_warning(self):
        w = " ".join(_config_warnings(cfg(airnow_api_key="k")))
        assert "airnow_api_key" not in w


# ============================================================================
# Discord embed limits  (a 400 on send otherwise)
# ============================================================================

class TestClampEmbed:
    @staticmethod
    def clamp(embed):
        import logging
        ns = {"log": logging.getLogger("test")}
        fn = _exec_block("_D_TITLE, _D_DESC", "def _embed(", ns)["_clamp_embed"]
        return fn(embed)

    def test_title_capped_at_256(self):
        assert len(self.clamp({"title": "X" * 300})["title"]) == 256

    def test_description_capped_at_4096(self):
        assert len(self.clamp({"description": "Y" * 5000})["description"]) == 4096

    def test_field_value_capped_at_1024(self):
        out = self.clamp({"fields": [{"name": "n", "value": "Z" * 2000}]})
        assert len(out["fields"][0]["value"]) == 1024

    def test_field_name_capped_at_256(self):
        out = self.clamp({"fields": [{"name": "N" * 400, "value": "v"}]})
        assert len(out["fields"][0]["name"]) == 256

    def test_footer_capped(self):
        out = self.clamp({"footer": {"text": "F" * 3000}})
        assert len(out["footer"]["text"]) <= 2048

    def test_field_count_capped_at_25(self):
        out = self.clamp({"fields": [{"name": str(i), "value": "v"}
                                     for i in range(40)]})
        assert len(out["fields"]) == 25

    def test_empty_field_name_becomes_zero_width(self):
        """Discord rejects a truly empty field name."""
        out = self.clamp({"fields": [{"name": "", "value": ""}]})
        assert out["fields"][0]["name"] == "\u200b"
        assert out["fields"][0]["value"] == "\u200b"

    def test_aggregate_capped_at_6000(self):
        big = {"title": "T" * 250, "description": "D" * 4000,
               "fields": [{"name": "n" * 250, "value": "v" * 1000}
                          for _ in range(3)]}
        out = self.clamp(big)
        total = (len(out["title"]) + len(out["description"]) +
                 sum(len(f["name"]) + len(f["value"]) for f in out["fields"]))
        assert total <= 6000

    def test_none_description_does_not_crash(self):
        assert self.clamp({"description": None}).get("description") == ""

    def test_short_embed_unchanged(self):
        e = {"title": "Hi", "description": "short",
             "fields": [{"name": "a", "value": "b"}]}
        out = self.clamp(dict(e))
        assert out["title"] == "Hi" and out["fields"][0]["value"] == "b"

    def test_truncation_adds_ellipsis(self):
        assert self.clamp({"title": "X" * 300})["title"].endswith("…")


class TestButtonViewCap:
    """Discord allows at most 25 components per view and 80-char labels."""

    @staticmethod
    def count_source_guard():
        block = SRC[SRC.index("class LinkButtonView"):
                    SRC.index("class ConditionsRefreshView")]
        return block

    def test_source_caps_at_25(self):
        assert "[:25]" in self.count_source_guard()

    def test_source_truncates_label(self):
        assert "label[:80]" in self.count_source_guard()

    def test_source_skips_empty_urls(self):
        assert "if u]" in self.count_source_guard()


class TestEmbedSendsAreClamped:
    """Every embed must be built via _embed() (which clamps) rather than a raw
    discord.Embed.from_dict(), so no send can 400 on a length limit."""

    def test_only_one_raw_from_dict_the_definition(self):
        raw = re.findall(r"discord\.Embed\.from_dict", SRC)
        # exactly one: the call inside _embed() itself
        assert len(raw) == 1, (
            f"found {len(raw)} raw discord.Embed.from_dict calls; all embed "
            f"construction should go through _embed()")

    def test_embed_helper_calls_clamp(self):
        block = SRC[SRC.index("def _embed("):SRC.index("def _embed(") + 300]
        assert "_clamp_embed" in block


class TestConditionsDashboardButtons:
    """The persistent conditions message carries Radar/Alerts/Forecast quick
    buttons.  Their custom_ids must be stable (so they survive a restart) and
    the handlers must reuse the shared responders rather than duplicate the
    slash-command logic."""

    view_src = SRC[SRC.index("class ConditionsRefreshView"):
                   SRC.index("class ", SRC.index("class ConditionsRefreshView") + 10)]

    def test_all_three_buttons_present(self):
        for cid in ("weather_bot:radar", "weather_bot:alerts",
                    "weather_bot:forecast"):
            assert f'custom_id="{cid}"' in self.view_src, cid

    def test_refresh_button_still_present(self):
        assert 'custom_id="weather_bot:conditions_refresh"' in self.view_src

    def test_custom_ids_are_unique(self):
        ids = re.findall(r'custom_id="([^"]+)"', SRC)
        assert len(ids) == len(set(ids)), "duplicate custom_id would break a view"

    def test_view_stays_within_25_component_limit(self):
        assert self.view_src.count("@discord.ui.button") <= 25

    def test_buttons_reply_ephemerally(self):
        """Quick-action buttons must not spam the shared channel."""
        for handler in ("radar_btn", "alerts_btn", "forecast_btn"):
            seg = self.view_src[self.view_src.index(f"def {handler}"):
                                self.view_src.index(f"def {handler}") + 400]
            assert "ephemeral=True" in seg, handler

    def test_buttons_delegate_to_shared_responders(self):
        assert "_respond_radar(interaction" in self.view_src
        assert "_respond_alerts(interaction" in self.view_src
        assert "_respond_forecast(interaction" in self.view_src


class TestSharedRespondersSingleSourced:
    """Each responder must have exactly one implementation, called by both the
    slash command and the button — not copy-pasted."""

    def test_slash_commands_delegate_to_responders(self):
        assert "_respond_alerts(interaction, ephemeral=False)" in SRC
        assert "_respond_forecast(interaction, ephemeral=False" in SRC
        assert "_respond_radar(interaction, ephemeral=False)" in SRC

    def test_responders_defined_once(self):
        for name in ("_respond_alerts", "_respond_forecast", "_respond_radar"):
            assert len(re.findall(rf"^async def {name}\(", SRC, re.M)) == 1, name


# ============================================================================
# Composite presence status
# ============================================================================

class TestPresenceText:
    @staticmethod
    def make(state, fallback="watching the skies"):
        ns = {"PRESENCE_FALLBACK": fallback, "_state": state}
        return _exec_block("def _presence_text", "async def _rotate_presence",
                           ns)["_presence_text"]()

    def test_fallback_before_any_data(self):
        assert self.make({}) == "watching the skies"

    def test_sky_and_temp_joined_with_dot(self):
        assert self.make({"last_sky": "Partly Cloudy", "last_temp_f": 72.4}) \
            == "Partly Cloudy · 72°F"

    def test_temp_rounded(self):
        assert "58°F" in self.make({"last_temp_f": 58.2})

    def test_no_alert_segment_before_first_check(self):
        """Must not claim 'No Alerts' before an alert check has run."""
        assert "clear" not in self.make({"last_temp_f": 70})

    def test_all_clear_after_check_with_no_alerts(self):
        out = self.make({"last_temp_f": 70, "last_alert_check_ts": 1,
                         "posted_alerts": {}})
        assert out.endswith("No Alerts")

    def test_active_alerts_plural(self):
        out = self.make({"last_alert_check_ts": 1,
                         "posted_alerts": {"a": {}, "b": {}}})
        assert "⚠️ 2 alerts" in out

    def test_active_alert_singular(self):
        out = self.make({"last_alert_check_ts": 1, "posted_alerts": {"a": {}}})
        assert "⚠️ 1 alert" in out and "alerts" not in out

    def test_cleared_and_superseded_not_counted(self):
        out = self.make({"last_alert_check_ts": 1, "posted_alerts": {
            "a": {"cleared": True}, "b": {"superseded_by": "x"}}})
        assert out == "No Alerts"

    def test_full_composite_order(self):
        out = self.make({"last_sky": "Thunderstorm", "last_temp_f": 68,
                         "last_alert_check_ts": 1, "posted_alerts": {"a": {}}})
        assert out == "Thunderstorm · 68°F · ⚠️ 1 alert"

    def test_malformed_temp_ignored(self):
        out = self.make({"last_sky": "Foggy", "last_temp_f": "warm",
                         "last_alert_check_ts": 1, "posted_alerts": {}})
        assert out == "Foggy · No Alerts"

    def test_result_capped_at_128_chars(self):
        out = self.make({"last_sky": "X" * 200})
        assert len(out) <= 128


class TestPresenceValidation:
    def test_absent_uses_defaults(self):
        assert _validate_config(cfg()) == []

    def test_custom_fallback_accepted(self):
        assert _validate_config(cfg(presence_fallback="the horizon")) == []

    def test_empty_fallback_rejected(self):
        assert any("presence_fallback" in e
                   for e in _validate_config(cfg(presence_fallback="")))

    def test_non_string_fallback_rejected(self):
        assert any("presence_fallback" in e
                   for e in _validate_config(cfg(presence_fallback=["x"])))

    def test_overlong_fallback_rejected(self):
        assert any("presence_fallback" in e
                   for e in _validate_config(cfg(presence_fallback="x" * 200)))


# ============================================================================
# Log redaction - credentials must never reach the log file
# ============================================================================

class TestRedactFilter:
    """aiohttp puts the full request URL in its exception text, and the Aeris
    and AirNow endpoints carry credentials in the query string, so an HTTP
    error would otherwise write them to weather-bot.log in the clear."""

    ID, SECRET, TOKEN = "REAL_ID_12345678", "REAL_SECRET_ABCDEF", "MTUxNjc0.tok.en"
    WEBHOOK = ("https://discord.com/api/webhooks/123456789012345678/"
               "AbCdEf-ghIjKlMnOpQrStUvWxYz0123456789")

    @classmethod
    def setup_class(cls):
        import logging as _logging
        ns = _exec_block("class _RedactFilter", "_redactor = _RedactFilter()",
                         {"logging": _logging})
        cls.F = ns["_RedactFilter"]
        cls.F.set_secrets(cls.ID, cls.SECRET, cls.TOKEN, cls.WEBHOOK, None, "", "short")
        cls.filt = cls.F()
        cls._logging = _logging

    def scrub(self, msg, args=None):
        rec = self._logging.LogRecord("t", 40, "", 1, msg, args, None)
        self.filt.filter(rec)
        return rec.msg, rec.args

    def test_airnow_key_in_url_redacted(self):
        msg, _ = self.scrub(
            f"AirNow fetch failed: 500, url='https://airnowapi.org/x?API_KEY={self.SECRET}'")
        assert self.SECRET not in msg
        assert "REDACTED" in msg

    def test_aeris_credentials_in_url_redacted(self):
        msg, _ = self.scrub(
            f"conditions: url='https://api.aerisapi.com/x"
            f"?client_id={self.ID}&client_secret={self.SECRET}'")
        assert self.ID not in msg and self.SECRET not in msg

    def test_bot_token_redacted(self):
        msg, _ = self.scrub(f"logging in with {self.TOKEN}")
        assert self.TOKEN not in msg

    def test_webhook_url_redacted(self):
        """A discord_webhook URL embeds a token in its last path segment, and
        aiohttp logs the full URL on error, so it must be scrubbed."""
        msg, _ = self.scrub(
            "Could not derive channel ID from webhook URL: "
            f"ClientResponseError: 401, url='{self.WEBHOOK}'")
        assert self.WEBHOOK not in msg
        assert "REDACTED" in msg

    def test_module_registers_webhook_secret(self):
        """Guard the wiring: weather_bot.py must pass discord_webhook to the
        redactor, not only the four API credentials."""
        block = SRC[SRC.index("_RedactFilter.set_secrets("):
                    SRC.index("_errs = _validate_config")]
        assert "discord_webhook" in block

    def test_args_are_scrubbed_too(self):
        _, args = self.scrub("value=%s", (self.SECRET,))
        assert self.SECRET not in args[0]

    def test_short_and_empty_values_not_registered(self):
        """Short strings would redact innocuous substrings all over the log."""
        assert "short" not in self.F._secrets
        assert "" not in self.F._secrets
        assert None not in self.F._secrets

    def test_ordinary_message_untouched(self):
        msg, _ = self.scrub("Checking NWS alerts")
        assert msg == "Checking NWS alerts"

    def test_filter_always_returns_true(self):
        """Returning False would silently drop the record entirely."""
        rec = self._logging.LogRecord("t", 40, "", 1, "x", None, None)
        assert self.filt.filter(rec) is True


# ============================================================================
# Update-chain reference matching  (v2.7.2 regression)
# ============================================================================

class TestFindRefInPosted:
    posted = {"aid_A": {"message_id": "1000", "ts": 1000},
              "aid_B": {"message_id": "2000", "ts": 2000}}

    def test_oldest_first_reference_order(self):
        refs = [{"identifier": "aid_A"}, {"identifier": "aid_B"}]
        assert _find_ref_in_posted(refs, self.posted) == "aid_B"

    def test_newest_first_reference_order(self):
        refs = [{"identifier": "aid_B"}, {"identifier": "aid_A"}]
        assert _find_ref_in_posted(refs, self.posted) == "aid_B"

    def test_single_reference(self):
        assert _find_ref_in_posted([{"identifier": "aid_B"}], self.posted) == "aid_B"

    def test_bare_urn_resolved_via_full_url(self):
        posted = {"https://api.weather.gov/alerts/urn:x:1": {"ts": 5}}
        assert (_find_ref_in_posted([{"identifier": "urn:x:1"}], posted)
                == "https://api.weather.gov/alerts/urn:x:1")

    def test_no_match_returns_none(self):
        assert _find_ref_in_posted([{"identifier": "unknown"}], self.posted) is None

    def test_empty_refs(self):
        assert _find_ref_in_posted([], self.posted) is None


# ============================================================================
# posted_alerts prune predicate  (v2.7.1 regression)
# ============================================================================

def prune(posted: dict, active_ids: set, now: float) -> dict:
    """Mirror of the prune comprehension in _task_alerts."""
    cutoff = now - 48 * 3600
    return {
        aid: info for aid, info in posted.items()
        if aid in active_ids
        or not ((info.get("cleared") or info.get("superseded_by")
                 or info.get("suppressed"))
                and info.get("ts", 0) < cutoff)
    }


class TestPrune:
    NOW = 1_000_000_000.0
    OLD = NOW - 72 * 3600     # 3 days old
    NEW = NOW - 1 * 3600      # 1 hour old

    def test_source_still_matches_this_mirror(self):
        """If the prune comprehension in weather_bot.py is edited, this
        reminds the editor to update the mirrored predicate above."""
        m = re.search(r"if aid in active_ids\s*\n\s*or not \(\(info\.get\(\"cleared\"\) "
                      r"or info\.get\(\"superseded_by\"\)\s*\n\s*or info\.get\(\"suppressed\"\)\)",
                      SRC)
        assert m, "prune logic in _task_alerts changed - update tests/test_weather_bot.py"

    @pytest.mark.parametrize("flag", ["cleared", "superseded_by", "suppressed"])
    def test_old_resolved_entry_pruned(self, flag):
        p = {"a": {"ts": self.OLD, flag: True}}
        assert prune(p, set(), self.NOW) == {}

    def test_recent_resolved_entry_kept(self):
        p = {"a": {"ts": self.NEW, "superseded_by": "b"}}
        assert "a" in prune(p, set(), self.NOW)

    def test_active_entry_kept_regardless_of_age(self):
        p = {"a": {"ts": self.OLD, "suppressed": True}}
        assert "a" in prune(p, {"a"}, self.NOW)

    def test_unresolved_entry_kept(self):
        # not cleared/superseded/suppressed -> the clear-loop's job, not prune's
        p = {"a": {"ts": self.OLD}}
        assert "a" in prune(p, set(), self.NOW)


# ============================================================================
# Config-driven /help text  (v2.9.2 region-neutrality)
# ============================================================================

class TestWeeklyWhen:
    """/help must describe the CONFIGURED weekly-summary slot, not a hardcoded
    'Sunday 8 AM ET'."""

    @staticmethod
    def make(day, hour):
        ns = {"WEEKLY_DAY": day, "WEEKLY_HOUR": hour}
        return _exec_block("def _fmt_weekly_when", "_HELP_EMBED = {",
                           ns)["_fmt_weekly_when"]()

    def test_default_sunday_8am(self):
        assert self.make(6, 8) == "Sunday 8 AM ET"

    def test_monday_1pm(self):
        assert self.make(0, 13) == "Monday 1 PM ET"

    def test_midnight_is_12_am(self):
        assert self.make(2, 0) == "Wednesday 12 AM ET"

    def test_noon_is_12_pm(self):
        assert self.make(4, 12) == "Friday 12 PM ET"


class TestHelpEmbedConfigDriven:
    """The /help embed must interpolate the configured radar station and weekly
    schedule rather than baking in KDIX / 'Sunday 8 AM ET' (region-neutrality)."""

    help_src = SRC[SRC.index("_HELP_EMBED = {"):SRC.index("_ready_once = False")]

    def test_radar_station_not_hardcoded(self):
        assert "KDIX" not in self.help_src
        assert "RADAR_STATION" in self.help_src

    def test_weekly_schedule_not_hardcoded(self):
        assert "Sunday 8 AM" not in self.help_src
        assert "_fmt_weekly_when()" in self.help_src
