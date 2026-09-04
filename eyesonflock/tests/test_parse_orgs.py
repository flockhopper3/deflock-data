#!/usr/bin/env python3
"""Tests for parse_orgs_lib — organization name parser for ALPR transparency data."""

import importlib
import sys
from pathlib import Path

# Allow import of parse_orgs_lib from sharing-network/scripts/
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

from parse_orgs_lib import (
    _strip_status_tags,
    canonical_slug,
    parse_org_name,
    portal_as_org_name,
    portal_canonical_slug,
    slugify,
)


# ---------------------------------------------------------------------------
# Standard PD patterns
# ---------------------------------------------------------------------------

class TestStandardPD:
    def test_simple_city_state_pd(self):
        result = parse_org_name("Wichita KS PD")
        assert result["city"] == "Wichita"
        assert result["state"] == "KS"
        assert result["type"] == "pd"

    def test_multi_word_city_pd(self):
        result = parse_org_name("North Charleston SC PD")
        assert result["city"] == "North Charleston"
        assert result["state"] == "SC"
        assert result["type"] == "pd"

    def test_raw_name_preserved(self):
        result = parse_org_name("Wichita KS PD")
        assert result["raw_name"] == "Wichita KS PD"


# ---------------------------------------------------------------------------
# Sheriff's Office (SO) patterns
# ---------------------------------------------------------------------------

class TestSO:
    def test_county_state_so(self):
        """Standard: County State SO"""
        result = parse_org_name("Oneida County WI SO")
        assert result["city"] == "Oneida County"
        assert result["state"] == "WI"
        assert result["type"] == "so"

    def test_edgar_county_so(self):
        result = parse_org_name("Edgar County IL SO")
        assert result["city"] == "Edgar County"
        assert result["state"] == "IL"
        assert result["type"] == "so"

    def test_so_before_state(self):
        """Variant: County SO State"""
        result = parse_org_name("Allegan County SO MI")
        assert result["city"] == "Allegan County"
        assert result["state"] == "MI"
        assert result["type"] == "so"

    def test_so_before_state_variant2(self):
        result = parse_org_name("Knox County SO NE")
        assert result["city"] == "Knox County"
        assert result["state"] == "NE"
        assert result["type"] == "so"


# ---------------------------------------------------------------------------
# Federal patterns
# ---------------------------------------------------------------------------

class TestFederal:
    def test_federal_no_state(self):
        result = parse_org_name("[Federal] US Postal Inspection Service")
        assert result["type"] == "federal"
        assert result["state"] == "DC"
        assert result["raw_name"] == "[Federal] US Postal Inspection Service"

    def test_federal_with_state(self):
        result = parse_org_name("[Federal] Fort Eustis VA US Army")
        assert result["type"] == "federal"
        assert result["state"] == "VA"

    def test_federal_inactive(self):
        result = parse_org_name("[Federal] ATF Louisville KY [inactive]")
        assert result["type"] == "federal"
        assert result["state"] == "KY"
        # raw_name should preserve original
        assert result["raw_name"] == "[Federal] ATF Louisville KY [inactive]"


# ---------------------------------------------------------------------------
# School / University patterns
# ---------------------------------------------------------------------------

class TestSchool:
    def test_college(self):
        result = parse_org_name("Southern College of Optometry TN")
        assert result["type"] == "school"
        assert result["state"] == "TN"

    def test_university(self):
        result = parse_org_name("San Jose State University CA")
        assert result["type"] == "school"
        assert result["state"] == "CA"

    def test_school_keyword(self):
        result = parse_org_name("Al-Huda School MD")
        assert result["type"] == "school"
        assert result["state"] == "MD"


# ---------------------------------------------------------------------------
# City of pattern
# ---------------------------------------------------------------------------

class TestCityOf:
    def test_city_of(self):
        result = parse_org_name("City of Sawmills NC")
        assert result["city"] == "Sawmills"
        assert result["state"] == "NC"

    def test_city_of_multi_word(self):
        result = parse_org_name("City of Castle Pines CO")
        assert result["city"] == "Castle Pines"
        assert result["state"] == "CO"


# ---------------------------------------------------------------------------
# Constable pattern
# ---------------------------------------------------------------------------

