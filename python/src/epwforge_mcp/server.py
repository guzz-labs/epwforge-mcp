"""FastMCP server exposing the EPWForge tools.

v0.2.0 — 4-tool consolidation:

  find_station          (no auth)  Search the GuzzStations catalog
  analyze_weather       (no auth)  Stats from an EPW URL or synthesized config
  chart_weather         (no auth)  SVG chart from an EPW URL or synthesized config
  generate_weather_file (auth)     Delivers EPW/DDY; charges credits

URL-mode for the 3 read tools runs entirely locally (download + parse +
chart). Config-mode (synthesized weather) routes through the hosted MCP
at https://epwforge.com/api/mcp so the morphing pipeline executes on
EPWForge infrastructure and never returns the EPW content to the caller —
anon-safe by construction. generate_weather_file requires an
EPWFORGE_API_KEY because it delivers actual EPW/DDY files and charges
credits.

Set EPWFORGE_API_KEY in env (or in your MCP client config) to enable
generate_weather_file. Read tools work without a key.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__
from .charts import compare_scenarios_svg, diurnal_profile_svg
from .client import EPWForgeClient, EPWForgeError, download_text, write_epw_base64
from .epw_parser import (
    EPWFile,
    c_to_f,
    daily_means_by_date,
    design_conditions_F,
    format_md,
    m_to_ft,
    monthly_means,
    parse_epw,
    percentile,
)


# ── TMY vintage choices (must match lib/tmy-period.ts on the platform side) ──
TMY_PERIOD_CHOICES = ("full", "2011-2025", "2009-2023", "2007-2021", "2004-2018")
DEFAULT_TMY_PERIOD = "2011-2025"
TmyPeriod = Literal["full", "2011-2025", "2009-2023", "2007-2021", "2004-2018"]

VALID_EVENTS = ("heatwave", "coldsnap", "hothumid", "coldwindy")


# ── MCP Apps (SEP-1865) interactive UI resources ─────────────────────────────
# Tools that reference a UI resource via their `meta.ui.resourceUri` render
# the linked HTML inline in supporting hosts (Claude Desktop, ChatGPT, VS
# Code, Goose). Clients without MCP Apps support fall back to the plain-text
# tool response — no regression.
COMPARE_SITES_URI = "ui://epwforge/compare-sites-v2.html"
DESIGN_EXPLORER_URI = "ui://epwforge/design-explorer-v1.html"

_VIEWS_DIR = Path(__file__).parent / "views"

def _read_view(filename: str) -> str:
    """Load a bundled MCP Apps view template from the package."""
    return (_VIEWS_DIR / filename).read_text(encoding="utf-8")


mcp = FastMCP("epwforge")
mcp._mcp_server.version = __version__


# ── MCP Apps UI resource: site-comparison cards ──────────────────────────────
# CSP allowlist: unpkg.com is required to load the ext-apps client library.
# No other external origins are loaded by compare-sites.html.
@mcp.resource(
    COMPARE_SITES_URI,
    name="Site comparison cards (interactive)",
    description=(
        "Interactive comparison-card view shown alongside analyze_weather "
        "multi-URL results in MCP Apps-capable hosts (Claude Desktop, "
        "ChatGPT, VS Code, Goose). Lets the user stress-test any compared "
        "site without retyping config."
    ),
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
)
def compare_sites_view() -> str:
    return _read_view("compare-sites.html")


@mcp.resource(
    DESIGN_EXPLORER_URI,
    name="Design conditions explorer (interactive)",
    description=(
        "Single-site live-tuning widget shown when explore_design_conditions "
        "is called. Sliders for SSP / year / percentile / UHI re-call the tool "
        "on change and re-render the diurnal chart + design-condition stats."
    ),
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
)
def design_explorer_view() -> str:
    return _read_view("design-explorer.html")


# ── Catalog resources (mirror of hosted MCP route.ts) ───────────────────────
# These let local Python users browse the same reference catalogs as users
# who go through epwforge.com/api/mcp. The hosted MCP remains the source of
# truth for content — these are kept short and link out for full details.
@mcp.resource(
    "epwforge://catalog/event-types",
    name="Extreme event catalog",
    description="Event types valid for the `events` config param; auto-compound pairs; intensity + duration guidance.",
    mime_type="application/json",
)
def catalog_event_types() -> str:
    return json.dumps({
        "events": [
            {"id": "heatwave",  "description": "Extended heat — sustained daily high above local 95th-percentile DB."},
            {"id": "coldsnap",  "description": "Extended cold — sustained daily low below local 5th-percentile DB."},
            {"id": "hothumid",  "description": "Humidity-amplified heat. Auto-compounds with heatwave."},
            {"id": "coldwindy", "description": "Wind-amplified cold. Auto-compounds with coldsnap."},
        ],
        "compound_pairs": [["heatwave", "hothumid"], ["coldsnap", "coldwindy"]],
        "intensity_scale": {
            "scale": "1-7 (default), unlock 8-10 with stress_test=true",
            "meanings": {
                "1": "Damped — 0.5x historical extreme",
                "5": "Historical baseline (default for unspecified events)",
                "7": "Severe — ~50-yr return period",
                "10": "Stress test — exceeds observed historical extremes",
            },
        },
        "auto_fill": "When ssp is set and intensity is left blank, the AR6 ensemble factor for that (region, ssp, year, percentile, event) is used. Cold events stay at 5.",
        "duration_guidance": {
            "param": "event_duration",
            "range": "3-30 days",
            "default": 14,
            "recommended": "14-21 for stress-test / resilience scenarios so the event spans a full work-week of operational impact",
            "avoid": "≤10 days for design or building-load analysis — too short to capture sustained impact",
        },
    }, indent=2)

# Lazy client — only constructed when a tool actually needs it. Read tools
# work without an API key (they hit public endpoints or fetch URLs directly).
_client: EPWForgeClient | None = None


def _get_client() -> EPWForgeClient:
    """Get the (lazily-constructed) HTTP client. Does NOT require an API key."""
    global _client
    if _client is None:
        _client = EPWForgeClient()
    return _client


def _base_url() -> str:
    return (os.environ.get("EPWFORGE_BASE_URL") or "https://epwforge.com").rstrip("/")


# Default to the latest TMYx EPW from the nearest real OneBuilding station
# when the caller passes a `config` (lat/lon). Synthesized custom-location
# TMYx is reserved as a fallback for genuinely remote sites; the caller must
# explicitly opt into it with allow_custom_location=true.
STATION_DISTANCE_THRESHOLD_KM = 50.0


# Belt-and-suspenders against ssp585 — the Literal enums already exclude it,
# but the `config` and `scenarios` params on analyze_weather / chart_weather /
# generate_weather_file are pass-through dicts where pydantic can't enforce
# the enum. Reject early so the agent gets a clean deprecation message instead
# of a confused 400 from the backend.
def _assert_ssp_allowed(*args: Any) -> None:
    """Walk dict-shaped tool args and reject any nested ssp == 'ssp585'.
    Skips None and non-dict inputs so callers don't need to pre-filter."""
    def _check(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            if obj.get("ssp") == "ssp585":
                raise ValueError(
                    f"ssp585 (SSP5-8.5) is deprecated per CMIP7 — the IPCC AR6 and the "
                    f"upcoming CMIP7 generation deem its trajectory implausible. Found at "
                    f"{path}.ssp. Retry with ssp='ssp370' (the recommended high-end scenario)."
                )
            for k, v in obj.items():
                _check(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _check(v, f"{path}[{i}]")
    for i, a in enumerate(args):
        _check(a, f"arg{i}")


async def _resolve_base_url_for_config(
    cfg: dict[str, Any] | None,
    *,
    allow_custom_location: bool,
) -> dict[str, Any] | None:
    """Look up the nearest OneBuilding station and return its EPW URL + label
    for use as the morph base. Returns None when caller has opted into custom
    synthesis (allow_custom_location=true) and no station is within threshold.
    Raises EPWForgeError when no station is nearby and the caller hasn't opted
    in (forcing the agent to confirm with the user)."""
    if not isinstance(cfg, dict):
        return None
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if lat is None or lon is None:
        return None
    # AMY basis is necessarily synthesized (per-year hourly from ERA5).
    if cfg.get("basis") == "amy":
        return None
    try:
        resp = await _call_hosted_mcp("find_station", {"lat": lat, "lon": lon, "max_results": 1})
    except Exception:
        resp = {}
    stations = resp.get("stations", []) if isinstance(resp, dict) else []
    nearest = stations[0] if stations else None
    dist_km = nearest.get("distance_km") if isinstance(nearest, dict) else None
    files = nearest.get("files") if isinstance(nearest, dict) else None
    target = None
    if isinstance(files, list):
        target = next((f for f in files if f.get("source") == "TMYx" and f.get("period") == "2011-2025"), None)
        if not target:
            target = next((f for f in files if f.get("source") == "TMYx"), None) or (files[0] if files else None)
    if not target or not target.get("epw_url"):
        if allow_custom_location:
            return None
        raise EPWForgeError(
            404,
            f"No OneBuilding TMYx EPW found near ({lat:.3f}, {lon:.3f}). "
            "Confirm with the user that a synthesized custom-location TMYx (ERA5 + "
            "Finkelstein-Schafer) is acceptable, then retry with allow_custom_location=true."
        )
    if isinstance(dist_km, (int, float)) and dist_km > STATION_DISTANCE_THRESHOLD_KM:
        if not allow_custom_location:
            raise EPWForgeError(
                404,
                f"Nearest OneBuilding station ({nearest.get('city', '?')}, {nearest.get('country', '?')}) "
                f"is {dist_km:.0f} km from ({lat:.3f}, {lon:.3f}) — exceeds the {STATION_DISTANCE_THRESHOLD_KM:.0f} km "
                "threshold for a real-station default. Ask the user if a synthesized "
                "custom-location TMYx (ERA5 + Finkelstein-Schafer) is acceptable for this work, "
                "then retry with allow_custom_location=true."
            )
        return None
    return {
        "base_url": target["epw_url"],
        "base_url_label": (
            f"Real {target.get('source','TMYx')} {target.get('period','')}".strip()
            + f" from {nearest.get('city','nearest station')}"
            + (f" ({dist_km:.0f} km)" if isinstance(dist_km, (int, float)) else "")
        ),
        "station": nearest,
        "file": target,
    }


# ============================================================================
# Tool 1: find_station — no auth needed
# ============================================================================
@mcp.tool()
async def find_station(
    query: Annotated[
        str | None,
        Field(description="Case-insensitive partial match on city / state. e.g. 'Boston', 'Manhattan'."),
    ] = None,
    lat: Annotated[
        float | None,
        Field(ge=-90, le=90, description="Latitude — when set with lon, results sort by proximity."),
    ] = None,
    lon: Annotated[
        float | None,
        Field(ge=-180, le=180, description="Longitude. Pair with lat."),
    ] = None,
    country: Annotated[
        str | None,
        Field(description="ISO 3-letter country code filter, e.g. 'USA', 'GBR', 'JPN'."),
    ] = None,
    max_results: Annotated[
        int,
        Field(ge=1, le=50, description="Max stations to return (default 10)."),
    ] = 10,
    include_amy_extremes: Annotated[
        bool,
        Field(description="When True with lat+lon, also returns the hottest/coldest/most-humid years on record (per ERA5). Routes through hosted MCP."),
    ] = False,
    include_climate_deltas: Annotated[
        bool,
        Field(description="When True with lat+lon+ssp+year, also returns the monthly CMIP6 delta-T. Routes through hosted MCP."),
    ] = False,
    ssp: Annotated[
        Literal["ssp126", "ssp245", "ssp370"] | None,
        Field(description="SSP scenario (only used with include_climate_deltas). ssp585 was deprecated per CMIP7 (deemed implausible) — use ssp370 as the high-end scenario."),
    ] = None,
    year: Annotated[
        Literal[2030, 2035, 2040, 2045, 2050, 2060, 2070, 2080, 2090, 2100] | None,
        Field(description="Future horizon (only used with include_climate_deltas)"),
    ] = None,
    percentile: Annotated[
        Literal[5, 10, 25, 50, 75, 90, 95],
        Field(description="Warming percentile (only used with include_climate_deltas, default 50)"),
    ] = 50,
) -> dict[str, Any]:
    """Search the GuzzStations catalog (17,000+ weather stations worldwide).

    Optional enrichments (route through hosted MCP for the extra queries):
      - include_amy_extremes: hottest/coldest/most-humid years on record
      - include_climate_deltas: monthly CMIP6 delta-T for the picked scenario

    No authentication required for any mode.

    Examples:
      find_station(query="Denver")
      find_station(lat=40.7, lon=-74.0, max_results=5)
      find_station(country="JPN", query="Tokyo")
      find_station(lat=40.7, lon=-74.0, include_amy_extremes=True)
      find_station(lat=40.7, lon=-74.0, include_climate_deltas=True, ssp="ssp245", year=2050)
    """
    # If any enrichment is requested, the hosted MCP handles the fan-out to
    # /api/amy-extremes and /api/climate-deltas (single round-trip vs 3).
    if include_amy_extremes or include_climate_deltas:
        return await _call_hosted_mcp("find_station", {
            "query": query, "lat": lat, "lon": lon, "country": country, "max_results": max_results,
            "include_amy_extremes": include_amy_extremes,
            "include_climate_deltas": include_climate_deltas,
            "ssp": ssp, "year": year, "percentile": percentile,
        })

    params: dict[str, Any] = {"limit": max_results}
    if query: params["q"] = query
    if lat is not None: params["lat"] = lat
    if lon is not None: params["lon"] = lon
    if country: params["country"] = country

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "epwforge-mcp"},
        follow_redirects=True,
    ) as c:
        resp = await c.get(f"{_base_url()}/api/stations", params=params)
        if resp.status_code >= 400:
            raise EPWForgeError(resp.status_code, f"find_station failed (HTTP {resp.status_code}): {resp.text[:200]}")
        data = resp.json()

    stations = data.get("stations", [])
    nearest_km = None
    if stations:
        try:
            nearest_km = min(s["distance_km"] for s in stations if s.get("distance_km") is not None)
        except (ValueError, KeyError):
            nearest_km = None

    if nearest_km is None:
        nudge = "No matches. Try a broader query (city only) or pass lat/lon for proximity sort."
    elif nearest_km <= 25:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km away — almost certainly representative. "
            "Use any station's epw_url with analyze_weather or chart_weather."
        )
    elif nearest_km <= 100:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km — may differ for microclimates "
            "(urban core, mountain, coastal). For exact-coordinate weather, use "
            "generate_weather_file (requires API key + credits) or analyze_weather "
            "with a config (no auth needed, returns stats only)."
        )
    else:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km — likely a different climate. "
            "Consider analyze_weather with a config for a synthesized TMYx at the exact lat/lon."
        )

    return {
        "count": len(stations),
        "stations": stations,
        "agent_guidance": nudge,
        "nearest_km": nearest_km,
        "meta": _meta("find_station"),
    }


