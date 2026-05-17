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


mcp = FastMCP("epwforge")
mcp._mcp_server.version = __version__

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
        Literal["ssp126", "ssp245", "ssp370", "ssp585"] | None,
        Field(description="SSP scenario (only used with include_climate_deltas)"),
    ] = None,
    year: Annotated[
        Literal[2030, 2050, 2070, 2090] | None,
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
@mcp.tool()
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
                "Multiple EPW URLs to compare (2-10). First is the baseline; "
                "others are reported as deltas from it."
            ),
        ),
    ] = None,
    config: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Synthesize an EPW server-side and analyze it. Same params as "
                "generate_weather_file (lat, lon, ssp, year, percentile, uhi, "
                "events, intensity, smoke, smoke_intensity, etc.). Routes through "
                "the hosted MCP at /api/mcp — runs the full morph/UHI/event pipeline "
                "and returns ONLY stats. The EPW content never reaches the caller. "
                "Anon-safe; no API key or credits required."
            )
        ),
    ] = None,
    include_full_ashrae: Annotated[
        bool,
        Field(description="Adds ASHRAE 0.4%/1%/2% cooling DB + 99.6%/99% heating DB design conditions. Routes through hosted MCP."),
    ] = False,
    include_improbability: Annotated[
        bool,
        Field(description="Adds EPWForge's stress-test improbability score (config mode only). Routes through hosted MCP."),
    ] = False,
    include_idf: Annotated[
        bool,
        Field(description="Adds ready-to-paste EnergyPlus SizingPeriod:DesignDay IDF objects to the response. Routes through hosted MCP."),
    ] = False,
) -> dict[str, Any]:
    """Compute design conditions, HDD/CDD, monthly stats, and peak days for one
    or more EPW files. No EPW content returned — stats only.

    Three modes:
      1. Single URL: analyze_weather(url="https://...")
      2. Multi-URL comparison: analyze_weather(urls=["...", "...", "..."])
      3. Synthesized config: analyze_weather(config={"lat": 40.7, "lon": -74,
          "ssp": "ssp585", "year": 2050, "uhi": "urban"})

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

    # If any enrichment is requested, route through hosted MCP (it has the
    # IDF emitter, full-ASHRAE computation, and improbability scorer in lib).
    if include_full_ashrae or include_improbability or include_idf:
        payload: dict[str, Any] = {
            "include_full_ashrae": include_full_ashrae,
            "include_improbability": include_improbability,
            "include_idf": include_idf,
        }
        if url: payload["url"] = url
        if urls: payload["urls"] = urls
        if config: payload["config"] = config
        return await _call_hosted_mcp("analyze_weather", payload)

    # Single URL — local fetch + parse.
    if url:
        text = await download_text(url)
        epw = parse_epw(text)
        return _summarize_epw(epw, source_url=url)

    # Multi-URL comparison — parallel fetch + parse, deltas vs first.
    if urls:
        async def _one(u: str) -> dict[str, Any]:
            text = await download_text(u)
            return _summarize_epw(parse_epw(text), source_url=u)
        summaries = list(await asyncio.gather(*(_one(u) for u in urls)))
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
            "meta": _meta("analyze_weather", mode="compare", n_urls=len(urls)),
        }

    # config mode — route through hosted MCP for the pipeline run.
    return await _call_hosted_mcp("analyze_weather", {"config": config})


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
            "temp_carpet = 8760-cell heatmap of hour x day-of-year. "
            "wind_rose = polar bars of direction x speed. "
            "monthly_boxplot = Q1/median/Q3 + whiskers per month. "
            "comparison = design-condition delta bars (needs urls). "
            "diurnal and comparison run locally; the 3 new types route through hosted MCP."
        )),
    ] = "diurnal",
    save_to: Annotated[
        str | None,
        Field(description="When set, writes SVG to this path and returns the path (saves agent context)."),
    ] = None,
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

    # New chart types (added in 0.3.0) live only in the hosted MCP; route there.
    if chart_type in ("temp_carpet", "wind_rose", "monthly_boxplot"):
        payload: dict[str, Any] = {"chart_type": chart_type}
        if url: payload["url"] = url
        if urls: payload["urls"] = urls
        if config: payload["config"] = config
        result = await _call_hosted_mcp("chart_weather", payload)
        if save_to and result.get("svg"):
            return _save_svg(result["svg"], chart_type, result.get("source", "synthesized"), save_to)
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
        # config — route through hosted MCP (it will fetch text internally).
        result = await _call_hosted_mcp("chart_weather", {"config": config, "chart_type": "diurnal"})
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
# Tool 4: generate_weather_file — auth + credits required
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
        Literal["ssp126", "ssp245", "ssp370", "ssp585"] | None,
        Field(description="CMIP6 emission scenario for future-climate morphing."),
    ] = None,
    year: Annotated[
        Literal[2030, 2050, 2070, 2090] | None,
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
