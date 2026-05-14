"""FastMCP server exposing the EPWForge tools."""

from __future__ import annotations

import asyncio
import base64
import sys
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__
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


mcp = FastMCP("epwforge")
# FastMCP doesn't expose `version` on its constructor; set it on the wrapped
# low-level Server so MCP clients see the package version, not the SDK version.
mcp._mcp_server.version = __version__

# One client instance for the whole server lifetime.
_client: EPWForgeClient | None = None


def _get_client() -> EPWForgeClient:
    global _client
    if _client is None:
        _client = EPWForgeClient()
    return _client


# ── Tool 1 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def generate_weather_file(
    lat: Annotated[float, Field(ge=-90, le=90, description="Latitude in decimal degrees")],
    lon: Annotated[float, Field(ge=-180, le=180, description="Longitude in decimal degrees")],
    basis: Annotated[
        Literal["tmy", "amy"],
        Field(description='"tmy" for typical met year (default) or "amy" for a specific year'),
    ] = "tmy",
    amy_year: Annotated[
        int | None,
        Field(description="Year for AMY basis. Only used when basis='amy'."),
    ] = None,
    ssp: Annotated[
        Literal["ssp126", "ssp245", "ssp370", "ssp585"] | None,
        Field(description="CMIP6 emission scenario for future-climate morphing. Requires Pro plan."),
    ] = None,
    year: Annotated[
        Literal[2030, 2050, 2070, 2090] | None,
        Field(description="Future horizon. Required if ssp is set."),
    ] = None,
    percentile: Annotated[
        Literal[5, 10, 25, 50, 75, 90, 95],
        Field(description="Warming percentile across the CMIP6 ensemble. Used with ssp."),
    ] = 50,
    uhi: Annotated[
        Literal["none", "suburban", "urban", "dense_urban"],
        Field(description="Urban Heat Island preset (Stewart & Oke 2012 LCZ framework)."),
    ] = "none",
    events: Annotated[
        str | None,
        Field(
            description=(
                'Comma-separated extreme events to inject: any of '
                '"heatwave", "coldsnap", "hothumid", "coldwindy". '
                'Compound pairs auto-blend (heatwave+hothumid; coldsnap+coldwindy). '
                'Example: "heatwave,hothumid"'
            )
        ),
    ] = None,
    event_duration: Annotated[
        int,
        Field(ge=3, le=30, description="Length of each event in days (3-30)."),
    ] = 14,
    intensity: Annotated[
        str | None,
        Field(
            description=(
                'Per-event intensity as CSV of "type:1-10" pairs. '
                '5 = historical (1.0×), 1 = damped (0.5×), 10 = max (2.5×). '
                'Example: "heatwave:8,coldsnap:5". '
                'When ssp is set and intensity is omitted for an event, the AR6 '
                'ensemble auto-fills it (cold events stay floored at 5).'
            )
        ),
    ] = None,
    intensity_auto: Annotated[
        bool,
        Field(
            description=(
                "Auto-fill unspecified-event intensities from AR6 when ssp is set. "
                "Set to false to keep them at 5 (historical baseline)."
            )
        ),
    ] = True,
    smoke: Annotated[
        bool,
        Field(description="Enable wildfire smoke overlay (Beer-Lambert solar attenuation, RH bump, temp shift)."),
    ] = False,
    smoke_intensity: Annotated[
        int | None,
        Field(
            ge=1, le=10,
            description="Smoke severity 1-10 → peak AOD 0.1-6.0. Reference: 3 = NYC June 2023, 5-7 = Bay Area Sept 2020.",
        ),
    ] = None,
    smoke_duration: Annotated[
        int | None,
        Field(ge=3, le=30, description="Smoke event length in days."),
    ] = None,
    save_to: Annotated[
        str | None,
        Field(
            description=(
                "Local path to write the EPW file to (e.g., '/tmp/weather.epw'). "
                "When set, returns the path and bytes written instead of inline base64. "
                "Recommended for EnergyPlus / OpenStudio workflows."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Synthesize an EPW weather file for any global lat/lon (no station required).

    This is the *custom-generation* path: EPWForge synthesizes a TMYx (or
    AMY) from ERA5 reanalysis on a 0.25-degree grid, with optional CMIP6
    morphing, UHI, extreme events, and smoke layered on. The output is
    a fresh file labeled "GuzzWeather ERA5 reanalysis" — distinct from the
    pre-computed OneBuilding TMY stations returned by `find_station`.

    The single workhorse endpoint. Combine basis + ssp + uhi + events + smoke
    in one call. Examples:

      Basic TMYx for NYC:
        generate_weather_file(lat=40.7, lon=-74.0)

      Future climate (SSP2-4.5 by 2050, P50):
        generate_weather_file(lat=40.7, lon=-74.0, ssp="ssp245", year=2050)

      Worst-case design scenario (SSP5-8.5 2090 P90 + urban UHI + 14-day heatwave):
        generate_weather_file(lat=40.7, lon=-74.0, ssp="ssp585", year=2090,
                              percentile=90, uhi="urban", events="heatwave")

      Compound heat + smoke (NYC 2020-like):
        generate_weather_file(lat=40.7, lon=-74.0, events="heatwave,hothumid",
                              smoke=True, smoke_intensity=4)

    Returns metadata + either the EPW base64 (default) or a saved-file
    descriptor (when save_to is provided).
    """
    client = _get_client()
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "basis": basis,
        "amy_year": amy_year,
        "ssp": ssp,
        "year": year,
        "percentile": percentile,
        "uhi": uhi,
        "events": events,
        "event_duration": event_duration,
        "intensity": intensity,
        "intensity_auto": str(intensity_auto).lower(),
        "smoke": str(smoke).lower() if smoke else None,
        "smoke_intensity": smoke_intensity,
        "smoke_duration": smoke_duration,
        "format": "json",
    }
    data = await client.get_json("/api/epwforge", params)
    return _handle_file_response(data, "epw_base64", save_to)


# ── Tool 2 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def generate_design_day(
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
    basis: Literal["tmy", "amy"] = "tmy",
    amy_year: int | None = None,
    ssp: Literal["ssp126", "ssp245", "ssp370", "ssp585"] | None = None,
    year: Literal[2030, 2050, 2070, 2090] | None = None,
    percentile: Literal[5, 10, 25, 50, 75, 90, 95] = 50,
    uhi: Literal["none", "suburban", "urban", "dense_urban"] = "none",
    events: str | None = None,
    event_duration: Annotated[int, Field(ge=3, le=30)] = 14,
    intensity: str | None = None,
    intensity_auto: bool = True,
    smoke: bool = False,
    smoke_intensity: Annotated[int | None, Field(ge=1, le=10)] = None,
    smoke_duration: Annotated[int | None, Field(ge=3, le=30)] = None,
    save_to: str | None = None,
) -> dict[str, Any]:
    """Generate an ASHRAE design day (DDY) file for EnergyPlus.

    Computes ASHRAE 0.4%/1%/2% design conditions from the hourly data after
    applying every option (UHI, events, smoke, SSP morph). Useful when you
    want design conditions that reflect a future + extreme-event scenario,
    not just historical 30-year ASHRAE values.

    Same parameters as generate_weather_file. Returns a DDY file.
    """
    client = _get_client()
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "basis": basis,
        "amy_year": amy_year,
        "ssp": ssp,
        "year": year,
        "percentile": percentile,
        "uhi": uhi,
        "events": events,
        "event_duration": event_duration,
        "intensity": intensity,
        "intensity_auto": str(intensity_auto).lower(),
        "smoke": str(smoke).lower() if smoke else None,
        "smoke_intensity": smoke_intensity,
        "smoke_duration": smoke_duration,
        "format": "json",
    }
    data = await client.get_json("/api/design-day", params)
    return _handle_file_response(data, "ddy_base64", save_to)


# ── Tool 3 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def generate_ensemble(
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
    ssp: Literal["ssp126", "ssp245", "ssp370", "ssp585"],
    year: Literal[2030, 2050, 2070, 2090],
    percentile: Literal[5, 10, 25, 50, 75, 90, 95] = 50,
    basis: Literal["tmy", "amy"] = "tmy",
    amy_year: int | None = None,
    uhi: Literal["none", "suburban", "urban", "dense_urban"] = "none",
    events: str | None = None,
    event_duration: Annotated[int, Field(ge=3, le=30)] = 14,
    intensity: str | None = None,
    intensity_auto: bool = True,
    smoke: bool = False,
    smoke_intensity: Annotated[int | None, Field(ge=1, le=10)] = None,
    smoke_duration: Annotated[int | None, Field(ge=3, le=30)] = None,
    save_to_dir: Annotated[
        str | None,
        Field(
            description=(
                "Directory to write per-model EPW files to. When set, returns paths "
                "instead of inline base64. Filenames are <model>_<ssp>_<year>.epw."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Generate a per-model CMIP6 ensemble (one morphed EPW per climate model).

    Returns up to 21 model-specific EPWs for true inter-model uncertainty
    analysis. Each member uses that model's physically consistent delta
    set. Responses can be 10-15 MB and take several seconds. Pro plan
    required.

    Same adjustment options as generate_weather_file are applied to every
    member of the ensemble so comparisons are internally consistent.

    Example:
      generate_ensemble(lat=40.7, lon=-74.0, ssp="ssp585", year=2090,
                       uhi="urban", events="heatwave",
                       save_to_dir="/tmp/nyc_ssp585_2090/")
    """
    client = _get_client()
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "ssp": ssp,
        "year": year,
        "percentile": percentile,
        "basis": basis,
        "amy_year": amy_year,
        "uhi": uhi,
        "events": events,
        "event_duration": event_duration,
        "intensity": intensity,
        "intensity_auto": str(intensity_auto).lower(),
        "smoke": str(smoke).lower() if smoke else None,
        "smoke_intensity": smoke_intensity,
        "smoke_duration": smoke_duration,
    }
    data = await client.get_json("/api/ensemble-epw", params)

    if save_to_dir:
        from pathlib import Path
        dir_path = Path(save_to_dir).expanduser().resolve()
        dir_path.mkdir(parents=True, exist_ok=True)
        saved = []
        for m in data.get("members", []):
            name = f"{m['model']}_{ssp}_{year}.epw"
            n = write_epw_base64(m["epw_base64"], dir_path / name)
            saved.append({"model": m["model"], "path": str(dir_path / name),
                          "bytes": n, "avg_delta_temp": m.get("avg_delta_temp")})
        return {
            "scenario": data.get("scenario"),
            "year": data.get("year"),
            "n_models": data.get("n_models"),
            "directory": str(dir_path),
            "members": saved,
        }
    return data


# ── Tool 4 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def find_station(
    lat: Annotated[float, Field(ge=-90, le=90)],
    lon: Annotated[float, Field(ge=-180, le=180)],
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find the nearest pre-computed OneBuilding TMY stations to a coordinate.

    This searches the GuzzStations library — EPWForge's mirror of the
    Climate.OneBuilding.org TMY catalog (~17,000 named airport / WMO
    stations worldwide). Each result includes the station's published
    EPW download URL.

    NOTE: this is the *pre-computed station* path. If no station is close
    enough or the location is remote, you don't need a station at all —
    `generate_weather_file(lat, lon, ...)` synthesizes a custom TMYx from
    ERA5 reanalysis at any global lat/lon (no station required).

    Response shape:
      { count, stations: [{ city, state, country, lat, lon, distance_km,
                            files: [{ source, period, url }, ...] }, ...] }

    Example:
      find_station(lat=40.7, lon=-74.0, max_results=5)
    """
    client = _get_client()
    data = await client.get_json("/api/stations", {"lat": lat, "lon": lon, "limit": max_results})
    return data


# ── Tool 5 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def analyze_epw(
    url: Annotated[
        str,
        Field(
            description=(
                "URL to an EPW file. Accepts signed Vercel Blob URLs returned "
                "by other EPWForge tools, OneBuilding mirror URLs, or any "
                "publicly fetchable .epw file."
            )
        ),
    ],
) -> dict[str, Any]:
    """Download an EPW URL and summarize its design conditions, degree-days,
    solar resource, and monthly temperature shape.

    No new generation — this just fetches the URL, parses the 8760 hourly
    records, and returns a compact statistical summary in imperial units.
    Useful when you've just generated an EPW (or have one handy) and want a
    quick read of "is this file what I expected" without a full simulation.

    Computed fields:
      - location: city, lat, lon, elevation_ft, timezone (from EPW header)
      - annual_mean_temp_F
      - cooling_design_db_F  (1% percentile, ASHRAE-style)
      - heating_design_db_F  (99% percentile, ASHRAE-style)
      - peak_cooling_day, peak_heating_day  (MM-DD of hottest/coldest day mean)
      - hdd_65_annual, cdd_65_annual  (degree-days base 65 °F)
      - ghi_total_annual_kwh_per_m2
      - monthly_mean_temp_F  (12-element array, Jan..Dec)

    Example:
      analyze_epw(url="https://blob.vercel-storage.com/epws/abc...epw?...")
    """
    # _get_client() validates EPWFORGE_API_KEY is set even though the
    # download itself is unauthenticated — this keeps the tool gated to
    # users who have signed up, mirroring the rest of the surface.
    _get_client()

    text = await download_text(url)
    epw = parse_epw(text)
    return _summarize_epw(epw, source_url=url)


# ── Tool 6 ─────────────────────────────────────────────────────────────
@mcp.tool()
async def compare_scenarios(
    configs: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            max_length=10,
            description=(
                "List of scenario configs (max 10). Each dict accepts the same "
                "keys as generate_weather_file: lat, lon, basis, amy_year, ssp, "
                "year, percentile, uhi, events, event_duration, intensity, "
                "intensity_auto, smoke, smoke_intensity, smoke_duration. lat "
                "and lon are required on every config."
            ),
        ),
    ],
    baseline_url: Annotated[
        str | None,
        Field(
            description=(
                "Optional EPW URL to use as the baseline (any public or signed "
                "URL — same shape as analyze_epw). When omitted, a fresh TMYx "
                "is generated for the first config's lat/lon and used as the "
                "baseline (counts as one extra scenario credit)."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Sensitivity sweep — generate up to 10 scenarios and return only the
    headline design-condition deltas vs a baseline. No full EPW content in
    the response, so even a 10-scenario sweep stays under ~2 KB of context.

    For each config: generates the EPW server-side (calls /api/epwforge),
    parses the 8760 hourly distribution, computes 1% cooling DB / 99%
    heating DB / 1% dewpoint, and reports the value plus delta vs baseline.

    Returns:
      {
        baseline: { source: <url|"generated">, location: {...},
                    cooling_db_F, heating_db_F, dewpoint_F },
        scenarios: [
          { config: <original input dict>,
            cooling_db_F, cooling_db_delta_F,
            heating_db_F, heating_db_delta_F,
            dewpoint_F,   dewpoint_delta_F },
          ...
        ],
        meta: { tool, version, timestamp, n_scenarios, credits_consumed }
      }

    Example:
      compare_scenarios(configs=[
        {"lat": 40.7, "lon": -74.0, "ssp": "ssp245", "year": 2050},
        {"lat": 40.7, "lon": -74.0, "ssp": "ssp585", "year": 2090, "percentile": 90},
        {"lat": 40.7, "lon": -74.0, "ssp": "ssp585", "year": 2090,
         "percentile": 90, "uhi": "urban", "events": "heatwave"},
      ])
    """
    client = _get_client()

    # ── Baseline ──
    credits = 0
    if baseline_url:
        baseline_text = await download_text(baseline_url)
        baseline_epw = parse_epw(baseline_text)
        baseline_source = baseline_url
    else:
        if not configs:
            raise ValueError("compare_scenarios requires at least one config")
        first = configs[0]
        if "lat" not in first or "lon" not in first:
            raise ValueError("first config must include lat and lon when baseline_url is omitted")
        baseline_data = await client.get_json(
            "/api/epwforge",
            {"lat": first["lat"], "lon": first["lon"], "format": "json"},
        )
        baseline_text = _decode_epw_b64(baseline_data, "epw_base64")
        baseline_epw = parse_epw(baseline_text)
        baseline_source = "generated_tmyx"
        credits += 1

    baseline_dc = design_conditions_F(baseline_epw)

    # ── Scenarios — run in parallel via asyncio.gather ──
    # The platform serializes per-scenario generation work; running all N
    # configs concurrently cuts a 10-config sweep from ~30-50s to ~5-10s.
    for cfg in configs:
        if "lat" not in cfg or "lon" not in cfg:
            raise ValueError("every config must include lat and lon")

    async def _run_one(cfg: dict[str, Any]) -> dict[str, Any]:
        params = _build_epwforge_params(cfg)
        data = await client.get_json("/api/epwforge", params)
        text = _decode_epw_b64(data, "epw_base64")
        epw = parse_epw(text)
        dc = design_conditions_F(epw)
        return {
            "config": cfg,
            "cooling_db_F": dc["cooling_db_F"],
            "cooling_db_delta_F": round(dc["cooling_db_F"] - baseline_dc["cooling_db_F"], 1),
            "heating_db_F": dc["heating_db_F"],
            "heating_db_delta_F": round(dc["heating_db_F"] - baseline_dc["heating_db_F"], 1),
            "dewpoint_F": dc["dewpoint_F"],
            "dewpoint_delta_F": round(dc["dewpoint_F"] - baseline_dc["dewpoint_F"], 1),
        }

    scenarios_out = list(await asyncio.gather(*(_run_one(cfg) for cfg in configs)))
    credits += len(configs)

    return {
        "baseline": {
            "source": baseline_source,
            "location": _location_imperial(baseline_epw),
            **baseline_dc,
        },
        "scenarios": scenarios_out,
        "meta": _meta("compare_scenarios", n_scenarios=len(configs), credits_consumed=credits),
    }


# ── Helpers ────────────────────────────────────────────────────────────
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

    # Daily mean DB in °F for peak-day + degree-day calcs.
    daily_mean_f = daily_means_by_date(db_f, h.month, h.day)
    peak_cooling_key = max(daily_mean_f, key=daily_mean_f.get)
    peak_heating_key = min(daily_mean_f, key=daily_mean_f.get)

    hdd_65 = sum(max(0.0, 65.0 - dm) for dm in daily_mean_f.values())
    cdd_65 = sum(max(0.0, dm - 65.0) for dm in daily_mean_f.values())

    # GHI in kWh/m² (sum hourly Wh/m² → divide by 1000).
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
        "meta": _meta("analyze_epw"),
    }


def _build_epwforge_params(cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate a compare_scenarios config dict into /api/epwforge params.

    Mirrors generate_weather_file's param shape. format=json is forced so
    we get back epw_base64 (we need the raw EPW to compute design conds).
    """
    params: dict[str, Any] = {
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
        "smoke": str(cfg.get("smoke", False)).lower() if cfg.get("smoke") else None,
        "smoke_intensity": cfg.get("smoke_intensity"),
        "smoke_duration": cfg.get("smoke_duration"),
        "format": "json",
    }
    return params


def _decode_epw_b64(data: dict[str, Any], key: str) -> str:
    """Extract base64 EPW from an /api/epwforge JSON response and return text."""
    b64 = data.get(key)
    if not b64:
        raise EPWForgeError(
            502,
            f"EPWForge response missing {key} — cannot compute design conditions",
        )
    return base64.b64decode(b64).decode("utf-8", errors="replace")


def _handle_file_response(
    data: dict[str, Any],
    b64_key: str,
    save_to: str | None,
) -> dict[str, Any]:
    if save_to is None:
        return data
    b64 = data.get(b64_key)
    if not b64:
        return {**data, "warning": f"Response did not include {b64_key}; cannot save to disk"}
    n = write_epw_base64(b64, save_to)
    # Strip the base64 payload from the response to avoid context bloat
    out = {k: v for k, v in data.items() if k != b64_key}
    out["saved_to"] = str(save_to)
    out["bytes_written"] = n
    return out


def main() -> None:
    """Entry point — runs the MCP server on stdio."""
    try:
        # Validate the API key up front so users get a clear error before any tool call.
        # The client constructor raises with a helpful message when EPWFORGE_API_KEY is missing.
        EPWForgeClient()
    except RuntimeError as e:
        print(f"epwforge-mcp: {e}", file=sys.stderr)
        sys.exit(1)
    mcp.run()


if __name__ == "__main__":
    main()