# ============================================================================
# Tool 2: analyze_weather — no auth needed (URL, urls, or config)
# ============================================================================
@mcp.tool(meta={
    # MCP Apps (SEP-1865): when multi-URL results are returned to an Apps-
    # capable host, render the compare-sites card view instead of raw JSON.
    # The view itself decides whether to render (presence of `summaries[]`)
    # — single-URL and config-mode calls still show as text.
    "ui": {"resourceUri": COMPARE_SITES_URI},
    "ui/resourceUri": COMPARE_SITES_URI,  # legacy spec key
})
async def analyze_weather(
    url: Annotated[
        str | None,
        Field(description="EPW URL to analyze (single file). Pass this for a single-file stats summary."),
    ] = None,
    urls: Annotated[
        list[str] | None,
        Field(
            min_length=2,
            max_length=10,
            description=(
                "Multiple EPW URLs to compare (2-10) in ONE call. First is the baseline; "
                "others are reported as deltas from it. **Use this for ANY multi-site "
                "comparison** (data center siting, climate-zone spread, portfolio "
                "resilience). The card UI renders all sites side-by-side with future "
                "deltas inline. Do NOT loop analyze_weather with single configs to fake "
                "a comparison — pass all URLs here."
            ),
        ),
    ] = None,
    config: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Synthesize a SINGLE morphed EPW server-side and analyze it. Required: "
                "`lat` (-90..90), `lon` (-180..180). Common params:\n"
                "  • ssp: 'ssp126'|'ssp245'|'ssp370' — emission scenario. "
                "ssp370 is the credible upper bound (ssp585 was deprecated per "
                "CMIP7 — deemed implausible — and is rejected). Default no SSP = "
                "present-day TMY.\n"
                "  • year: 2030|2035|2040|2045|2050|2060|2070|2080|2090|2100 — future horizon (5-yr through 2050, 10-yr after). Pair with ssp.\n"
                "  • percentile: 5|10|25|50|75|90|95 — warming percentile across CMIP6 "
                "models. **Use 75 for design-realistic warming; 50 is the median and "
                "underestimates the tail for siting/sizing work.** Default 50.\n"
                "  • uhi: 'none'|'suburban'|'urban'|'dense_urban' — UHI preset.\n"
                "  • events: comma-separated string of 'heatwave','coldsnap','hothumid',"
                "'coldwindy'. Auto-compounds heat+humid and cold+wind pairs.\n"
                "  • intensity: per-event string like 'heatwave:8,coldsnap:7'. 5 = "
                "typical extreme, 7 = severe ~50-yr return, 8-10 = stress-test (requires "
                "stress_test=true). Leave blank with ssp set to auto-fill from AR6.\n"
                "  • event_duration: integer 3-30, **default 14**. For stress-test or "
                "resilience scenarios use 14-21 days — shorter durations don't capture "
                "sustained operational impact. 7 is too short for any design work.\n"
                "  • smoke: bool. smoke_intensity: 1-10 (peak AOD 0.1-6.0). "
                "smoke_duration: 3-30 days, default 14 (NOT 7).\n"
                "  • stress_test: bool — unlocks intensity 8-10.\n"
                "Use for: (a) drilling into ONE site at a specific future scenario, or "
                "(b) stress-testing event compounds. **Never loop for multi-site work** "
                "— use `urls=[...]` instead. Routes through the hosted MCP — runs the "
                "full morph/UHI/event pipeline and returns ONLY stats. Anon-safe."
            )
        ),
    ] = None,
    compact: Annotated[
        bool,
        Field(description="Token-saver. Returns a ~10-field headline-only response (~100 tokens) instead of the full ~800-token payload — drops monthly arrays, peak days, n_hours, weather_basis. Good for sanity checks, dashboards, batched chained calls. Set false (default) when you need the full payload. Routes through hosted MCP."),
    ] = False,
    include_full_ashrae: Annotated[
        bool,
        Field(description="Adds ASHRAE 0.4%/1%/2% cooling DB + 99.6%/99% heating DB design conditions. Ignored when compact=true. Routes through hosted MCP."),
    ] = False,
    include_improbability: Annotated[
        bool,
        Field(description="Adds EPWForge's stress-test improbability score (config mode only). Routes through hosted MCP."),
    ] = False,
    include_idf: Annotated[
        bool,
        Field(description="Adds ready-to-paste EnergyPlus SizingPeriod:DesignDay IDF objects to the response. Routes through hosted MCP."),
    ] = False,
    units: Annotated[
        Literal["imperial", "metric"],
        Field(description="Output units (default imperial). When 'metric', temperatures are °C, HDD/CDD base 18 °C, elevation in m. Routes through hosted MCP."),
    ] = "imperial",
    include_future_projection: Annotated[
        bool,
        Field(
            description=(
                "When true (default for multi-URL comparisons) runs the SSP 3-7.0 P75 2050 "
                "morph pipeline per site (via hosted MCP) and embeds future-projected "
                "design conditions and CDD-65 deltas under each summary's `future_projection`. "
                "Lets a UI show '92.8 → 97.7 °F' baseline→future on each card. Per CMIP7 "
                "guidance, SSP 3-7.0 is the credible upper bound (SSP 5-8.5 was deemed "
                "implausible). P75 is the design-realistic warming percentile vs P50 median. "
                "Adds N hosted MCP calls; parallelized via asyncio.gather. Free. Set to false "
                "to skip for a faster baseline-only response."
            )
        ),
    ] = True,
    allow_custom_location: Annotated[
        bool,
        Field(
            description=(
                "Required to fall back to synthesized TMYx when no real OneBuilding "
                "station is within 50 km of the requested lat/lon. By default, config-mode "
                "uses the nearest real station's TMYx EPW as the morph base — synthesizing "
                "from ERA5 is reserved for genuinely remote sites. If the nearest station "
                "is >50 km away and this flag is false, the call returns an error asking "
                "you to confirm a custom synthesized location is acceptable, then retry "
                "with allow_custom_location=true."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Compute design conditions, HDD/CDD, monthly stats, and peak days for one
    or more EPW files. No EPW content returned — stats only.

    Three modes:
      1. Single URL: analyze_weather(url="https://...")
      2. Multi-URL comparison: analyze_weather(urls=["...", "...", "..."])
      3. Synthesized config: analyze_weather(config={"lat": 40.7, "lon": -74,
          "ssp": "ssp370", "year": 2050, "uhi": "urban"})

    ⚠ CRITICAL ROUTING RULE — read before calling:

    If the user is comparing N sites (data-center siting, climate-zone
    spread, portfolio resilience, etc.) you MUST:
      1. Call `find_station` once per city to get its EPW URL
      2. Call `analyze_weather` EXACTLY ONCE with `urls=[all N urls]`

    Do NOT call analyze_weather N times in a loop with single configs.
    That breaks the comparison card UI (each call renders a separate
    blank widget), produces no future-projection deltas, and is slow.

    `include_future_projection=true` (the default for url-mode) embeds
    SSP 3-7.0 P75 2050 design conditions per site in one shot, which is
    what the inline card UI needs.

    Use `config` mode ONLY for: (a) a single site morphed to a specific
    future scenario, or (b) stress-testing event compounds for one site.
    Never use config mode in a loop to fake a comparison.

    Modes 1 + 2 download the URLs and parse locally (purely client-side).
    Mode 3 routes through the hosted EPWForge MCP so the morph/UHI/event/smoke
    pipeline runs on EPWForge infrastructure — the synthesized EPW never
    leaves the server. Use mode 3 to preview a future-climate scenario or
    a UHI / extreme-event sensitivity without spending credits.

    No authentication required for any mode.
    """
    inputs_set = [x for x in (url, urls, config) if x is not None]
    if len(inputs_set) != 1:
        raise ValueError("analyze_weather requires exactly one of: url, urls, config")
    _assert_ssp_allowed(config)

    # Post-processing hook: when the caller wants future-projected design
    # conditions, apply CMIP6 monthly delta-T values **to the real baseline**
    # (no synthesis). Belcher-style mean shift only at the design-condition
    # level. For each site, fetch climate_deltas via find_station and apply
    # to that site's real baseline. Total cost: N parallel hosted calls,
    # ~3-5s end-to-end.
    async def _attach_future_projection(summaries_in: list[dict[str, Any]]) -> None:
        if not include_future_projection or not summaries_in:
            return
        FUTURE_SSP = "ssp370"
        FUTURE_YEAR = 2050
        FUTURE_PCT  = 75
        async def _future_for(s: dict[str, Any]) -> dict[str, Any]:
            loc = s.get("location") or {}
            lat, lon = loc.get("lat"), loc.get("lon")
            if lat is None or lon is None:
                return {"error": "missing lat/lon on baseline EPW"}
            try:
                # Fetch monthly CMIP6 delta-T (°C) for this site/scenario.
                # find_station with include_climate_deltas returns deltas at
                # top level; no EPW synthesis happens.
                resp = await _call_hosted_mcp("find_station", {
                    "lat": lat, "lon": lon,
                    "include_climate_deltas": True,
                    "ssp": FUTURE_SSP, "year": FUTURE_YEAR, "percentile": FUTURE_PCT,
                    "max_results": 1,
                })
                cd = resp.get("climate_deltas") if isinstance(resp, dict) else None
                if not cd or not cd.get("delta_temp") or len(cd["delta_temp"]) != 12:
                    return {"error": "climate deltas unavailable", "ssp": FUTURE_SSP, "year": FUTURE_YEAR, "percentile": FUTURE_PCT}
                # 12 monthly delta-T (°C → °F)
                d_f = [v * 9.0 / 5.0 for v in cd["delta_temp"]]
                base_cool = s.get("cooling_design_db_F")
                base_heat = s.get("heating_design_db_F")
                base_cdd  = s.get("cdd_65_annual")
                base_hdd  = s.get("hdd_65_annual")
                base_monthly = s.get("monthly_mean_temp_F") or []
                base_annual_mean = s.get("annual_mean_temp_F")
                # Cooling design (99% annual DB) peaks in summer: apply max of Jun/Jul/Aug delta.
                summer_delta = max(d_f[5], d_f[6], d_f[7]) if len(d_f) == 12 else max(d_f)
                # Heating design (1% annual DB) hits in winter: apply min of Dec/Jan/Feb delta.
                winter_delta = min(d_f[11], d_f[0], d_f[1]) if len(d_f) == 12 else min(d_f)
                # Recompute CDD/HDD from monthly means + month-wise deltas (approx, monthly mean basis).
                future_cdd = future_hdd = None
                if len(base_monthly) == 12:
                    DAYS = [31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                    future_monthly = [m + d for m, d in zip(base_monthly, d_f)]
                    future_cdd = round(sum(max(0.0, m - 65) * d for m, d in zip(future_monthly, DAYS)))
                    future_hdd = round(sum(max(0.0, 65 - m) * d for m, d in zip(future_monthly, DAYS)))
                cdd_pct = round((future_cdd - base_cdd) / base_cdd * 100) if (base_cdd and future_cdd and base_cdd > 0) else None
                annual_delta_F = round(sum(d_f) / 12, 1) if len(d_f) == 12 else None
                future_annual_mean = round(base_annual_mean + annual_delta_F, 1) if (base_annual_mean is not None and annual_delta_F is not None) else None
                return {
                    "ssp": FUTURE_SSP,
                    "year": FUTURE_YEAR,
                    "percentile": FUTURE_PCT,
                    "method": "CMIP6 monthly delta-T applied to real baseline (no EPW synthesis)",
                    "cooling_design_db_F": round(base_cool + summer_delta, 1) if base_cool is not None else None,
                    "heating_design_db_F": round(base_heat + winter_delta, 1) if base_heat is not None else None,
                    "annual_mean_temp_F":  future_annual_mean,
                    "cdd_65_annual":       future_cdd,
                    "hdd_65_annual":       future_hdd,
                    "cdd_pct_delta":       cdd_pct,
                }
            except Exception as e:
                return {"error": str(e), "ssp": FUTURE_SSP, "year": FUTURE_YEAR, "percentile": FUTURE_PCT}
        futures = await asyncio.gather(*(_future_for(s) for s in summaries_in))
        for s, fp in zip(summaries_in, futures):
            s["future_projection"] = fp

    # Helper: reverse-geocode lat/lon to a nearby GuzzStation name. Used for
    # config-mode responses that come back with location="Custom, Unknown".
    async def _nearest_station(lat: float | None, lon: float | None) -> dict[str, Any] | None:
        if lat is None or lon is None:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "epwforge-mcp"}, follow_redirects=True) as c:
                r = await c.get(f"{_base_url()}/api/stations", params={"lat": lat, "lon": lon, "limit": 1})
                if r.status_code == 200:
                    stations = r.json().get("stations", [])
                    if stations:
                        return stations[0]
        except Exception:
            pass
        return None

    def _apply_station_to_location(result_obj: dict[str, Any], nearest: dict[str, Any]) -> None:
        loc = result_obj.get("location") or {}
        is_generic = (
            not loc.get("city")
            or loc.get("city") in ("Custom", "Unknown", "")
            or not loc.get("state")
            or loc.get("country") in ("Unknown", "", None)
        )
        if not is_generic:
            return
        loc["city"]    = nearest.get("city")    or nearest.get("name") or loc.get("city")    or "Unknown"
        loc["state"]   = nearest.get("state")   or loc.get("state", "")
        loc["country"] = nearest.get("country") or loc.get("country") or "Unknown"
        if (not loc.get("elevation_ft") or loc.get("elevation_ft") == 0) and nearest.get("elevation_m") is not None:
            loc["elevation_ft"] = round(nearest["elevation_m"] * 3.28084)
        result_obj["location"] = loc
        dist = nearest.get("distance_km")
        result_obj.setdefault("location_meta", {})["enriched_from"] = (
            f"nearest station ({dist:.0f} km)" if isinstance(dist, (int, float)) else "nearest station"
        )

    async def _enrich_config_location(result_obj: Any, cfg: dict[str, Any] | None) -> None:
        """If a config-mode result has a generic Custom/Unknown location, fill it via nearest station."""
        if not isinstance(result_obj, dict) or not isinstance(cfg, dict):
            return
        nearest = await _nearest_station(cfg.get("lat"), cfg.get("lon"))
        if nearest:
            _apply_station_to_location(result_obj, nearest)

    # _resolve_base_url_for_config is module-level so chart_weather can reuse it.
    # See module-level definition near top of file.

    # Anything in the config that triggers the morph/event/smoke/UHI pipeline.
    # When set, the result is a *modified* scenario — useless without baseline
    # for comparison.
    def _config_is_morphed(cfg: dict[str, Any] | None) -> bool:
        if not isinstance(cfg, dict):
            return False
        if cfg.get("ssp") or cfg.get("year"):
            return True
        if cfg.get("uhi") and cfg.get("uhi") != "none":
            return True
        if cfg.get("events") or cfg.get("intensity"):
            return True
        if cfg.get("smoke"):
            return True
        if cfg.get("stress_test"):
            return True
        return False

    async def _attach_baseline_reference(result_obj: Any, cfg: dict[str, Any] | None) -> None:
        """For config-mode stress/morph results, fetch the **real OneBuilding
        TMYx EPW** at the nearest station and use it as the baseline reference
        so the UI can render '88 → 101 °F' deltas. Skipped when the config is
        already baseline (no morphing params). Never synthesizes — uses the
        actual file a user would download via find_station."""
        if not isinstance(result_obj, dict) or not isinstance(cfg, dict):
            return
        if not _config_is_morphed(cfg):
            return
        lat, lon = cfg.get("lat"), cfg.get("lon")
        if lat is None or lon is None:
            return
        try:
            # Step 1: nearest OneBuilding station + its TMYx EPW URL
            resp = await _call_hosted_mcp("find_station", {
                "lat": lat, "lon": lon, "max_results": 1,
            })
            stations = resp.get("stations", []) if isinstance(resp, dict) else []
            if not stations:
                return
            files = stations[0].get("files") or []
            # Prefer 2011-2025 TMYx; fall back to first available.
            preferred = next((f for f in files if f.get("source") == "TMYx" and f.get("period") == "2011-2025"), None)
            target = preferred or (files[0] if files else None)
            if not target or not target.get("epw_url"):
                return
            # Step 2: fetch and parse the real EPW (no synthesis)
            text = await download_text(target["epw_url"])
            baseline_epw = parse_epw(text)
            baseline = _summarize_epw(baseline_epw, source_url=target["epw_url"])
            dist_km = stations[0].get("distance_km")
            result_obj["baseline_reference"] = {
                "cooling_design_db_F": baseline.get("cooling_design_db_F"),
                "heating_design_db_F": baseline.get("heating_design_db_F"),
                "annual_mean_temp_F":  baseline.get("annual_mean_temp_F"),
                "cdd_65_annual":       baseline.get("cdd_65_annual"),
                "hdd_65_annual":       baseline.get("hdd_65_annual"),
                "source_url":          target["epw_url"],
                "source_label":        f"Real TMYx from {stations[0].get('city', 'nearest station')}" + (f" ({dist_km:.0f} km away)" if isinstance(dist_km, (int, float)) else ""),
                "vintage":             f"{target.get('source', 'TMYx')} {target.get('period', '')}".strip(),
                "method":              "Real OneBuilding TMYx EPW (no synthesis)",
            }
        except Exception:
            # Silent fallback: don't break the morphed response if baseline lookup fails.
            pass

    # If any enrichment (or metric units, or compact mode) is requested,
    # route through hosted MCP — it has the IDF emitter, full-ASHRAE
    # computation, improbability scorer, compact projection, and the
    # unit-converted summarizer all in lib.
    if include_full_ashrae or include_improbability or include_idf or units == "metric" or compact:
        payload: dict[str, Any] = {
            "include_full_ashrae": include_full_ashrae,
            "include_improbability": include_improbability,
            "include_idf": include_idf,
            "units": units,
            "compact": compact,
        }
        if url: payload["url"] = url
        if urls: payload["urls"] = urls
        if config:
            # Inject real-station base_url when one is within threshold; raises
            # EPWForgeError telling the agent to confirm custom synth if not.
            base = await _resolve_base_url_for_config(config, allow_custom_location=allow_custom_location)
            cfg_with_base = {**config}
            if base:
                cfg_with_base["base_url"] = base["base_url"]
                cfg_with_base["base_url_label"] = base["base_url_label"]
            payload["config"] = cfg_with_base
        result = await _call_hosted_mcp("analyze_weather", payload)
        # Hosted MCP doesn't yet know about include_future_projection — we
        # post-process its summaries locally to attach it. Works for either
        # multi-URL (summaries[] array) or single-URL (result IS the summary).
        if include_future_projection and isinstance(result, dict):
            if urls:
                inner_summaries = result.get("summaries")
                if isinstance(inner_summaries, list):
                    await _attach_future_projection(inner_summaries)
            elif url:
                await _attach_future_projection([result])
        # Config mode + any enrichment came back with "Custom/Unknown" location
        # because hosted MCP doesn't reverse-geocode lat/lon. Fix it. Also attach
        # a baseline reference so the UI can show before→after deltas for any
        # morphed/stressed scenario.
        if config:
            await asyncio.gather(
                _enrich_config_location(result, config),
                _attach_baseline_reference(result, config),
            )
        return result

    # Single URL — local fetch + parse, with optional future projection.
    if url:
        text = await download_text(url)
        epw = parse_epw(text)
        summary = _summarize_epw(epw, source_url=url)
        if include_future_projection:
            await _attach_future_projection([summary])
        return summary

    # Multi-URL comparison — parallel fetch + parse, deltas vs first.
    if urls:
        async def _one(u: str) -> dict[str, Any]:
            text = await download_text(u)
            return _summarize_epw(parse_epw(text), source_url=u)
        summaries = list(await asyncio.gather(*(_one(u) for u in urls)))
        await _attach_future_projection(summaries)

        baseline = summaries[0]
        comparisons = [
            {
                "source_url": s["source_url"],
                "cooling_db_delta_F": round(s["cooling_design_db_F"] - baseline["cooling_design_db_F"], 1),
                "heating_db_delta_F": round(s["heating_design_db_F"] - baseline["heating_design_db_F"], 1),
                "annual_mean_temp_delta_F": round(s["annual_mean_temp_F"] - baseline["annual_mean_temp_F"], 1),
            }
            for s in summaries[1:]
        ]
        return {
            "baseline_url": baseline["source_url"],
            "count": len(summaries),
            "summaries": summaries,
            "comparisons": comparisons,
            "meta": _meta("analyze_weather", mode="compare", n_urls=len(urls), future_projection=include_future_projection),
        }

    # config mode — route through hosted MCP for the pipeline run, then
    # enrich the location and attach baseline reference if morphed.
    # First inject real-station base_url so morph operates on the actual
    # OneBuilding TMYx (or raise asking the agent to confirm custom synth).
    base = await _resolve_base_url_for_config(config, allow_custom_location=allow_custom_location)
    cfg_with_base = {**config}
    if base:
        cfg_with_base["base_url"] = base["base_url"]
        cfg_with_base["base_url_label"] = base["base_url_label"]
    morph = await _call_hosted_mcp("analyze_weather", {"config": cfg_with_base})
    await asyncio.gather(
        _enrich_config_location(morph, config),
        _attach_baseline_reference(morph, config),
    )
    return morph


# ============================================================================
# Tool 3: chart_weather — no auth needed (URL, urls, or config)
# ============================================================================
@mcp.tool()
async def chart_weather(
    url: Annotated[
        str | None,
        Field(description="EPW URL (for chart_type='diurnal')."),
    ] = None,
    urls: Annotated[
        list[str] | None,
        Field(
            min_length=2,
            max_length=10,
            description="EPW URLs for chart_type='comparison' (first = baseline).",
        ),
    ] = None,
    config: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Synthesize an EPW server-side and chart it. Same params as "
                "generate_weather_file. Routes through hosted MCP — pipeline "
                "runs on EPWForge infra, only SVG returned. Anon-safe."
            )
        ),
    ] = None,
    chart_type: Annotated[
        Literal["diurnal", "temp_carpet", "wind_rose", "monthly_boxplot", "comparison"],
        Field(description=(
            "Chart type. diurnal = monthly Max/Avg/Min hourly profile. "
            "temp_carpet = heatmap of hour x day-of-year. "
            "wind_rose = polar bars of direction x speed. "
            "monthly_boxplot = Q1/median/Q3 + whiskers per month. "
            "comparison = design-condition delta bars (needs urls)."
        )),
    ] = "diurnal",
    resolution: Annotated[
        Literal["preview", "full"],
        Field(description=(
            "temp_carpet only. 'preview' (default) ~150 KB with 32-color quantization. "
            "'full' ~600 KB with per-cell rgb() — exact fidelity. Either way, "
            "outputs over 50 KB auto-upload to Blob (hosted MCP) and return svg_url "
            "instead of inline svg — keeps your context lean."
        )),
    ] = "preview",
    save_to: Annotated[
        str | None,
        Field(description="When set, writes SVG to this path and returns the path (saves agent context)."),
    ] = None,
    allow_custom_location: Annotated[
        bool,
        Field(description=(
            "Required to fall back to synthesized TMYx when no real OneBuilding station "
            "is within 50 km of the requested lat/lon (config mode only). Default false: "
            "config-mode chart uses the nearest real station's TMYx EPW as the morph base. "
            "If no station nearby and this flag is false, the call returns an error asking "
            "you to confirm a custom synthesized location is acceptable."
        )),
    ] = False,
) -> dict[str, Any]:
    """Render an SVG chart from EPW data.

    chart_type='diurnal' — monthly Max / Avg / Min hourly temperature profile
    in °F (January and July highlighted, annual mean overlaid). Pass `url`
    or `config`.

    chart_type='comparison' — horizontal-bar chart of cooling/heating
    deltas across multiple EPWs. Pass `urls` (first = baseline).

    No authentication required for any mode.
    """
    inputs_set = [x for x in (url, urls, config) if x is not None]
    if len(inputs_set) != 1:
        raise ValueError("chart_weather requires exactly one of: url, urls, config")
    _assert_ssp_allowed(config)

    # New chart types (added in 0.3.0) live only in the hosted MCP; route there.
    if chart_type in ("temp_carpet", "wind_rose", "monthly_boxplot"):
        payload: dict[str, Any] = {"chart_type": chart_type, "resolution": resolution}
        if url: payload["url"] = url
        if urls: payload["urls"] = urls
        if config:
            # Real-station default (50 km threshold) — see analyze_weather for rationale.
            base = await _resolve_base_url_for_config(config, allow_custom_location=allow_custom_location)
            cfg_with_base = {**config}
            if base:
                cfg_with_base["base_url"] = base["base_url"]
                cfg_with_base["base_url_label"] = base["base_url_label"]
            payload["config"] = cfg_with_base
        result = await _call_hosted_mcp("chart_weather", payload)
        # Hosted MCP may have auto-uploaded large SVGs to Blob and replaced
        # the `svg` field with `svg_url`. Honor save_to for inline SVGs;
        # fetch + save when we only got a URL.
        if save_to:
            svg_str = result.get("svg")
            if svg_str:
                return _save_svg(svg_str, chart_type, result.get("source", "synthesized"), save_to)
            svg_url = result.get("svg_url")
            if svg_url:
                async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "epwforge-mcp"}) as c:
                    r = await c.get(svg_url)
                    if r.status_code < 400:
                        return _save_svg(r.text, chart_type, result.get("source", "synthesized"), save_to,
                                         extra={"svg_url": svg_url})
        return result

    # Diurnal — needs a single EPW.
    if chart_type == "diurnal":
        if urls:
            # Take first URL of the array — diurnal is single-EPW.
            url = urls[0]
        if url:
            text = await download_text(url)
            epw = parse_epw(text)
            svg = diurnal_profile_svg(epw)
            return _chart_result(svg, "diurnal", url, save_to)
        # config — route through hosted MCP (it will fetch text internally),
        # injecting real-station base_url so morph operates on the real EPW.
        base = await _resolve_base_url_for_config(config, allow_custom_location=allow_custom_location)
        cfg_with_base = {**config}
        if base:
            cfg_with_base["base_url"] = base["base_url"]
            cfg_with_base["base_url_label"] = base["base_url_label"]
        result = await _call_hosted_mcp("chart_weather", {"config": cfg_with_base, "chart_type": "diurnal"})
        svg = result.get("svg")
        if save_to and svg:
            return _save_svg(svg, "diurnal", "synthesized", save_to, extra={"weather_basis": result.get("weather_basis")})
        return result

    # Comparison — needs multiple EPWs.
    if not urls or len(urls) < 2:
        raise ValueError("chart_type='comparison' requires urls (2-10 entries)")
    async def _one(u: str) -> dict[str, Any]:
        text = await download_text(u)
        return {"url": u, "epw": parse_epw(text)}
    parsed = list(await asyncio.gather(*(_one(u) for u in urls)))
    baseline_dc = design_conditions_F(parsed[0]["epw"])
    scenarios = []
    for p in parsed[1:]:
        dc = design_conditions_F(p["epw"])
        label = p["url"].rsplit("/", 1)[-1][:40]
        scenarios.append({
            "config": {"label": label},
            "cooling_db_F": dc["cooling_db_F"],
            "cooling_db_delta_F": round(dc["cooling_db_F"] - baseline_dc["cooling_db_F"], 1),
            "heating_db_F": dc["heating_db_F"],
            "heating_db_delta_F": round(dc["heating_db_F"] - baseline_dc["heating_db_F"], 1),
            "dewpoint_F": dc["dewpoint_F"],
            "dewpoint_delta_F": round(dc["dewpoint_F"] - baseline_dc["dewpoint_F"], 1),
        })
    svg = compare_scenarios_svg(baseline_dc, scenarios)
    return _chart_result(svg, "comparison", f"{len(urls)} URLs", save_to, extra={"n_scenarios": len(parsed)})


# ============================================================================
# Tool 4 (new in 0.5.1): explore_design_conditions — interactive single-site
#                        widget for tuning SSP / year / percentile / UHI live
# ============================================================================
@mcp.tool(meta={
    "ui": {"resourceUri": DESIGN_EXPLORER_URI},
    "ui/resourceUri": DESIGN_EXPLORER_URI,  # legacy spec key
})
async def explore_design_conditions(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude, decimal degrees")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude, decimal degrees")],
    ssp: Annotated[
        Literal["ssp126", "ssp245", "ssp370"] | None,
        Field(description="CMIP6 emission scenario. Pass None / omit for present-day TMY. ssp585 was deprecated per CMIP7 (deemed implausible) — use ssp370 as the high-end scenario."),
    ] = None,
    year: Annotated[
        Literal[2030, 2035, 2040, 2045, 2050, 2060, 2070, 2080, 2090, 2100] | None,
        Field(description="Future horizon. Pair with ssp."),
    ] = None,
    percentile: Annotated[
        int,
        Field(ge=5, le=95, description="Warming percentile across CMIP6 models. Use 75 for design-realistic; 50 is median."),
    ] = 75,
    uhi: Annotated[
        Literal["none", "suburban", "urban", "dense_urban"],
        Field(description="Urban Heat Island preset."),
    ] = "none",
    allow_custom_location: Annotated[
        bool,
        Field(description=(
            "Required when no OneBuilding station is within 50 km. By default this tool "
            "uses the nearest real station's TMYx EPW as the morph base."
        )),
    ] = False,
) -> dict[str, Any]:
    """Interactive single-site design-conditions explorer.

    Returns the full ASHRAE design conditions + a diurnal-profile SVG chart
    for the requested scenario. In MCP Apps-capable hosts (Claude Desktop,
    ChatGPT, VS Code, Goose), the response renders as a widget with sliders
    for SSP / year / percentile / UHI; dragging a slider re-calls this tool
    with the new value and re-renders the chart + stats live.

    Use when the user wants to interactively tune a single site — much
    better UX than asking them to retype config each time. For multi-site
    comparison, use analyze_weather(urls=[...]) which renders cards.

    Defaults: present-day TMY (no morph) — pass ssp+year for future scenarios.
    P75 default percentile is design-realistic; P50 underestimates the tail.

    No auth required.
    """
    # Build config — drop None values so analyze_weather sees a clean dict.
    cfg: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "percentile": percentile,
        "uhi": uhi,
    }
    if ssp:  cfg["ssp"] = ssp
    if year: cfg["year"] = year

    # Parallel: stats (with full ASHRAE) + diurnal chart. Each call goes
    # through analyze_weather / chart_weather which apply the v0.5.0
    # real-station base_url default for free.
    stats_task = analyze_weather(
        config=cfg,
        include_full_ashrae=True,
        allow_custom_location=allow_custom_location,
    )
    chart_task = chart_weather(
        config=cfg,
        chart_type="diurnal",
        allow_custom_location=allow_custom_location,
    )
    stats, chart = await asyncio.gather(stats_task, chart_task)

    return {
        "analysis": stats,
        "chart_svg": chart.get("svg") if isinstance(chart, dict) else None,
        "control_state": {
            "ssp": ssp,
            "year": year,
            "percentile": percentile,
            "uhi": uhi,
        },
        "meta": _meta("explore_design_conditions"),
    }


# ============================================================================
# Tool 5: generate_weather_file — auth + credits required
# ============================================================================
@mcp.tool()
async def generate_weather_file(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude, decimal degrees")] = None,  # type: ignore[assignment]
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude, decimal degrees")] = None,  # type: ignore[assignment]
    basis: Annotated[
        Literal["tmy", "amy"],
        Field(description='"tmy" for typical met year (default) or "amy" for a specific year'),
    ] = "tmy",
    amy_year: Annotated[int | None, Field(description="Year for AMY basis. Only when basis='amy'.")] = None,
    ssp: Annotated[
        Literal["ssp126", "ssp245", "ssp370"] | None,
        Field(description="CMIP6 emission scenario for future-climate morphing. ssp585 was deprecated per CMIP7 (deemed implausible) — use ssp370 as the high-end scenario."),
    ] = None,
    year: Annotated[
        Literal[2030, 2035, 2040, 2045, 2050, 2060, 2070, 2080, 2090, 2100] | None,
        Field(description="Future horizon. Required if ssp is set."),
    ] = None,
    percentile: Annotated[
        Literal[5, 10, 25, 50, 75, 90, 95],
        Field(description="Warming percentile across the CMIP6 ensemble."),
    ] = 50,
    uhi: Annotated[
        Literal["none", "suburban", "urban", "dense_urban"],
        Field(description="Urban Heat Island preset."),
    ] = "none",
    events: Annotated[
        str | None,
        Field(description='Comma-separated events: heatwave, coldsnap, hothumid, coldwindy.'),
    ] = None,
    event_duration: Annotated[int, Field(ge=3, le=30)] = 14,
    intensity: Annotated[
        str | None,
        Field(description='Per-event "type:1-7" intensity (1-10 with stress_test=true). Example: "heatwave:7,coldsnap:5".'),
    ] = None,
    intensity_auto: Annotated[bool, Field(description="Auto-fill unspecified intensities from AR6 when ssp is set.")] = True,
    stress_test: Annotated[bool, Field(description="Unlock intensity 8-10. Default false.")] = False,
    smoke: Annotated[bool, Field(description="Enable wildfire smoke overlay.")] = False,
    smoke_intensity: Annotated[int | None, Field(ge=1, le=10, description="Smoke severity 1-10.")] = None,
    smoke_duration: Annotated[int | None, Field(ge=3, le=30, description="Smoke days.")] = None,
    tmy_period: Annotated[
        TmyPeriod,
        Field(description=f"TMYx vintage. Default {DEFAULT_TMY_PERIOD}."),
    ] = DEFAULT_TMY_PERIOD,
    format: Annotated[Literal["epw", "ddy"], Field(description="Output format (default epw).")] = "epw",
    include_ddy: Annotated[
        bool,
        Field(description="When format=epw, also include the matching DDY in the response. Single-file only."),
    ] = False,
    ensemble: Annotated[
        bool,
        Field(description="Generate per-model CMIP6 ensemble (~20 EPWs, one per climate model). Costs 10 credits. Requires ssp + year."),
    ] = False,
    scenarios: Annotated[
        list[dict[str, Any]] | None,
        Field(
            max_length=10,
            description=(
                "Batch mode — list of full configs (max 10), each generated in parallel. "
                "Same shape as the top-level params. When set, top-level params are ignored. "
                "Costs 1 credit per scenario."
            ),
        ),
    ] = None,
    save_to: Annotated[
        str | None,
        Field(description="Local path to write the file to (single-file mode only). Returns path + bytes instead of base64."),
    ] = None,
    save_to_dir: Annotated[
        str | None,
        Field(description="Directory to write per-model / per-scenario files to (ensemble or batch mode)."),
    ] = None,
) -> dict[str, Any]:
    """Generate and deliver an EPW or DDY file. Requires an EPWFORGE_API_KEY.

    Charges credits per call: 1 for single, 1×N for scenarios batch, 10 for
    ensemble. Free signup at https://epwforge.com includes 5 welcome credits.

    Three modes:
      1. Single file (default): generate_weather_file(lat=40.7, lon=-74,
            ssp="ssp245", year=2050)
      2. Batch (1×N): generate_weather_file(scenarios=[{lat, lon, ssp:...}, ...])
      3. Ensemble (10 credits): generate_weather_file(lat=, lon=, ssp=,
            year=, ensemble=True) — returns ~20 per-model EPWs

    For analysis / charts without paying credits, use analyze_weather or
    chart_weather with a `config` argument — same morph/UHI/event pipeline,
    stats/SVG returned, no EPW delivered.
    """
    _assert_ssp_allowed(scenarios)
    client = _get_client()
    client.require_api_key()

    # ── Batch mode
    if scenarios:
        if len(scenarios) > 10:
            raise ValueError("scenarios max 10 per call")
        endpoint = "/api/design-day" if format == "ddy" else "/api/epwforge"
        out_dir = Path(save_to_dir).expanduser().resolve() if save_to_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        async def _run(i: int, cfg: dict[str, Any]) -> dict[str, Any]:
            if "lat" not in cfg or "lon" not in cfg:
                return {"index": i, "label": cfg.get("label"), "ok": False, "error": "config missing lat/lon"}
            params = _build_params(cfg, format)
            try:
                data = await client.get_json(endpoint, params)
            except EPWForgeError as e:
                return {"index": i, "label": cfg.get("label"), "ok": False, "error": str(e), "config": cfg}
            entry: dict[str, Any] = {
                "index": i,
                "label": cfg.get("label"),
                "ok": True,
                "config": cfg,
                "weather_basis": _weather_basis_synthesized(cfg.get("basis", "tmy"), cfg.get("amy_year"), cfg.get("tmy_period", DEFAULT_TMY_PERIOD)),
            }
            b64_key = "ddy_base64" if format == "ddy" else "epw_base64"
            if out_dir and data.get(b64_key):
                label = cfg.get("label") or f"cfg{i + 1}"
                ext = format
                scenario_tag = "_".join(filter(None, [cfg.get("ssp"), str(cfg.get("year")) if cfg.get("year") else None]))
                suffix = f"_{scenario_tag}" if scenario_tag else ""
                fname = f"{label}_{cfg['lat']}_{cfg['lon']}{suffix}.{ext}"
                fpath = out_dir / fname
                n = write_epw_base64(data[b64_key], fpath)
                entry.update({"path": str(fpath), "filename": fname, "bytes_written": n})
            else:
                entry["data"] = data
            return entry

        results = list(await asyncio.gather(*(_run(i, c) for i, c in enumerate(scenarios))))
        return {
            "mode": "batch",
            "count": len(results),
            "ok_count": sum(1 for r in results if r.get("ok")),
            "directory": str(out_dir) if out_dir else None,
            "results": results,
            "meta": _meta("generate_weather_file", mode="batch", n_scenarios=len(scenarios)),
        }

    # ── Ensemble mode
    if ensemble:
        if not ssp or not year:
            raise ValueError("ensemble=True requires ssp and year")
        params = _build_params({
            "lat": lat, "lon": lon, "ssp": ssp, "year": year, "percentile": percentile,
            "basis": basis, "amy_year": amy_year, "uhi": uhi, "events": events,
            "event_duration": event_duration, "intensity": intensity,
            "intensity_auto": intensity_auto, "stress_test": stress_test,
            "smoke": smoke, "smoke_intensity": smoke_intensity, "smoke_duration": smoke_duration,
            "tmy_period": tmy_period,
        }, format)
        data = await client.get_json("/api/ensemble-epw", params)
        data["weather_basis"] = _weather_basis_synthesized(basis, amy_year, tmy_period)
        data["mode"] = "ensemble"

        if save_to_dir:
            out_dir = Path(save_to_dir).expanduser().resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            saved = []
            for m in data.get("members", []):
                if not m.get("epw_base64"):
                    continue
                name = f"{m['model']}_{ssp}_{year}.epw"
                n = write_epw_base64(m["epw_base64"], out_dir / name)
                saved.append({
                    "model": m["model"],
                    "path": str(out_dir / name),
                    "bytes": n,
                    "avg_delta_temp": m.get("avg_delta_temp"),
                })
            return {
                "mode": "ensemble",
                "scenario": data.get("scenario"),
                "year": data.get("year"),
                "n_models": data.get("n_models"),
                "directory": str(out_dir),
                "members": saved,
                "weather_basis": data["weather_basis"],
                "meta": _meta("generate_weather_file", mode="ensemble"),
            }
        return data

    # ── Single-file mode (default)
    if lat is None or lon is None:
        raise ValueError("generate_weather_file requires lat and lon (unless scenarios is provided)")
    params = _build_params({
        "lat": lat, "lon": lon, "basis": basis, "amy_year": amy_year,
        "ssp": ssp, "year": year, "percentile": percentile, "uhi": uhi,
        "events": events, "event_duration": event_duration,
        "intensity": intensity, "intensity_auto": intensity_auto, "stress_test": stress_test,
        "smoke": smoke, "smoke_intensity": smoke_intensity, "smoke_duration": smoke_duration,
        "tmy_period": tmy_period,
    }, format)
    if format == "epw" and include_ddy:
        params["include_ddy"] = "true"

    endpoint = "/api/design-day" if format == "ddy" else "/api/epwforge"
    data = await client.get_json(endpoint, params)
    b64_key = "ddy_base64" if format == "ddy" else "epw_base64"
    out = _handle_file_response(data, b64_key, save_to)
    out["weather_basis"] = _weather_basis_synthesized(basis, amy_year, tmy_period)
    out["mode"] = "single"
    out["meta"] = _meta("generate_weather_file", mode="single", format=format)
    return out


# ============================================================================
# Helpers
# ============================================================================

def _meta(tool: str, **extra: Any) -> dict[str, Any]:
    return {
        "tool": tool,
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }


def _location_imperial(epw: EPWFile) -> dict[str, Any]:
    loc = epw.location
    return {
        "city": loc.city,
        "state": loc.state,
        "country": loc.country,
        "lat": loc.latitude,
        "lon": loc.longitude,
        "elevation_ft": round(m_to_ft(loc.elevation_m), 0),
        "timezone": loc.timezone,
    }


def _summarize_epw(epw: EPWFile, *, source_url: str) -> dict[str, Any]:
    h = epw.hourly
    db_c = h.dry_bulb_c
    db_f = [c_to_f(c) for c in db_c]

    daily_mean_f = daily_means_by_date(db_f, h.month, h.day)
    peak_cooling_key = max(daily_mean_f, key=daily_mean_f.get)
    peak_heating_key = min(daily_mean_f, key=daily_mean_f.get)
    hdd_65 = sum(max(0.0, 65.0 - dm) for dm in daily_mean_f.values())
    cdd_65 = sum(max(0.0, dm - 65.0) for dm in daily_mean_f.values())
    ghi_total_kwh = sum(h.ghi_wh_m2) / 1000.0
    monthly_f = monthly_means(db_f, h.month)

    return {
        "source_url": source_url,
        "location": _location_imperial(epw),
        "annual_mean_temp_F": round(sum(db_f) / len(db_f), 1),
        "cooling_design_db_F": round(c_to_f(percentile(db_c, 99.0)), 1),
        "heating_design_db_F": round(c_to_f(percentile(db_c, 1.0)), 1),
        "peak_cooling_day": format_md(*peak_cooling_key),
        "peak_heating_day": format_md(*peak_heating_key),
        "hdd_65_annual": round(hdd_65, 0),
        "cdd_65_annual": round(cdd_65, 0),
        "ghi_total_annual_kwh_per_m2": round(ghi_total_kwh, 0),
        "monthly_mean_temp_F": [round(v, 1) for v in monthly_f],
        "n_hours": len(db_c),
        "meta": _meta("analyze_weather"),
    }


def _build_params(cfg: dict[str, Any], format: str = "epw") -> dict[str, Any]:
    """Build query params for /api/epwforge or /api/design-day from a config dict.

    Accepts both the rich top-level argument set and a minimal `{lat, lon}`-style
    batch config. format=json is forced so we get JSON with base64 back.
    """
    smoke_flag = cfg.get("smoke")
    return {
        "lat": cfg["lat"],
        "lon": cfg["lon"],
        "basis": cfg.get("basis", "tmy"),
        "amy_year": cfg.get("amy_year"),
        "ssp": cfg.get("ssp"),
        "year": cfg.get("year"),
        "percentile": cfg.get("percentile", 50),
        "uhi": cfg.get("uhi", "none"),
        "events": cfg.get("events"),
        "event_duration": cfg.get("event_duration", 14),
        "intensity": cfg.get("intensity"),
        "intensity_auto": str(cfg.get("intensity_auto", True)).lower(),
        "stress_test": "true" if cfg.get("stress_test") else None,
        "smoke": "true" if smoke_flag else None,
        "smoke_intensity": cfg.get("smoke_intensity"),
        "smoke_duration": cfg.get("smoke_duration"),
        "tmy_period": cfg.get("tmy_period", DEFAULT_TMY_PERIOD),
        "format": "json",
    }


def _weather_basis_synthesized(basis: str, amy_year: int | None, tmy_period: str) -> dict[str, Any]:
    if basis == "amy":
        return {
            "type": "synthesized_amy",
            "vintage": f"AMY {amy_year}" if amy_year else "AMY (current year)",
            "source": "ECMWF ERA5 reanalysis (single year)",
        }
    return {
        "type": "synthesized_tmyx",
        "vintage": tmy_period,
        "source": "ECMWF ERA5 reanalysis via GuzzWeather (Finkelstein-Schafer)",
    }


def _handle_file_response(data: dict[str, Any], b64_key: str, save_to: str | None) -> dict[str, Any]:
    if save_to is None:
        return data
    b64 = data.get(b64_key)
    if not b64:
        return {**data, "warning": f"Response did not include {b64_key}; cannot save to disk"}
    n = write_epw_base64(b64, save_to)
    out = {k: v for k, v in data.items() if k != b64_key}
    out["saved_to"] = str(save_to)
    out["bytes_written"] = n
    return out


def _chart_result(svg: str, chart_type: str, source: str, save_to: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap an SVG into the standard chart_weather response, optionally saving to disk."""
    if save_to:
        return _save_svg(svg, chart_type, source, save_to, extra)
    body = {"svg": svg, "format": "svg", "chart_type": chart_type, "source": source, "meta": _meta("chart_weather", chart_type=chart_type)}
    if extra:
        body.update(extra)
    return body


def _save_svg(svg: str, chart_type: str, source: str, save_to: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(save_to).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    body = {
        "saved_to": str(path),
        "bytes_written": len(svg.encode("utf-8")),
        "format": "svg",
        "chart_type": chart_type,
        "source": source,
        "meta": _meta("chart_weather", chart_type=chart_type, saved=True),
    }
    if extra:
        body.update(extra)
    return body


async def _call_hosted_mcp(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Forward a tool call to the hosted MCP endpoint at /api/mcp.

    Used by analyze_weather + chart_weather when they're called with a `config`
    argument — the pipeline must run on EPWForge infra. No auth header is sent
    (read tools are anon-OK at the hosted endpoint). If the user has set
    EPWFORGE_API_KEY we still don't pass it for read tools — hosted MCP doesn't
    charge credits for analyze/chart regardless.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    async with httpx.AsyncClient(
        timeout=120.0,
        headers={"User-Agent": "epwforge-mcp"},
    ) as c:
        resp = await c.post(f"{_base_url()}/api/mcp", json=payload)
        if resp.status_code >= 400:
            raise EPWForgeError(resp.status_code, f"Hosted MCP returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
    if body.get("error"):
        err = body["error"]
        raise EPWForgeError(500, err.get("message", "Hosted MCP error"))
    result = body.get("result", {})
    if result.get("isError"):
        msg = result.get("content", [{}])[0].get("text", "Unknown tool error")
        raise EPWForgeError(500, msg)
    # MCP wraps the result as {content: [{type: "text", text: "<json>"}]}
    text = result.get("content", [{}])[0].get("text", "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Hosted tool returned non-JSON text — pass it through as a message field.
        return {"message": text}


# ============================================================================
# Entry point
# ============================================================================

def main() -> None:
    """Entry point — runs the MCP server on stdio.

    No longer requires EPWFORGE_API_KEY upfront. The 3 read tools work
    without a key; only generate_weather_file checks for one (and raises
    a clear error if missing).
    """
    if os.environ.get("EPWFORGE_API_KEY"):
        print(f"epwforge-mcp v{__version__}: API key detected — all 4 tools available", file=sys.stderr)
    else:
        print(
            f"epwforge-mcp v{__version__}: no EPWFORGE_API_KEY set — "
            "read tools (find_station, analyze_weather, chart_weather) work without auth. "
            "Set EPWFORGE_API_KEY (free at https://epwforge.com/account) to enable generate_weather_file.",
            file=sys.stderr,
        )
    mcp.run()


if __name__ == "__main__":
    main()
