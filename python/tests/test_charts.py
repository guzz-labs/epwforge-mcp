"""Unit tests for the SVG chart generators.

These don't hit the network — they exercise the pure-Python SVG renderer
on synthetic fixtures and assert the output is valid SVG, contains the
expected legend labels, and doesn't leak NaN / None / undefined.
"""

from __future__ import annotations

from epwforge_mcp.charts import compare_scenarios_svg, diurnal_profile_svg
from tests.test_epw_parser import _synthesize_epw
from epwforge_mcp.epw_parser import parse_epw


# ── diurnal_profile_svg ─────────────────────────────────────────────────
def test_diurnal_profile_svg_returns_valid_svg():
    epw = parse_epw(_synthesize_epw())
    svg = diurnal_profile_svg(epw)
    assert svg.startswith('<svg')
    assert svg.endswith('</svg>')
    # Must contain the title + both highlighted months in the legend.
    assert "Diurnal Temperature Profile" in svg
    assert "January" in svg
    assert "July" in svg
    assert "Annual avg" in svg


def test_diurnal_profile_svg_no_undefined_values():
    epw = parse_epw(_synthesize_epw())
    svg = diurnal_profile_svg(epw)
    for needle in ("nan", "None", "undefined", "NaN", "null"):
        assert needle not in svg, f"chart leaked {needle!r}"


def test_diurnal_profile_svg_has_y_axis_labels_in_F():
    epw = parse_epw(_synthesize_epw())
    svg = diurnal_profile_svg(epw)
    # Y axis is in °F (e.g. "30°F", "40°F" labels)
    assert "°F" in svg


# ── compare_scenarios_svg ───────────────────────────────────────────────
_BASELINE = {"cooling_db_F": 87.4, "heating_db_F": 16.5, "dewpoint_F": 73.4}
_SCENARIOS = [
    {
        "config": {"lat": 40.71, "lon": -74.01, "ssp": "ssp245", "year": 2050, "percentile": 50},
        "cooling_db_F": 91.4, "cooling_db_delta_F": 4.0,
        "heating_db_F": 21.2, "heating_db_delta_F": 4.7,
        "dewpoint_F": 77.2,   "dewpoint_delta_F": 3.8,
    },
    {
        "config": {"lat": 40.71, "lon": -74.01, "ssp": "ssp370", "year": 2090, "percentile": 90},
        "cooling_db_F": 101.6, "cooling_db_delta_F": 14.2,
        "heating_db_F": 26.8,  "heating_db_delta_F": 10.3,
        "dewpoint_F": 88.2,    "dewpoint_delta_F": 14.8,
    },
]


def test_compare_scenarios_svg_basic():
    svg = compare_scenarios_svg(_BASELINE, _SCENARIOS)
    assert svg.startswith('<svg')
    assert svg.endswith('</svg>')
    assert "Design-Condition Deltas" in svg
    # Baseline summary line includes all three values
    assert "87.4" in svg and "16.5" in svg and "73.4" in svg
    # Both scenario row labels include the SSP
    assert "SSP245" in svg and "SSP370" in svg
    # Delta values appear with explicit + sign for positive
    assert "+4.0" in svg or "4.0" in svg
    assert "+14.2" in svg


def test_compare_scenarios_svg_no_undefined():
    svg = compare_scenarios_svg(_BASELINE, _SCENARIOS)
    for needle in ("nan", "None", "undefined", "NaN", "null"):
        assert needle not in svg


def test_compare_scenarios_svg_handles_empty():
    svg = compare_scenarios_svg(_BASELINE, [])
    assert "No scenarios" in svg


def test_compare_scenarios_svg_handles_negative_deltas():
    # SSP1-2.6 in some Arctic-cooling-effect spots could go negative.
    scenarios = [
        {
            "config": {"lat": 60.0, "lon": -150.0, "ssp": "ssp126", "year": 2030},
            "cooling_db_F": 75.0, "cooling_db_delta_F": -2.5,
            "heating_db_F": -10.0, "heating_db_delta_F": -3.5,
            "dewpoint_F": 50.0, "dewpoint_delta_F": -1.2,
        },
    ]
    svg = compare_scenarios_svg(_BASELINE, scenarios)
    assert "-2.5" in svg
    assert "-3.5" in svg
