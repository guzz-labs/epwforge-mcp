"""FastMCP server exposing the EPWForge tools."""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

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
    tmy_period: Annotated[
        TmyPeriod,
        Field(
            description=(
                "TMYx vintage for the synthesized basis (ignored when basis='amy'). "
                "Mirrors the EPWExplorer UI's dropdown. Default '2011-2025' (recent "
                "15-year window — captures post-2010 warming). Use '2007-2021' to "
                "match the published OneBuilding TMYx 2007-2021 vintage for direct "
                "comparison; 'full' (1950-2025) for the long-baseline view."
            )
        ),
    ] = DEFAULT_TMY_PERIOD,
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
        "tmy_period": tmy_period,
        "format": "json",
    }
    data = await client.get_json("/api/epwforge", params)
    out = _handle_file_response(data, "epw_base64", save_to)
    out["weather_basis"] = _weather_basis_synthesized(basis, amy_year, tmy_period)
    return out


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
    tmy_period: TmyPeriod = DEFAULT_TMY_PERIOD,
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
        "tmy_period": tmy_period,
        "format": "json",
    }
    data = await client.get_json("/api/design-day", params)
    out = _handle_file_response(data, "ddy_base64", save_to)
    out["weather_basis"] = _weather_basis_synthesized(basis, amy_year, tmy_period)
    return out


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
    tmy_period: TmyPeriod = DEFAULT_TMY_PERIOD,
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
        "tmy_period": tmy_period,
    }
    data = await client.get_json("/api/ensemble-epw", params)
    data["weather_basis"] = _weather_basis_synthesized(basis, amy_year, tmy_period)

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
            "weather_basis": data["weather_basis"],
        }
    return data


