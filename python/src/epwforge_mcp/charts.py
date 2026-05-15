"""Pure-Python SVG chart generators for the MCP viz tools.

No numpy / matplotlib dependency — keeps the package lean. SVG is rendered
inline by Claude Desktop's artifact panel and is universally readable; CLI
clients can save the string to a `.svg` file.

Two starter charts (more in 0.1.6+):
  - diurnal_profile_svg(epw): monthly Max/Avg/Min hourly profiles
  - compare_scenarios_svg(scenarios, baseline): horizontal bars of design deltas
"""

from __future__ import annotations

from typing import Any, Iterable

from .epw_parser import EPWFile, c_to_f


# ── SVG primitives ──────────────────────────────────────────────────────
_PALETTE_HEAT = "#e64a4a"   # cooling deltas + max line
_PALETTE_COOL = "#3a7fbf"   # heating deltas + min line
_PALETTE_NEUTRAL = "#888888"
_PALETTE_AVG = "#c2854a"    # EPWForge brand orange
_PALETTE_DEW = "#5aab7a"    # Guzz green
_BG = "#ffffff"
_GRID = "#e8e8e8"
_TEXT = "#1f2937"
_TEXT_MUTED = "#6b7280"


def _esc(s: str) -> str:
    """XML-escape text for SVG inclusion."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _svg_open(width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system, BlinkMacSystemFont, '
        f'Segoe UI, sans-serif">'
        f'<rect width="{width}" height="{height}" fill="{_BG}"/>'
    )


def _line(x1: float, y1: float, x2: float, y2: float, color: str, width: float = 1.0,
          dash: str | None = None) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{color}" stroke-width="{width}"{d}/>'
    )


def _text(x: float, y: float, s: str, *, size: int = 11, color: str = _TEXT,
          anchor: str = "start", weight: str = "normal") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{_esc(s)}</text>'
    )


def _polyline(points: Iterable[tuple[float, float]], color: str, width: float = 1.5) -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}"/>'


def _rect(x: float, y: float, w: float, h: float, color: str, *, opacity: float = 1.0) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
        f'fill="{color}" fill-opacity="{opacity}"/>'
    )


# ── Chart 1: diurnal profile ─────────────────────────────────────────────
def diurnal_profile_svg(epw: EPWFile, *, width: int = 720, height: int = 360) -> str:
    """Monthly Max / Avg / Min hourly temperature in °F.

    For each of the 12 months, computes the hour-of-day Max / Avg / Min
    across all days in that month, then renders 12 small multiples (or one
    overlay if requested). Default: single overlay with January and July
    highlighted (the most informative for a year-shape read at a glance).

    Returns an SVG string.
    """
    db_f = [c_to_f(c) for c in epw.hourly.dry_bulb_c]
    months = epw.hourly.month
    hours = epw.hourly.hour  # 1..24

    # by_month_hour[month][hour] = list of values
    by_month_hour: dict[int, dict[int, list[float]]] = {m: {h: [] for h in range(1, 25)} for m in range(1, 13)}
    for v, m, h in zip(db_f, months, hours):
        if 1 <= m <= 12 and 1 <= h <= 24:
            by_month_hour[m][h].append(v)

    # Annual profile: avg across all months for the legend overview line.
    annual_avg = [sum(db_f[i] for i in range(len(db_f)) if hours[i] == h) /
                  max(1, sum(1 for hh in hours if hh == h))
                  for h in range(1, 25)]

    pad = {"l": 50, "r": 90, "t": 40, "b": 40}
    plot_w = width - pad["l"] - pad["r"]
    plot_h = height - pad["t"] - pad["b"]

    # Y range across all months' maxima/minima.
    all_vals: list[float] = []
    for m in range(1, 13):
        for h in range(1, 25):
            v = by_month_hour[m][h]
            if v:
                all_vals.extend(v)
    if not all_vals:
        return _svg_open(width, height) + _text(width / 2, height / 2, "No data", anchor="middle") + "</svg>"

    y_min = min(all_vals)
    y_max = max(all_vals)
    y_range = max(1.0, y_max - y_min)

    # Round Y bounds to nice 10°F ticks.
    y_lo = (int(y_min // 10)) * 10
    y_hi = (int(y_max // 10) + 1) * 10
    y_range = y_hi - y_lo

    def x_for(hour: int) -> float:
        return pad["l"] + (hour - 1) / 23.0 * plot_w

    def y_for(val: float) -> float:
        return pad["t"] + (1 - (val - y_lo) / y_range) * plot_h

    out = [_svg_open(width, height)]

    # Title
    out.append(_text(width / 2, 22, "Diurnal Temperature Profile (°F) — Monthly Max / Avg / Min",
                     size=14, color=_TEXT, anchor="middle", weight="600"))

    # Y gridlines + labels (10°F steps)
    for v in range(int(y_lo), int(y_hi) + 1, 10):
        y = y_for(v)
        out.append(_line(pad["l"], y, pad["l"] + plot_w, y, _GRID))
        out.append(_text(pad["l"] - 6, y + 3, f"{v}°F", size=10, color=_TEXT_MUTED, anchor="end"))

    # X axis labels (every 4 hours)
    for h in (1, 5, 9, 13, 17, 21, 24):
        x = x_for(h)
        label = "midnight" if h == 1 else "noon" if h == 13 else f"{h - 1:02d}h" if h <= 12 else f"{h - 1:02d}h"
        out.append(_text(x, height - pad["b"] + 14, label, size=9, color=_TEXT_MUTED, anchor="middle"))

    # 12 monthly faint avg lines + Jan + Jul highlighted with full Max/Avg/Min envelopes.
    for m in range(1, 13):
        avgs = []
        for h in range(1, 25):
            vals = by_month_hour[m][h]
            if vals:
                avgs.append((x_for(h), y_for(sum(vals) / len(vals))))
        if len(avgs) >= 2 and m not in (1, 7):
            out.append(_polyline(avgs, _PALETTE_NEUTRAL, width=0.7))

    # Highlight January (cool reference) + July (warm reference) with Max/Avg/Min envelope.
    for m, label, color in ((1, "Jan", _PALETTE_COOL), (7, "Jul", _PALETTE_HEAT)):
        maxs, avgs, mins = [], [], []
        for h in range(1, 25):
            vals = by_month_hour[m][h]
            if vals:
                maxs.append((x_for(h), y_for(max(vals))))
                avgs.append((x_for(h), y_for(sum(vals) / len(vals))))
                mins.append((x_for(h), y_for(min(vals))))
        if len(avgs) >= 2:
            out.append(_polyline(maxs, color, width=1.4))
            out.append(_polyline(avgs, color, width=2.0))
            out.append(_polyline(mins, color, width=1.4))

    # Annual mean as bold orange line.
    out.append(_polyline([(x_for(h), y_for(annual_avg[h - 1])) for h in range(1, 25)],
                         _PALETTE_AVG, width=2.5))

    # Legend (right side)
    legend_x = pad["l"] + plot_w + 10
    legend_y = pad["t"] + 6
    legend_items = [
        (_PALETTE_AVG, "Annual avg", "bold"),
        (_PALETTE_HEAT, "July (warm month)", ""),
        (_PALETTE_COOL, "January (cool month)", ""),
        (_PALETTE_NEUTRAL, "Other months (avg)", ""),
    ]
    for i, (color, label, weight) in enumerate(legend_items):
        ly = legend_y + i * 18
        out.append(_line(legend_x, ly, legend_x + 18, ly, color, width=2.0))
        out.append(_text(legend_x + 22, ly + 3, label, size=10, color=_TEXT,
                         weight="600" if weight else "normal"))

    # Sub-title: location + n hours
    loc = epw.location
    sub = f"{loc.city}{', ' + loc.state if loc.state else ''}{', ' + loc.country if loc.country else ''}"
    out.append(_text(width / 2, height - 6, sub.strip(", "), size=10, color=_TEXT_MUTED, anchor="middle"))

    out.append("</svg>")
    return "".join(out)


# ── Chart 2: compare_scenarios delta bars ────────────────────────────────
def compare_scenarios_svg(
    baseline: dict[str, Any],
    scenarios: list[dict[str, Any]],
    *,
    width: int = 720,
    height: int | None = None,
) -> str:
    """Horizontal bar chart of cooling / heating / dewpoint deltas vs baseline.

    Consumes the shape compare_scenarios returns. Each scenario gets a
    grouped 3-bar row: cooling delta (red), heating delta (blue), dewpoint
    delta (green). Zero line in center; positive deltas extend right,
    negative left.
    """
    n = len(scenarios)
    if n == 0:
        return _svg_open(width, 100) + _text(width / 2, 50, "No scenarios to compare", anchor="middle") + "</svg>"

    row_h = 56
    if height is None:
        height = 80 + n * row_h + 40

    pad = {"l": 220, "r": 80, "t": 50, "b": 40}
    plot_w = width - pad["l"] - pad["r"]
    plot_h = height - pad["t"] - pad["b"]

    # Find max abs delta to set X scale (symmetric around 0).
    max_abs = 0.1
    for s in scenarios:
        for k in ("cooling_db_delta_F", "heating_db_delta_F", "dewpoint_delta_F"):
            v = s.get(k, 0.0) or 0.0
            if abs(v) > max_abs:
                max_abs = abs(v)
    # Round up to a nice tick.
    tick = 1.0 if max_abs <= 5 else (2.0 if max_abs <= 10 else 5.0)
    max_abs = (int(max_abs / tick) + 1) * tick

    def x_for(delta: float) -> float:
        center = pad["l"] + plot_w / 2
        return center + (delta / max_abs) * (plot_w / 2)

    out = [_svg_open(width, height)]

    # Title
    title_l1 = "Design-Condition Deltas vs Baseline (°F)"
    out.append(_text(width / 2, 22, title_l1, size=14, anchor="middle", weight="600"))
    bl_str = (
        f"Baseline cooling {baseline.get('cooling_db_F', '?')}°F · "
        f"heating {baseline.get('heating_db_F', '?')}°F · "
        f"dewpoint {baseline.get('dewpoint_F', '?')}°F"
    )
    out.append(_text(width / 2, 40, bl_str, size=11, color=_TEXT_MUTED, anchor="middle"))

    # X-axis ticks + zero line
    center_x = pad["l"] + plot_w / 2
    for i in range(int(-max_abs / tick), int(max_abs / tick) + 1):
        v = i * tick
        x = x_for(v)
        out.append(_line(x, pad["t"], x, pad["t"] + plot_h, _GRID,
                         dash=None if v == 0 else "2,3"))
        if v == 0:
            out.append(_text(x, pad["t"] + plot_h + 16, "0", size=10, color=_TEXT, anchor="middle"))
        elif v == int(v):
            label = f"+{int(v)}" if v > 0 else str(int(v))
            out.append(_text(x, pad["t"] + plot_h + 16, label, size=10, color=_TEXT_MUTED, anchor="middle"))

    out.append(_line(center_x, pad["t"], center_x, pad["t"] + plot_h, _TEXT_MUTED, width=1.0))

    # Per-scenario rows
    for i, s in enumerate(scenarios):
        row_top = pad["t"] + i * row_h + 6
        cfg = s.get("config", {})
        # Build a short label from the config
        parts = []
        if cfg.get("ssp"):
            parts.append(cfg["ssp"].upper())
        if cfg.get("year"):
            parts.append(str(cfg["year"]))
        if cfg.get("percentile") and cfg.get("percentile") != 50:
            parts.append(f"P{cfg['percentile']}")
        if cfg.get("uhi") and cfg.get("uhi") != "none":
            parts.append(f"UHI:{cfg['uhi']}")
        if cfg.get("events"):
            parts.append(f"events:{cfg['events']}")
        if cfg.get("tmy_period"):
            parts.append(f"vintage:{cfg['tmy_period']}")
        label = " · ".join(parts) if parts else "baseline-shape sweep"
        out.append(_text(pad["l"] - 10, row_top + 14, label, size=11, color=_TEXT, anchor="end", weight="600"))

        bar_h = 12
        gap = 2
        bars = [
            ("cooling_db_delta_F", _PALETTE_HEAT, "cooling"),
            ("heating_db_delta_F", _PALETTE_COOL, "heating"),
            ("dewpoint_delta_F",   _PALETTE_DEW,  "dewpoint"),
        ]
        for j, (k, color, name) in enumerate(bars):
            v = s.get(k, 0.0) or 0.0
            x_end = x_for(v)
            y = row_top + 4 + j * (bar_h + gap)
            if v >= 0:
                out.append(_rect(center_x, y, x_end - center_x, bar_h, color))
                if v != 0:
                    out.append(_text(x_end + 4, y + bar_h - 2, f"+{v:.1f}", size=10, color=color))
            else:
                out.append(_rect(x_end, y, center_x - x_end, bar_h, color))
                if v != 0:
                    out.append(_text(x_end - 4, y + bar_h - 2, f"{v:.1f}", size=10, color=color, anchor="end"))

    # Mini legend bottom right
    legend_x = pad["l"] + plot_w + 6
    legend_y = pad["t"] + 6
    for i, (color, name) in enumerate([(_PALETTE_HEAT, "cooling"),
                                        (_PALETTE_COOL, "heating"),
                                        (_PALETTE_DEW,  "dewpoint")]):
        ly = legend_y + i * 16
        out.append(_rect(legend_x, ly, 14, 10, color))
        out.append(_text(legend_x + 18, ly + 9, name, size=10, color=_TEXT))

    out.append("</svg>")
    return "".join(out)
