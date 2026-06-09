# epwforge-mcp

> MCP server for [EPWForge](https://epwforge.com) — give Claude, Cursor, and other AI agents the ability to generate, morph, and download weather files for building energy simulation.

**Status:** 0.9.2 (Python). Four consolidated tools — `find_station`, `analyze_weather`, `chart_weather`, `generate_weather_file`. Production backend, Pro-tier features wired in. Mirrored 1:1 by the hosted MCP at `https://epwforge.com/api/mcp` (so Claude Web users get the same surface).

> **Note (2026-06-09):** the tool surface was consolidated from ten tools to four in v0.2.0. Operations like batch generation, ensemble synthesis, and design-day generation are now *parameters* on the four core tools (e.g. `ensemble=...`, `scenarios=[...]`, `format="ddy"`). The full Tools section below still describes the older ten-tool surface and will be rewritten in a forthcoming release — for the actual current tool descriptions and arguments, see the tool docstrings in `python/src/epwforge_mcp/server.py` or call `tools/list` on the running server.

## What is EPWForge?

EPWForge generates and morphs weather files (`.epw`, `.ddy`) for building energy simulation tools — EnergyPlus, OpenStudio, IES VE, eQUEST, and any workflow that consumes EPW. The platform supports:

- **TMYx generation anywhere** — typical meteorological years synthesized from ERA5 reanalysis for any global lat/lon
- **AMY (Actual Meteorological Year)** — historical hourly weather for hindcasting and calibration
- **CMIP6 climate morphing** — apply future-scenario deltas (SSP1-2.6, SSP2-4.5, SSP3-7.0) at 7 warming percentiles. SSP5-8.5 was deprecated per CMIP7 (deemed implausible) — use SSP3-7.0 as the high-end scenario.
- **Urban Heat Island adjustment** — Stewart & Oke LCZ presets (suburban / urban / dense_urban)
- **Extreme event injection** — heat waves, cold snaps, humidity events, wind events, with auto-compound blending and per-event intensity (1-10 slider, AR6-auto-fill under SSP)
- **Wildfire smoke overlays** — CAMS-derived AOD with Beer-Lambert solar attenuation, RH bump, temp shift
- **Per-model CMIP6 ensembles** — up to 21 morphed EPWs (one per model) for inter-model uncertainty analysis

## Tools

Nine MCP tools — generation, station discovery + fetch, analysis, sensitivity sweep, and inline SVG charts:

| Tool | Purpose |
|---|---|
| `generate_weather_file` | **Synthesize** an EPW from ERA5 reanalysis at any global lat/lon. Combine basis + SSP + UHI + extreme events + smoke in one call. Default vintage **2011-2025** (recent 15 yr); pick another via `tmy_period`. |
| `generate_design_day` | DDY file for EnergyPlus design-day sizing, computed from the same enriched hourly data. |
| `generate_ensemble` | Per-model CMIP6 ensemble — one morphed EPW per climate model (Pro plan). |
| `generate_batch` | Generate up to 10 EPWs in parallel into a `save_to_dir`. Same param shape as `generate_weather_file` per config. Use for parametric sweeps when you want the actual files (not just deltas — that's `compare_scenarios`). |
| `find_station` | Search the GuzzStations / OneBuilding library for the nearest published TMYx stations. Returns each station's `files[]` URLs plus `agent_guidance` so the LLM asks the user "published station or synthesize?" before generating. |
| `get_station_epw` | **Fetch** a published OneBuilding/GuzzStation TMYx file by URL (URL comes from `find_station`). Returns the .epw (and .ddy when available). |
| `analyze_epw` | Download an EPW URL and summarize design conditions, degree-days, GHI, monthly temperature shape. No new generation. |
| `compare_scenarios` | Sensitivity sweep — up to 10 scenarios in parallel, returns only design-condition deltas vs baseline (no full EPW content). |
| `chart_diurnal_profile` | Inline SVG: monthly Max/Avg/Min hourly profile in °F. Highlights January + July with annual mean overlaid. |
| `chart_compare_scenarios` | Inline SVG: horizontal bar chart of cooling/heating/dewpoint deltas. Consumes `compare_scenarios`'s response shape directly. |

Most agents will use `find_station` to discover what's available, then either `get_station_epw` (for a published TMY) or `generate_weather_file` (for a custom synthesized one). Reach for `analyze_epw` / `compare_scenarios` for quick reads, and `chart_*` to visualize.

### Synthesized vs published — what's the difference?

| | `generate_weather_file` | `get_station_epw` |
|---|---|---|
| Source | ERA5 reanalysis at the exact lat/lon | Published TMYx for a named airport / WMO station |
| Speed | ~10s per call | ~1s (cached on the GuzzStations VPS mirror) |
| Customization | Full SSP / UHI / events / smoke / vintage stack | None — file is what it is |
| When to use | Custom site, microclimate concerns, future climate, what-if scenarios | Compliance / submittals, reproducibility, comparison to industry baseline |
| Vintage default | 2011-2025 (configurable via `tmy_period`) | Whatever the published file is — usually TMYx 2007-2021 |

## Quick example

```python
# What an AI agent might call to get a worst-case design weather file:
generate_weather_file(
    lat=40.71,
    lon=-74.01,
    ssp="ssp370",      # SSP3-7.0 emissions (high-end scenario; SSP5-8.5 deprecated per CMIP7)
    year=2090,         # End-of-century
    percentile=90,     # 90th percentile warming
    uhi="urban",       # Stewart-Oke urban LCZ
    events="heatwave,hothumid",  # Auto-compound heat + humidity
    event_duration=14,
    smoke=True,
    smoke_intensity=5,
    save_to="/tmp/nyc_2090_worst_case.epw",
)
```

Returns `{filename, saved_to, bytes_written, scenario, lat, lon, ...}` — no inline base64 bloat when `save_to` is set.

## Install

```bash
pip install epwforge-mcp
```

Requires Python ≥ 3.10.

## Connecting to Claude / Cursor

Add to your MCP client config (Claude Desktop's `claude_desktop_config.json`, Cursor's MCP settings, etc.):

```json
{
  "mcpServers": {
    "epwforge": {
      "command": "epwforge-mcp",
      "env": {
        "EPWFORGE_API_KEY": "sk_live_..."
      }
    }
  }
}
```

Generate an API key at [epwforge.com/account](https://epwforge.com/account).

## Plan requirements

| Feature | Plan |
|---|---|
| TMYx / AMY basis (`generate_weather_file`, `generate_design_day` without SSP) | Starter |
| UHI / events / smoke adjustments | Starter |
| SSP future-climate morphing | Pro |
| `generate_ensemble` (per-model CMIP6) | Pro |
| `analyze_epw` (parse-only, no generation) | Free (key required) |
| `compare_scenarios` | Inherits — each scenario counts as one generation under your tier |
| `find_station` / `get_station_epw` | Free (key required) — pre-computed files, no synthesis cost |
| `chart_diurnal_profile` / `chart_compare_scenarios` | Free (key required) — pure parse + render |

Tier enforcement happens at the API; the MCP surfaces 403s as `"Plan upgrade required — upgrade at https://epwforge.com/pricing"`.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `EPWFORGE_API_KEY` | Bearer token for the EPWForge API | required |
| `EPWFORGE_BASE_URL` | Override the API host (mainly for testing) | `https://epwforge.com` |

## Behavior notes

- **File output:** every file-generating tool accepts `save_to` (or `save_to_dir` for ensembles). When provided, the EPW is written to disk and the tool returns the path. When omitted, the EPW is returned base64-encoded in the response (≈ 250 KB for a typical year). **`save_to` is recommended** to keep agent context lean.
- **Compound events:** `events="heatwave,hothumid"` automatically blends `hothumid`'s humidity onto the heatwave at 50%. `events="coldsnap,coldwindy"` does the same for wind onto a cold snap. The secondary is folded into the primary stitch — not stitched separately.
- **Event placement:** events are anchored at the cell's hottest day (heat-family) or coldest day (cold-family) and centered for the requested duration. The peak day's diurnal cycle is sustained across the event, producing 30 days of peak heat for a 30-day request — not a stretched 14-day shape.
- **Smoke + heat compound:** when both are active, smoke aligns to the same anchor day and pads with peak AOD on the heat event's shoulders so solar is fully attenuated across the entire event window.
- **AR6 SSP auto-fill:** with an SSP scenario active, unspecified event intensities are auto-filled from IPCC AR6 ensemble factors for the cell's region. Cold events stay at intensity 5 (no future amplification) because recent observations (Texas 2021, polar-vortex disruption) don't yet support the AR6 ensemble's cold-side dampening. Pass `intensity_auto=false` to disable.

## Development

```bash
git clone https://github.com/guzz-labs/epwforge-mcp
cd epwforge-mcp/python
uv sync
uv run epwforge-mcp  # runs the stdio server
```

To test against a local API:

```bash
EPWFORGE_BASE_URL=http://localhost:3000 \
EPWFORGE_API_KEY=sk_live_... \
uv run epwforge-mcp
```

## Links

- **Website:** [epwforge.com](https://epwforge.com)
- **API docs / OpenAPI spec:** [epwforge.com/docs](https://epwforge.com/docs)
- **Maker:** [Guzzlabs](https://guzzlabs.com)
- **Issues:** [github.com/guzz-labs/epwforge-mcp/issues](https://github.com/guzz-labs/epwforge-mcp/issues)

## License

MIT