class TestConstable:
    def test_harris_county_const(self):
        result = parse_org_name("Harris County Const TX Pct 8")
        # Precinct is included in the city field so different precincts get
        # distinct canonical slugs.
        assert result["city"] == "Harris County Constable Pct 8"
        assert result["state"] == "TX"
        assert result["type"] == "pd"

    def test_fort_bend_county_const(self):
        result = parse_org_name("Fort Bend County Const TX Pct 1")
        assert result["city"] == "Fort Bend County Constable Pct 1"
        assert result["state"] == "TX"
        assert result["type"] == "pd"

    def test_constable_with_paren_state(self):
        # Regression: paren-state pattern previously consumed this before
        # the constable regex could match, producing type='other'.
        result = parse_org_name("Harris County Const Pct 4 (TX)")
        assert result["city"] == "Harris County Constable Pct 4"
        assert result["state"] == "TX"
        assert result["type"] == "pd"

    def test_different_precincts_dont_collapse(self):
        a = parse_org_name("Harris County Const TX Pct 4")
        b = parse_org_name("Harris County Const TX Pct 5")
        assert a["city"] != b["city"]


# ---------------------------------------------------------------------------
# State-prefix pattern (XX - Name)
# ---------------------------------------------------------------------------

class TestStatePrefix:
    def test_state_prefix_pd(self):
        result = parse_org_name("IN - Newburgh PD")
        assert result["state"] == "IN"
        assert result["type"] == "pd"
        assert result["city"] == "Newburgh"

    def test_state_prefix_so(self):
        result = parse_org_name("AL - Autauga County SO")
        assert result["state"] == "AL"
        assert result["type"] == "so"
        assert result["city"] == "Autauga County"


# ---------------------------------------------------------------------------
# Dash-state pattern (Name-XX)
# ---------------------------------------------------------------------------

class TestDashState:
    def test_dash_state(self):
        result = parse_org_name("ACRATT -CA")
        assert result["state"] == "CA"

    def test_dash_state_no_space(self):
        result = parse_org_name("Mendocino County SO-CA")
        assert result["state"] == "CA"
        assert result["type"] == "so"


# ---------------------------------------------------------------------------
# Parenthesized state
# ---------------------------------------------------------------------------

class TestParenState:
    def test_paren_state_pd(self):
        result = parse_org_name("Arlington PD (WA)")
        assert result["state"] == "WA"
        assert result["type"] == "pd"

    def test_paren_state_so(self):
        result = parse_org_name("Lassen County SO (CA)")
        assert result["state"] == "CA"
        assert result["type"] == "so"


# ---------------------------------------------------------------------------
# Inactive tag handling
# ---------------------------------------------------------------------------

class TestInactive:
    def test_inactive_stripped_for_parsing(self):
        result = parse_org_name("[Federal] ATF Louisville KY [inactive]")
        assert result["state"] == "KY"

    def test_inactive_case_insensitive(self):
        result = parse_org_name("[Federal] FBI [Inactive]")
        assert result["type"] == "federal"

    def test_inactive_so(self):
        result = parse_org_name("Ness County KS SO [Inactive]")
        assert result["state"] == "KS"
        assert result["type"] == "so"


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert slugify("Wichita KS PD") == "wichita-ks-pd"

    def test_special_chars(self):
        assert slugify("[Federal] ATF Louisville KY") == "federal-atf-louisville-ky"

    def test_multiple_spaces(self):
        slug = slugify("Harris County  Const TX Pct 8")
        assert "--" not in slug  # No double hyphens

    def test_parentheses(self):
        slug = slugify("Arlington PD (WA)")
        assert "(" not in slug
        assert ")" not in slug

    def test_ampersand(self):
        slug = slugify("Baylor Scott & White PD (TX)")
        assert "&" not in slug

    def test_apostrophe(self):
        slug = slugify("Bellevue ID Marshal's Office")
        assert "'" not in slug


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_dead_old_prefix(self):
        result = parse_org_name("(Dead/Old) Plymouth IN PD")
        assert result["state"] == "IN"
        assert result["type"] == "pd"

    def test_town_of(self):
        result = parse_org_name("Town of Woodside CA (SMCSO)")
        assert result["state"] == "CA"

    def test_village_of(self):
        result = parse_org_name("Village of Bloomingdale IL PD")
        assert result["state"] == "IL"
        assert result["type"] == "pd"

    def test_returns_dict_with_required_keys(self):
        result = parse_org_name("Wichita KS PD")
        assert "raw_name" in result
        assert "city" in result
        assert "state" in result
        assert "type" in result