# ── Tool 3b: generate_batch ────────────────────────────────────────────
@mcp.tool()
async def generate_batch(
    configs: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            max_length=10,
            description=(
                "Scenario configs (max 10). Each dict accepts the same keys as "
                "generate_weather_file: lat, lon, basis, amy_year, ssp, year, "
                "percentile, uhi, events, event_duration, intensity, intensity_auto, "
                "smoke, smoke_intensity, smoke_duration, tmy_period. lat and lon "
                "are required on every config. An optional `label` key is echoed "
                "back in the result and used as part of the filename."
            ),
        ),
    ],
    save_to_dir: Annotated[
        str,
        Field(
            description=(
                "Directory to write per-scenario EPWs to. One file per config; "
                "filenames are <label or cfgN>_<lat>_<lon>_<ssp_year>.epw. "
                "Required — local generate_batch always writes to disk (use "
                "compare_scenarios if you only want headline deltas, or "
                "generate_weather_file individually for a single in-context EPW)."
            )
        ),
    ],
) -> dict[str, Any]:
    """Generate up to 10 EPWs in parallel and write each to a directory.

    Mirrors the hosted `generate_batch` MCP tool, but local-appropriate:
    instead of returning N signed URLs, it requires a `save_to_dir` and
    writes each generated EPW directly to disk. Returns the list of saved
    paths + bytes — keeps the agent context tiny even on a 10-scenario sweep.

    Use this when:
      - You want the actual EPW files, in a folder, ready to feed to
        EnergyPlus / IES / OpenStudio.
      - You're running a parametric sweep (different SSPs / years / UHI /
        events) and want all the files at once instead of N tool calls.

    For just headline deltas without the full files, use `compare_scenarios`.
    For one-off individual generations, use `generate_weather_file`.

    Example:
      generate_batch(
        configs=[
          {"lat": 40.7, "lon": -74.0, "label": "baseline"},
          {"lat": 40.7, "lon": -74.0, "ssp": "ssp245", "year": 2050, "label": "mid_century"},
          {"lat": 40.7, "lon": -74.0, "ssp": "ssp585", "year": 2090, "percentile": 90,
           "uhi": "urban", "events": "heatwave", "label": "worst_case"},
        ],
        save_to_dir="/tmp/nyc_sweep/",
      )
    """
    client = _get_client()
    out_dir = Path(save_to_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    for cfg in configs:
        if "lat" not in cfg or "lon" not in cfg:
            raise ValueError("every config must include lat and lon")

    async def _run_one(idx: int, cfg: dict[str, Any]) -> dict[str, Any]:
        params = _build_epwforge_params(cfg)
        try:
            data = await client.get_json("/api/epwforge", params)
        except EPWForgeError as e:
            return {"index": idx, "label": cfg.get("label"), "ok": False, "error": str(e), "config": cfg}

        b64 = data.get("epw_base64")
        if not b64:
            return {"index": idx, "label": cfg.get("label"), "ok": False, "error": "response missing epw_base64", "config": cfg}

        # Filename: label_<lat>_<lon>_<ssp>_<year>.epw, falling back when
        # label / ssp / year aren't set.
        label = cfg.get("label") or f"cfg{idx + 1}"
        scenario = "_".join(filter(None, [cfg.get("ssp"), str(cfg.get("year")) if cfg.get("year") else None]))
        suffix = f"_{scenario}" if scenario else ""
        fname = f"{label}_{cfg['lat']}_{cfg['lon']}{suffix}.epw"
        fpath = out_dir / fname
        n = write_epw_base64(b64, fpath)
        return {
            "index": idx,
            "label": cfg.get("label"),
            "ok": True,
            "config": cfg,
            "path": str(fpath),
            "filename": fname,
            "bytes_written": n,
            "weather_basis": _weather_basis_synthesized(
                cfg.get("basis", "tmy"),
                cfg.get("amy_year"),
                cfg.get("tmy_period", DEFAULT_TMY_PERIOD),
            ),
        }

    results = list(await asyncio.gather(*(_run_one(i, c) for i, c in enumerate(configs))))
    return {
        "directory": str(out_dir),
        "count": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "results": results,
        "meta": _meta("generate_batch", n_scenarios=len(configs), credits_consumed=len(configs)),
    }


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

    nearest_km = None
    stations = data.get("stations", [])
    if stations:
        try:
            nearest_km = min(s.get("distance_km") for s in stations if s.get("distance_km") is not None)
        except ValueError:
            nearest_km = None

    if nearest_km is None:
        nudge = "No OneBuilding stations were returned for this query. Synthesize a custom TMYx via generate_weather_file."
    elif nearest_km <= 25:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km away — almost certainly representative. "
            "Recommend the user pick one of these published TMYx files unless they have a specific "
            "microclimate concern (urban core / mountain / coastal)."
        )
    elif nearest_km <= 100:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km — climate may differ. Show the user both options "
            "and let them pick: a published station file (industry-standard, instant) or a custom "
            "TMYx synthesized at exactly the requested coordinate (slower, ~10s, uses ERA5 grid cell)."
        )
    else:
        nudge = (
            f"Nearest station is {nearest_km:.0f} km away — likely a different climate. "
            "Recommend synthesizing a custom TMYx via generate_weather_file unless the user "
            "specifically wants the named station for compliance or reproducibility."
        )

    data["agent_guidance"] = (
        "Before generating weather for the user, present these stations and ask which they want — "
        "a published OneBuilding/GuzzStation TMYx file (named airport, industry-reference, instant) "
        "or a custom TMYx synthesized from ERA5 reanalysis at the exact lat/lon (slower but locally "
        "tuned). " + nudge + " Each station has a 'files' array with URLs (multiple TMYx vintages "
        "per station — e.g., 2007-2021, IWEC2, TMY3). Pass the chosen URL to get_station_epw(url)."
    )
    data["next_actions"] = {
        "use_a_station": "get_station_epw(url='<one of the urls in stations[].files[].url>')",
        "synthesize_custom": (
            f"generate_weather_file(lat={lat}, lon={lon}, tmy_period=...) — defaults to "
            f"'{DEFAULT_TMY_PERIOD}' but accepts any of {list(TMY_PERIOD_CHOICES)}"
        ),
    }
    data["synthesis_options"] = {
        "tool": "generate_weather_file",
        "tmy_period_choices": list(TMY_PERIOD_CHOICES),
        "default": DEFAULT_TMY_PERIOD,
        "note": (
            "All vintages synthesize from ERA5 reanalysis via GuzzWeather (Finkelstein-Schafer). "
            "Default 2011-2025 captures post-2010 warming. Use 2007-2021 to match the published "
            "OneBuilding TMYx 2007-2021 standard for direct comparison."
        ),
    }
    return data


