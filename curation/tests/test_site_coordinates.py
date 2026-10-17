"""The site-coordinate check: parsing, the polygon test, and what it says when wrong.

The two cases that motivated it are real release sites (2026-09-25): Lincoln, UK stored
as 53°15', 000°34' (no sign, so in the North Sea) and a Spanish site at 37.05 N, 2.35 E
(in the Mediterranean). Both must be reported with "negate the longitude" as the fix,
and ordinary coastal sites must not be.
"""
from __future__ import annotations

import pytest

from malavi_curation import site_coordinates as sc


class TestParse:
    @pytest.mark.parametrize("text, lat, want", [
        ("11°28.20000'", True, 11.47),
        ("-021°46.98000'", True, -21.783),
        ("46°14.99167'N", True, 46.24986),
        ("12°30.00000'S", True, -12.5),
        ("075°56.40000'", False, 75.94),
        ("46.62 W", False, -46.62),
        ("11.47", True, 11.47),
        ("-46,62", False, -46.62),                # decimal comma
        ("12°30'15\"S", True, -12.50417),        # seconds
        (" 5°0.0' ", True, 5.0),
    ])
    def test_reads_the_forms_submitters_use(self, text, lat, want):
        assert sc.parse_coordinate(text, is_latitude=lat) == pytest.approx(want, abs=1e-4)

    @pytest.mark.parametrize("text, lat", [
        ("x", True), ("", True), (None, False),
        ("95", True),                # latitude past 90
        ("181", False),              # longitude past 180
        ("12°61'", True),            # minutes past 60
        ("46°14.99167'N", False),    # an N on a longitude
        ("46.62 W", True),           # a W on a latitude
    ])
    def test_refuses_what_is_not_a_coordinate(self, text, lat):
        assert sc.parse_coordinate(text, is_latitude=lat) is None

    def test_minus_and_south_cancel_as_at_ingest(self):
        assert sc.parse_coordinate("-12°30.0'S", is_latitude=True) == pytest.approx(12.5)


class TestAtlas:
    def test_malavi_spellings_resolve(self):
        for name, admin in [("United States", "United States of America"),
                            ("Czech Republic", "Czechia"), ("Korea, South", "South Korea"),
                            ("Turkey", "Turkey"), ("Guadeloupe", "France"),
                            ("Sweden", "Sweden"), ("United Kingdom", "United Kingdom")]:
            country = sc.country_for(name)
            assert country is not None, name
            assert country.admin == admin

    def test_an_unknown_country_is_none_not_a_guess(self):
        assert sc.country_for("Netherlands Antilles") is None
        assert sc.country_for("Unknown Country") is None
        assert sc.country_for("") is None

    def test_a_feature_beats_a_sovereignty_of_the_same_name(self):
        # "France" the sovereignty covers New Caledonia; the feature must be France itself.
        france = sc.country_for("France")
        assert sc.contains(france, 2.35, 48.86)          # Paris
        assert not sc.contains(france, 166.45, -22.27)   # Nouméa


class TestCheck:
    def test_lincoln_without_its_minus_sign_is_reported_with_the_fix(self):
        result = sc.check_site("United Kingdom", 53.25, 0.5667)
        assert result.verdict == "outside"
        assert 10 < result.distance_km < 30
        assert result.found_in is None                     # the North Sea
        assert result.fix == "negate the longitude"

    def test_a_spanish_site_in_the_mediterranean_is_reported(self):
        result = sc.check_site("Spain", 37.05, 2.35)
        assert result.verdict == "outside"
        assert result.fix == "negate the longitude"

    def test_a_point_in_another_country_names_it(self):
        result = sc.check_site("Brazil", -21.78, 46.62)
        assert result.verdict == "outside"
        assert result.found_in == "Madagascar"
        assert result.fix == "negate the longitude"

    def test_swapped_latitude_and_longitude_is_diagnosed(self):
        # Lund is 55.7 N, 13.2 E; swapped, it is in the Indian Ocean off Somalia.
        result = sc.check_site("Sweden", 13.2, 55.7)
        assert result.verdict == "outside"
        assert result.fix == "swap latitude and longitude"

    def test_inside_is_silent(self):
        assert sc.check_site("Sweden", 55.7, 13.2).verdict == "inside"
        assert sc.check_site("Brazil", -21.78, -46.62).verdict == "inside"

    def test_a_coastal_site_a_few_km_out_is_near_not_outside(self):
        # The Öresund off Malmö: about 5 km from the Swedish coast.
        result = sc.check_site("Sweden", 55.35, 12.9)
        assert result.verdict == "near"
        assert result.distance_km < sc.TOLERANCE_KM

    def test_an_overseas_department_tests_against_france(self):
        result = sc.check_site("Guadeloupe", 16.25, -61.58)
        assert result.verdict == "inside" and result.admin == "France"

    def test_an_unknown_country_is_not_tested(self):
        result = sc.check_site("Netherlands Antilles", 12.1, -68.9)
        assert result.verdict == "country_unknown"
        assert result.as_dict()["country"] == "Netherlands Antilles"
