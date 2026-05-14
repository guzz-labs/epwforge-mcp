"""Minimal EPW parser — header LOCATION line + 8760 hourly records.

Pure Python, no numpy / pandas dependency. Returns plain Python lists of
floats so the analysis tools (`analyze_epw`, `compare_scenarios`) can work
on any platform without pulling in scientific stack.

Field indices follow the EPW Auxiliary Programs spec
(https://bigladdersoftware.com/epx/docs/22-2/auxiliary-programs/).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


# ── EPW record-field indices (0-based) ───────────────────────────────────
_F_YEAR = 0
_F_MONTH = 1
_F_DAY = 2
_F_HOUR = 3
_F_DRY_BULB_C = 6
_F_DEW_POINT_C = 7
_F_REL_HUMIDITY = 8
_F_GHI_WH_M2 = 13
_F_WIND_SPEED_MS = 21


@dataclass(frozen=True)
class EPWLocation:
    """Decoded LOCATION header line."""

    city: str
    state: str
    country: str
    source: str
    wmo: str
    latitude: float
    longitude: float
    timezone: float       # hours from UTC
    elevation_m: float


@dataclass(frozen=True)
class EPWHourly:
    """8760 hourly records as parallel float lists."""

    year: list[int]
    month: list[int]
    day: list[int]
    hour: list[int]
    dry_bulb_c: list[float]
    dew_point_c: list[float]
    rel_humidity_pct: list[float]
    ghi_wh_m2: list[float]
    wind_speed_ms: list[float]

    def __len__(self) -> int:
        return len(self.dry_bulb_c)


@dataclass(frozen=True)
class EPWFile:
    location: EPWLocation
    hourly: EPWHourly


def parse_epw(text: str) -> EPWFile:
    """Parse a raw EPW text blob into an EPWFile.

    Tolerates trailing blank lines and short rows; raises ValueError if
    the file doesn't begin with a LOCATION header.
    """
    lines = text.splitlines()
    if not lines or not lines[0].upper().startswith("LOCATION"):
        raise ValueError("EPW file does not start with a LOCATION header line")

    loc_parts = lines[0].split(",")
    location = EPWLocation(
        city=loc_parts[1].strip(),
        state=loc_parts[2].strip(),
        country=loc_parts[3].strip(),
        source=loc_parts[4].strip(),
        wmo=loc_parts[5].strip(),
        latitude=float(loc_parts[6]),
        longitude=float(loc_parts[7]),
        timezone=float(loc_parts[8]),
        elevation_m=float(loc_parts[9]),
    )

    # 8 EPW header lines per spec: LOCATION, DESIGN CONDITIONS,
    # TYPICAL/EXTREME PERIODS, GROUND TEMPERATURES, HOLIDAYS/DAYLIGHT
    # SAVINGS, COMMENTS 1, COMMENTS 2, DATA PERIODS.
    data_lines = lines[8:]

    year, month, day, hour = [], [], [], []
    db, dp, rh, ghi, ws = [], [], [], [], []
    for line in data_lines:
        if not line.strip():
            continue
        f = line.split(",")
        # Need at least up to wind speed (field 21). Some synthesized EPWs
        # truncate trailing optional fields; skip rows that are too short.
        if len(f) <= _F_WIND_SPEED_MS:
            continue
        year.append(int(f[_F_YEAR]))
        month.append(int(f[_F_MONTH]))
        day.append(int(f[_F_DAY]))
        hour.append(int(f[_F_HOUR]))
        db.append(float(f[_F_DRY_BULB_C]))
        dp.append(float(f[_F_DEW_POINT_C]))
        rh.append(float(f[_F_REL_HUMIDITY]))
        ghi.append(float(f[_F_GHI_WH_M2]))
        ws.append(float(f[_F_WIND_SPEED_MS]))

    return EPWFile(
        location=location,
        hourly=EPWHourly(year, month, day, hour, db, dp, rh, ghi, ws),
    )


# ── Unit conversions (imperial output for the MCP tools) ─────────────────
def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def m_to_ft(m: float) -> float:
    return m * 3.28084


# ── Aggregation helpers ──────────────────────────────────────────────────
def percentile(values: list[float], pct: float) -> float:
    """Simple percentile by sorted-index, no interpolation. pct in [0,100]."""
    if not values:
        raise ValueError("percentile of empty list")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(round(pct / 100.0 * (n - 1)))))
    return sorted_vals[idx]


def daily_means_by_date(
    values: list[float],
    months: list[int],
    days: list[int],
) -> dict[tuple[int, int], float]:
    """Group hourly values by (month, day) and return the daily mean."""
    by_day: dict[tuple[int, int], list[float]] = {}
    for v, m, d in zip(values, months, days):
        by_day.setdefault((m, d), []).append(v)
    return {k: sum(vs) / len(vs) for k, vs in by_day.items()}


def monthly_means(values: list[float], months: list[int]) -> list[float]:
    """12-element list — mean for each month (Jan..Dec)."""
    by_month: list[list[float]] = [[] for _ in range(12)]
    for v, m in zip(values, months):
        if 1 <= m <= 12:
            by_month[m - 1].append(v)
    return [sum(vs) / len(vs) if vs else 0.0 for vs in by_month]


def format_md(month: int, day: int) -> str:
    """ISO-style MM-DD label for the peak-day fields (year-agnostic)."""
    return f"{month:02d}-{day:02d}"


# ── Design-condition extraction (used by both new tools) ─────────────────
def design_conditions_F(epw: EPWFile) -> dict[str, float]:
    """ASHRAE-style 1% / 99% design percentiles in °F.

    Cooling 1% DB → P99 (DB exceeded 1% of hours).
    Heating 99% DB → P1 (DB exceeded 99% of hours, i.e. lowest 1%).
    Dewpoint 1% → P99 of dewpoint. (We compute dewpoint percentile rather
    than wet-bulb to avoid pulling in psychrometrics; the two track each
    other closely at design conditions.)
    """
    db = epw.hourly.dry_bulb_c
    dp = epw.hourly.dew_point_c
    return {
        "cooling_db_F": round(c_to_f(percentile(db, 99.0)), 1),
        "heating_db_F": round(c_to_f(percentile(db, 1.0)), 1),
        "dewpoint_F": round(c_to_f(percentile(dp, 99.0)), 1),
    }