# ---------------------------------------------------------------------------
# Status-tag stripping
# ---------------------------------------------------------------------------

class TestStatusTagStripping:
    def test_strip_inactive_bracket(self):
        assert _strip_status_tags("Allen Park MI PD [Inactive]") == "Allen Park MI PD"

    def test_strip_inactive_case_insensitive(self):
        assert _strip_status_tags("Ferndale MI PD [INACTIVE]") == "Ferndale MI PD"

    def test_strip_dnu_paren(self):
        assert _strip_status_tags("Newport PD RI (DNU)") == "Newport PD RI"

    def test_strip_dead_old_prefix(self):
        assert _strip_status_tags("(Dead/Old) Plymouth IN PD") == "Plymouth IN PD"

    def test_strip_do_not_use_suffix(self):
        assert (
            _strip_status_tags("Cass County MO SO - DO NOT USE DUPLICATE ACCOUNT")
            == "Cass County MO SO"
        )

    def test_preserves_name_without_tags(self):
        assert _strip_status_tags("Wichita KS PD") == "Wichita KS PD"


# ---------------------------------------------------------------------------
# Canonical slug collapse
# ---------------------------------------------------------------------------

class TestCanonicalSlug:
    def _slug(self, name):
        return canonical_slug(parse_org_name(name), name)

    def test_word_order_converges(self):
        assert self._slug("Allen Park MI PD") == self._slug("Allen Park PD MI")

    def test_state_prefix_converges(self):
        assert self._slug("Storm Lake IA PD") == self._slug("IA - Storm Lake PD")

    def test_paren_state_converges(self):
        assert self._slug("Arlington WA PD") == self._slug("Arlington PD (WA)")

    def test_county_suffix_converges(self):
        assert self._slug("Coconino County AZ SO") == self._slug("Coconino AZ SO")

    def test_so_word_order_converges(self):
        assert self._slug("Washington County IN SO") == self._slug("Washington County SO IN")

    def test_inactive_tag_converges(self):
        assert self._slug("Brooklyn Park MN PD") == self._slug("Brooklyn Park MN PD [Inactive]")

    def test_constable_precincts_stay_distinct(self):
        a = self._slug("Harris County Const TX Pct 4")
        b = self._slug("Harris County Const TX Pct 5")
        assert a != b

    def test_never_returns_empty(self):
        assert self._slug("DO NOT USE") != ""

    def test_unparseable_still_unique(self):
        # Numeric and unparseable names still produce distinct slugs
        assert self._slug("265") != self._slug("988")


class TestPortalCanonicalSlug:
    def test_pd_portal_matches_shared_name(self):
        portal = {"city": "Allen Park", "state": "MI", "type": "PD"}
        expected = canonical_slug(parse_org_name("Allen Park MI PD"), "Allen Park MI PD")
        assert portal_canonical_slug(portal) == expected

    def test_sd_portal_matches_so_name(self):
        portal = {"city": None, "county": "Spokane", "state": "WA", "type": "SD"}
        expected = canonical_slug(parse_org_name("Spokane County WA SO"), "Spokane County WA SO")
        assert portal_canonical_slug(portal) == expected

    def test_portal_as_org_name_sd(self):
        portal = {"city": None, "county": "Spokane", "state": "WA", "type": "SD"}
        assert portal_as_org_name(portal) == "Spokane County WA SO"


# ---------------------------------------------------------------------------
# _portal_meta / portal_slug preservation
# ---------------------------------------------------------------------------

# importlib needed because the module name starts with a digit
_parse01 = importlib.import_module("01_parse_orgs")


class TestPortalSlugPreservation:
    def test_portal_meta_carries_portal_slug(self):
        portal = {
            "slug": "allen-park-mi",
            "total_cameras": 12,
            "total_searches": 34,
            "vehicles_captured": 56,
            "population": 28000,
            "hotlist_hits": 3,
            "data_retention": 30,
        }
        meta = _parse01._portal_meta(portal)
        assert meta["portal_slug"] == "allen-park-mi"
        assert meta["cameras"] == 12

    def test_empty_meta_has_null_portal_slug(self):
        meta = _parse01._empty_meta()
        assert meta["portal_slug"] is None


