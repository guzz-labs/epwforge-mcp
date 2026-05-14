"""Unit tests for the EPW parser + analysis helpers.

These don't hit the network — they exercise the pure-Python parser and
the unit-conversion / aggregation helpers used by analyze_epw and
compare_scenarios. The full end-to-end smoke test that hits the live
EPWForge API lives at scripts/smoke_test_new_tools.py.
"""

from __future__ import annotations

import pytest

from epwforge_mcp.epw_parser import (
    c_to_f,
    daily_means_by_date,
    design_conditions_F,
    format_md,
    m_to_ft,
    monthly_means,
    parse_epw,
    percentile,
)


def _synthesize_epw(*, db_pattern_c: list[float] | None = None) -> str:
    """Build a minimal but valid 8760-hour EPW for parser tests.

    Default pattern: a clean sine-ish curve from -10 °C in Jan to +30 °C
    in Jul so the percentile / monthly / peak-day helpers have something
    physically plausible to chew on.
    """
    import math

    header = (
        "LOCATION,Test City,NY,USA,EPWForge Synthetic,000000,40.71,-74.01,-5.0,10.0\n"
        "DESIGN CONDITIONS,0\n"
        "TYPICAL/EXTREME PERIODS,0\n"
        "GROUND TEMPERATURES,0\n"
        "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0\n"
        "COMMENTS 1,test\n"
        "COMMENTS 2,test\n"
        "DATA PERIODS,1,1,Data,Sunday,1/1,12/31\n"
    )

    rows: list[str] = []
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    hour_of_year = 0
    for m_idx, dim in enumerate(days_in_month):
        month = m_idx + 1
        for day in range(1, dim + 1):
            for h in range(1, 25):
                # Annual sine: peak around mid-Jul, trough around mid-Jan.
                if db_pattern_c is not None:
                    db = db_pattern_c[hour_of_year]
                else:
                    seasonal = 10.0 + 20.0 * math.sin(2 * math.pi * (hour_of_year / 8760.0 - 0.27))
                    diurnal = 5.0 * math.sin(2 * math.pi * (h - 6) / 24.0)
                    db = seasonal + diurnal
                dp = db - 5.0  # Crude; just needs to be < db for sanity.
                rh = 60.0
                ghi = max(0.0, 600.0 * math.sin(math.pi * (h - 6) / 12.0)) if 6 <= h <= 18 else 0.0
                ws = 3.0
                # 35 EPW fields total; we only populate the ones we need.
                row = [str(2020), str(month), str(day), str(h), "0", "?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9?9*9*9?9?9"]
                # Pad to field 22 (wind speed) with zeros where unused.
                row += [
                    f"{db:.1f}", f"{dp:.1f}", f"{rh:.0f}",   # 6,7,8
                    "101325", "0", "0", "0",                  # 9-12
                    f"{ghi:.0f}", "0", "0",                   # 13-15
                    "0", "0", "0", "0",                       # 16-19
                    "0", f"{ws:.1f}",                         # 20,21
                ]
                rows.append(",".join(row))
                hour_of_year += 1

    return header + "\n".join(rows) + "\n"


# ── Unit conversions ─────────────────────────────────────────────────────
def test_c_to_f_known_values():
    assert c_to_f(0.0) == 32.0
    assert c_to_f(100.0) == 212.0
    assert c_to_f(-40.0) == -40.0


def test_m_to_ft():
    # 1 m = 3.28084 ft
    assert abs(m_to_ft(10.0) - 32.8084) < 1e-3


# ── percentile ────────────────────────────────────────────────────────────
def test_percentile_basic():
    vals = list(range(1, 101))  # 1..100
    assert percentile(vals, 1.0) == 1.0 or percentile(vals, 1.0) == 2.0
    assert percentile(vals, 50.0) == 50.0 or percentile(vals, 50.0) == 51.0
    assert percentile(vals, 99.0) == 99.0 or percentile(vals, 99.0) == 100.0


def test_percentile_endpoints():
    assert percentile([5.0], 0.0) == 5.0
    assert percentile([5.0], 100.0) == 5.0


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50.0)


# ── Aggregation helpers ──────────────────────────────────────────────────
def test_daily_means_by_date():
    out = daily_means_by_date(
        values=[10.0, 20.0, 30.0, 40.0],
        months=[1, 1, 1, 2],
        days=[1, 1, 2, 1],
    )
    assert out[(1, 1)] == 15.0
    assert out[(1, 2)] == 30.0
    assert out[(2, 1)] == 40.0


def test_monthly_means_returns_12():
    out = monthly_means([1.0, 2.0, 3.0], [1, 6, 12])
    assert len(out) == 12
    assert out[0] == 1.0
    assert out[5] == 2.0
    assert out[11] == 3.0


def test_format_md():
    assert format_md(1, 5) == "01-05"
    assert format_md(12, 31) == "12-31"


# ── End-to-end parser ────────────────────────────────────────────────────
def test_parse_epw_synthesized_round_trip():
    text = _synthesize_epw()
    epw = parse_epw(text)
    # 8760 hourly records.
    assert len(epw.hourly) == 8760
    # Header decoded.
    assert epw.location.city == "Test City"
    assert epw.location.latitude == pytest.approx(40.71)
    assert epw.location.longitude == pytest.approx(-74.01)
    assert epw.location.elevation_m == 10.0
    # Reasonable annual mean for the synthetic sine pattern (~10°C).
    annual_mean_c = sum(epw.hourly.dry_bulb_c) / len(epw.hourly.dry_bulb_c)
    assert 5.0 < annual_mean_c < 15.0


def test_design_conditions_synthesized():
    epw = parse_epw(_synthesize_epw())
    dc = design_conditions_F(epw)
    # Cooling 1% should be the warmest hours (synthetic peak ~30+5 °C ≈ 95°F).
    assert 85.0 < dc["cooling_db_F"] < 105.0
    # Heating 99% should be the coldest hours (synthetic trough ~-10-5 °C ≈ 5°F).
    assert -10.0 < dc["heating_db_F"] < 25.0
    # Dewpoint follows DB - 5 °C in synthetic data.
    assert dc["dewpoint_F"] < dc["cooling_db_F"]


def test_parse_epw_rejects_non_location_header():
    with pytest.raises(ValueError):
        parse_epw("NOTLOCATION,bogus\n" + "\n" * 7)


def test_parse_epw_tolerates_blank_trailing_lines():
    epw = parse_epw(_synthesize_epw() + "\n\n  \n")
    assert len(epw.hourly) == 8760
