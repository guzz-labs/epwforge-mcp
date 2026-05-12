"""FastMCP server exposing the four EPWForge tools."""

from __future__ import annotations

import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import EPWForgeClient, EPWForgeError, write_epw_base64


mcp = FastMCP("epwforge")

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
    """Generate an EPW weather file for any global location.

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
    """Find weather stations near a coordinate.

    Useful for "what data is available near X" queries. Returns station
    name, exact coordinates, WMO ID, distance, and (when available)
    climate zone. EPWForge can generate weather files for ANY lat/lon
    via TMYx synthesis — this is for users who want the nearest
    physically-observed station instead.

    Example:
      find_station(lat=40.7, lon=-74.0, max_results=5)
    """
    client = _get_client()
    data = await client.get_json("/api/stations", {"lat": lat, "lon": lon, "limit": max_results})
    return data


# ── Helpers ────────────────────────────────────────────────────────────
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