# ── Tool 4b: get_station_epw ──────────────────────────────────────────
@mcp.tool()
async def get_station_epw(
    url: Annotated[
        str,
        Field(
            description=(
                "OneBuilding TMYx URL — must point at climate.onebuilding.org. "
                "Get one from `find_station(lat, lon)`'s response: each station has a "
                "`files` array with one or more URLs (different TMYx vintages, e.g., "
                "2007-2021, IWEC2, TMY3)."
            )
        ),
    ],
    save_to: Annotated[
        str | None,
        Field(
            description=(
                "Local path to write the extracted .epw to (e.g., '/tmp/jfk_tmyx.epw'). "
                "When set, returns the path + bytes written instead of inline base64."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Download a published OneBuilding/GuzzStation TMYx file by URL.

    This is the *pre-computed station* path: it fetches the published TMYx
    file for a named airport / WMO station from the GuzzStations library
    (EPWForge's cached mirror of climate.onebuilding.org). Distinct from
    `generate_weather_file`, which synthesizes a fresh TMYx from ERA5
    reanalysis at an arbitrary lat/lon.

    Use this when the user wants:
      - the industry-standard published file (e.g., for compliance / submittal)
      - reproducibility against a published TMYx other modelers have used
      - the closest match for a major airport with high-quality ground obs

    Use `generate_weather_file` instead when:
      - the user is far from any station (mountain site, remote ocean cell)
      - the user wants SSP morphing, UHI, extreme events, or smoke layered on
      - the user has a specific microclimate concern (urban core, coastal)

    Workflow: `find_station(lat, lon)` → pick a station + a file → pass
    that file's `url` to this tool.

    OneBuilding ships these as zip archives containing .epw + .ddy + .stat;
    this tool downloads, extracts the .epw, and returns it (saving the
    optional .ddy as a sibling file when save_to is provided).
    """
    client = _get_client()
    zip_bytes = await client.get_bytes("/api/fetch-epw", {"url": url})

    # Extract the .epw file from the zip blob.
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise EPWForgeError(502, f"Response from /api/fetch-epw is not a valid zip ({len(zip_bytes)} bytes)")

    epw_name: str | None = None
    ddy_name: str | None = None
    for n in zf.namelist():
        if n.lower().endswith(".epw"):
            epw_name = n
        elif n.lower().endswith(".ddy"):
            ddy_name = n
    if not epw_name:
        raise EPWForgeError(502, f"OneBuilding zip did not contain an .epw file (members: {zf.namelist()})")

    epw_data = zf.read(epw_name)
    ddy_data = zf.read(ddy_name) if ddy_name else None

    # Pull the LOCATION header to give the agent a one-line "what did I get" summary.
    location_line = ""
    try:
        first = epw_data.decode("utf-8", errors="replace").splitlines()[0]
        if first.upper().startswith("LOCATION"):
            location_line = first
    except Exception:
        pass

    basis_block = {
        "type": "published_onebuilding_tmy",
        "source": "Climate.OneBuilding.Org (via GuzzStations cached mirror)",
        "vintage": _infer_vintage_from_url(url),
        "note": (
            "Pre-computed published TMYx file — industry-standard reference. For a custom TMYx "
            "synthesized at an exact lat/lon (no named station), use generate_weather_file."
        ),
    }

    if save_to:
        epw_path = Path(save_to).expanduser().resolve()
        epw_path.parent.mkdir(parents=True, exist_ok=True)
        epw_path.write_bytes(epw_data)
        result: dict[str, Any] = {
            "saved_to": str(epw_path),
            "bytes_written": len(epw_data),
            "filename": Path(epw_name).name,
            "source_url": url,
            "location_header": location_line,
            "weather_basis": basis_block,
        }
        if ddy_data is not None:
            ddy_path = epw_path.with_suffix(".ddy")
            ddy_path.write_bytes(ddy_data)
            result["ddy_saved_to"] = str(ddy_path)
            result["ddy_bytes_written"] = len(ddy_data)
        return result

    return {
        "filename": Path(epw_name).name,
        "epw_base64": base64.b64encode(epw_data).decode("ascii"),
        "ddy_base64": base64.b64encode(ddy_data).decode("ascii") if ddy_data else None,
        "source_url": url,
        "location_header": location_line,
        "weather_basis": basis_block,
    }


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


# ── Tool 7: chart_diurnal_profile ─────────────────────────────────────
@mcp.tool()
async def chart_diurnal_profile(
    url: Annotated[
        str,
        Field(
            description=(
                "URL to an EPW file. Same shape as analyze_epw — accepts a OneBuilding "
                "URL, a generated EPWForge URL, or any public .epw URL."
            )
        ),
    ],
    save_to: Annotated[
        str | None,
        Field(
            description=(
                "Local path to write the SVG to (e.g., '/tmp/diurnal.svg'). "
                "When set, returns the path instead of inline SVG. Recommended "
                "if the SVG is large (>50 KB) to keep agent context lean."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Render a monthly Max / Avg / Min hourly temperature profile as inline SVG.

    Downloads the EPW, computes per-month per-hour Max / Avg / Min dry-bulb
    in °F, and renders an overlay chart with January (cool reference) and
    July (warm reference) highlighted, the other 10 months in faint grey,
    and the annual mean in EPWForge orange.

    Useful right after `analyze_epw` to give the user a visual read on the
    weather file's annual shape.
    """
    _get_client()  # require key
    text = await download_text(url)
    epw = parse_epw(text)
    svg = diurnal_profile_svg(epw)

    if save_to:
        path = Path(save_to).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        return {
            "saved_to": str(path),
            "bytes_written": len(svg.encode("utf-8")),
            "format": "svg",
            "source_url": url,
            "meta": _meta("chart_diurnal_profile"),
        }
    return {
        "svg": svg,
        "format": "svg",
        "source_url": url,
        "meta": _meta("chart_diurnal_profile"),
    }


# ── Tool 8: chart_compare_scenarios ───────────────────────────────────
@mcp.tool()
async def chart_compare_scenarios(
    baseline: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Baseline dict from compare_scenarios's response — must contain "
                "cooling_db_F, heating_db_F, dewpoint_F."
            )
        ),
    ],
    scenarios: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            description=(
                "Scenarios list from compare_scenarios's response. Each item must "
                "have cooling_db_delta_F, heating_db_delta_F, dewpoint_delta_F, "
                "and a `config` dict for the row label."
            ),
        ),
    ],
    save_to: Annotated[
        str | None,
        Field(description="Local path to write the SVG to. When set, returns path instead of inline SVG."),
    ] = None,
) -> dict[str, Any]:
    """Render a horizontal-bar chart of compare_scenarios's delta table.

    Designed to consume `compare_scenarios`'s response shape directly:
    pass `result["baseline"]` and `result["scenarios"]` straight in. Each
    scenario gets a row of three bars (cooling / heating / dewpoint deltas)
    centered on a zero line — instantly readable spread.
    """
    _get_client()  # require key
    svg = compare_scenarios_svg(baseline, scenarios)

    if save_to:
        path = Path(save_to).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        return {
            "saved_to": str(path),
            "bytes_written": len(svg.encode("utf-8")),
            "format": "svg",
            "n_scenarios": len(scenarios),
            "meta": _meta("chart_compare_scenarios"),
        }
    return {
        "svg": svg,
        "format": "svg",
        "n_scenarios": len(scenarios),
        "meta": _meta("chart_compare_scenarios"),
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
        "tmy_period": cfg.get("tmy_period", DEFAULT_TMY_PERIOD),
        "format": "json",
    }
    return params