# ---------------------------------------------------------------------------
# Spelled-out Sheriff / Police suffix normalization
# ---------------------------------------------------------------------------

class TestSpelledOutSuffixes:
    def test_sheriffs_office_with_trailing_state(self):
        r = parse_org_name("Kings County Sheriff's Office CA")
        assert r["type"] == "so"
        assert r["state"] == "CA"
        assert r["city"] == "Kings County"
        assert canonical_slug(r, "Kings County Sheriff's Office CA") == "kings-county-ca-so"

    def test_sheriffs_office_with_state_in_middle(self):
        r = parse_org_name("Tompkins County NY Sheriff's Office")
        assert r["type"] == "so"
        assert r["state"] == "NY"
        assert r["city"] == "Tompkins County"
        assert canonical_slug(r, "Tompkins County NY Sheriff's Office") == "tompkins-county-ny-so"

    def test_lone_sheriff_with_paren_state(self):
        r = parse_org_name("San Luis Obispo County (CA) Sheriff")
        assert r["type"] == "so"
        assert r["state"] == "CA"
        assert r["city"] == "San Luis Obispo County"

    def test_sheriffs_dept_variant(self):
        r = parse_org_name("Clark Co. IL Sheriff's Dept.")
        assert r["type"] == "so"
        assert r["state"] == "IL"

    def test_police_department_with_paren_state(self):
        r = parse_org_name("Everett (WA) Police Department")
        assert r["type"] == "pd"
        assert r["state"] == "WA"
        assert r["city"] == "Everett"
        assert canonical_slug(r, "Everett (WA) Police Department") == "everett-wa-pd"

    def test_police_department_dash_state(self):
        r = parse_org_name("Sparks Police Department - NV")
        assert r["type"] == "pd"
        assert r["state"] == "NV"
        assert r["city"] == "Sparks"
        assert canonical_slug(r, "Sparks Police Department - NV") == "sparks-nv-pd"

    def test_stateless_sheriff_parses_city_and_type(self):
        """Yuba case: no state in name, but city+type still extracted so the
        rescue pass in 01_parse_orgs can match it to the portal."""
        r = parse_org_name("Yuba County Sheriffs Office")
        assert r["type"] == "so"
        assert r["state"] is None
        assert r["city"] == "Yuba County"


# ── Alias map + shared-name resolver (shared by steps 03 and 04) ─────────────

from parse_orgs_lib import build_alias_map, make_shared_name_resolver  # noqa: E402


class TestAliasMap:
    def test_build_alias_map_covers_raw_name_and_aliases(self):
        parsed = {
            "berkeley-ca-pd": {"raw_name": "Berkeley CA PD", "aliases": ["Berkeley", "Berkeley PD CA"]},
            "yuba-county-ca-so": {"raw_name": "Yuba County CA SO", "aliases": ["Yuba County Sheriffs Office"]},
        }
        assert build_alias_map(parsed) == {
            "Berkeley CA PD": "berkeley-ca-pd",
            "Berkeley": "berkeley-ca-pd",
            "Berkeley PD CA": "berkeley-ca-pd",
            "Yuba County CA SO": "yuba-county-ca-so",
            "Yuba County Sheriffs Office": "yuba-county-ca-so",
        }

    def test_build_alias_map_tolerates_missing_aliases_key(self):
        assert build_alias_map({"x-tx-pd": {"raw_name": "X TX PD"}}) == {"X TX PD": "x-tx-pd"}

    def test_resolver_prefers_alias_map_then_canonical(self):
        resolve = make_shared_name_resolver({"Berkeley": "berkeley-ca-pd"})
        assert resolve("Berkeley") == "berkeley-ca-pd"
        assert resolve("Allen Park MI PD") == "allen-park-mi-pd"

    def test_resolver_with_empty_map_is_pure_canonical(self):
        resolve = make_shared_name_resolver({})
        assert resolve("Berkeley") == canonical_slug(parse_org_name("Berkeley"), "Berkeley")