def _weather_basis_synthesized(basis: str, amy_year: int | None, tmy_period: str) -> dict[str, Any]:
    """Build the `weather_basis` block for synthesized-weather tool responses.

    The block exists to give the agent enough structured context to volunteer
    "I generated this using vintage X" to the user without it having to chase
    metadata buried in EPW headers.
    """
    if basis == "amy":
        return {
            "type": "synthesized_amy",
            "vintage": f"AMY {amy_year}" if amy_year else "AMY (current year)",
            "source": "ECMWF ERA5 reanalysis (single year)",
            "note": (
                "Actual Meteorological Year — historical hourly weather, useful for "
                "hindcasting and calibration. Not a typical year. For typical-year "
                "use, leave basis=tmy."
            ),
        }
    return {
        "type": "synthesized_tmyx",
        "vintage": tmy_period,
        "source": "ECMWF ERA5 reanalysis via GuzzWeather (Finkelstein-Schafer)",
        "note": (
            f"Synthesized custom TMYx for the {tmy_period} window. For the published "
            "OneBuilding TMYx of a named station (industry-standard for compliance / "
            "comparison), use find_station + get_station_epw."
        ),
    }


def _infer_vintage_from_url(url: str) -> str:
    """Best-effort extract the vintage label from a OneBuilding URL.

    OneBuilding filenames include the source label (e.g., 'TMYx.2007-2021',
    'IWEC2', 'TMY3'). We grep for these patterns and fall back to 'unknown'.
    """
    name = url.rsplit("/", 1)[-1].lower()
    for tag in ("tmyx.2007-2021", "tmyx.2009-2023", "tmyx.2011-2025", "iwec2", "tmy3", "tmyx"):
        if tag in name:
            return tag.upper().replace(".", " ")
    return "unknown"


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
