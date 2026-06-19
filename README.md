# epwforge-mcp

> MCP server for [EPWForge](https://epwforge.com) — give Claude, Cursor, and other AI agents the ability to generate, morph, and download weather files for building energy simulation.

**Status:** 0.10.0 (Python). Four consolidated tools — `find_station`, `analyze_weather`, `chart_weather`, `generate_weather_file`. Production backend, all tier features wired in. Mirrored 1:1 by the hosted MCP at `https://epwforge.com/api/mcp` (Claude Web / hosted MCP clients get the same surface).

## What is EPWForge?

EPWForge generates and morphs weather files (`.epw`, `.ddy`, `.csv`) for building energy simulation tools — EnergyPlus, OpenStudio, IES VE, eQUEST, and any workflow that consumes EPW. The platform supports:

- **TMYx generation anywhere** — typical meteorological years synthesized from ERA5 reanalysis (1950–present) for any global lat/lon, or passthrough of published OneBuilding TMYx files for ~17,000 known stations.
- **AMY (Actual Meteorological Year)** — historical hourly weather for any specific year since 1950. Useful for stress-testing against observed extremes.
- **CMIP6 climate morphing** — apply SSP1-2.6 / SSP2-4.5 / SSP3-7.0 at horizons 2030–2100 across 7 warming percentiles, plus SSP5-8.5 as an opt-in extreme stress test. SSP3-7.0 is the recommended high-end for design. Belcher 2005 mean-shift hybridised with UKCP18 / NOAA Atlas 14 diurnal anomalies.
- **Urban Heat Island adjustment** — Stewart & Oke 2012 Local Climate Zone presets (suburban / urban / dense_urban).
- **Extreme event injection** — heatwave, cold snap, hot-humid, cold-windy, wildfire smoke. Per-event intensity 1–10, AR6-auto-fill under an SSP. Events stitched at the baseline's hottest / coldest 14-day window.
- **ASHRAE 169 design conditions** — full percentile bins (0.4 / 1 / 2 cooling, 99.6 / 99 heating, WB / DP / Enth variants), computed from the modified hourly distribution.
- **Output formats** — EnergyPlus (`.epw` + `.ddy` + `.stat`), CSV hourly, PVsyst, ESP-r `.clm`. Bundled `.zip` available.

## Tools

Four consolidated tools. Operations that were once separate tools (batch, ensemble, design-day, etc.) are now **parameters** on these four — see the deprecation map below.

| Tool | What it does | Auth |
|---|---|---|
| `find_station` | Search the ~17,000-station GuzzStations catalog by name, country, or coordinates. Optional `compact=True` returns just the newest TMYx per station (6–10× smaller responses for chained agent workflows). | none |
| `analyze_weather` | Statistical summary of an EPW. Three modes: `url=` (single file), `urls=[]` (2–10 file comparison), `config={...}` (synthesize a morphed scenario from lat/lon — no EPW content returned). Optional `include_full_ashrae`, `include_improbability`, `include_idf`. | none |
| `chart_weather` | Inline SVG chart. Single-EPW types: `diurnal`, `temp_carpet`, `wind_rose`, `monthly_boxplot`, `utci_carpet`, `economizer_carpet`, `pv_tilt_azimuth`, `solar_under_events`. Multi-EPW type: `comparison`. | none |
| `generate_weather_file` | Generate and return a downloadable weather file with the full morph stack. Format = `epw` / `ddy` / `csv` / `zip` / `pvsyst`. Supports `ensemble=true` (all SSPs at once). | API key + credits |

### Deprecation map (v0.2.0 consolidation)

Migrating from a pre-0.2.0 agent script? Old → new:

| Old tool | Now reached via |
|---|---|
| `generate_design_day` | `generate_weather_file(format="ddy")` |
| `generate_ensemble` | `generate_weather_file(ensemble=true)` |
| `generate_batch` | Loop `generate_weather_file` client-side, or `generate_weather_file(scenarios=[...])` |
| `get_station_epw` | Pass the `epw_url` from `find_station` to `analyze_weather` / `chart_weather`, or download directly |
| `analyze_epw` | `analyze_weather(url=...)` |
| `compare_scenarios` | `analyze_weather(config={...})` per scenario, or `analyze_weather(urls=[...])` for static EPWs |
| `chart_diurnal_profile` | `chart_weather(url=..., chart_type="diurnal")` |
| `chart_compare_scenarios` | `chart_weather(urls=[...], chart_type="comparison")` |
| `explore_design_conditions` | Removed in v0.9.0. Functionality being folded into `analyze_weather` with a scenario-grid widget (Phase 3 redesign — see brain-central notes). |

### Full reference

The **canonical reference** lives on the EPWForge site:

- **[epwforge.com/docs](https://epwforge.com/docs)** — every parameter, example call, error codes, methodology references, validation numbers.
- **Tool docstrings** in [`python/src/epwforge_mcp/server.py`](python/src/epwforge_mcp/server.py) — read-it-in-IDE source of truth, lifted into the docs page above.
- `tools/list` on the running server — most accurate, reflects the exact version installed.

## Quick examples

```python
# Find the nearest station to a coordinate, with token-efficient response
find_station(lat=40.71, lon=-74.01, compact=True)

# Analyze a published TMYx file
analyze_weather(url="https://.../USA_NY_New.York-JFK.AP.744860_TMYx.2011-2025.epw")

# Compare cooling design conditions across 3 cities
analyze_weather(urls=["...A.epw", "...B.epw", "...C.epw"])

# Synthesize a stress-test scenario at any lat/lon (no auth needed — no EPW content returned)
analyze_weather(
    config={
        "lat": 40.71, "lon": -74.01,
        "ssp": "ssp370", "year": 2050, "percentile": 75,
        "uhi": "urban",
        "events": "heatwave",
        "intensity": "heatwave:6",
        "event_duration": 14,
    },
    include_full_ashrae=True,
    include_improbability=True,
)

# Inline SVG chart (zero context cost vs base64 PNG)
chart_weather(url="https://.../...epw", chart_type="temp_carpet")

# Generate the actual downloadable file (auth + credits)
generate_weather_file(
    lat=40.71, lon=-74.01,
    format="zip",       # EPW + DDY + STAT bundled
    ssp="ssp370", year=2090, percentile=90,
    uhi="urban",
    events="heatwave,hothumid", event_duration=14,
    smoke_enabled=True, smoke_intensity=5,
)
```

## Install

```bash
pip install epwforge-mcp
# or, with uv:
uvx epwforge-mcp
```

Requires Python ≥ 3.10.

## Connecting to Claude / Cursor / other MCP clients

Add to your MCP client config (Claude Desktop's `claude_desktop_config.json`, Cursor's MCP settings, VS Code's MCP extension, Goose):

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

Get an API key (free or paid) at [epwforge.com/account](https://epwforge.com/account). Read-only tools (`find_station`, `analyze_weather`, `chart_weather`) work without a key; only `generate_weather_file` requires one.

For browser-based MCP clients (Claude Web, ChatGPT, etc.), use the **hosted** endpoint:

```
https://epwforge.com/api/mcp
```

## Credits & pricing

Credit-based. Every plan gets every feature; credits gate volume.

| Plan | Price | Credits/mo | $/credit |
|---|---|---|---|
| Free | $0 | 5 lifetime | n/a |
| Starter | $49 | 10 | $4.90 |
| Pro | $149 | 50 | $2.98 |
| Pro+ | $249 | 100 | $2.49 |

One-time top-up packs available: 5 cr / $50, 20 cr / $180, 50 cr / $400.

| Tool call | Cost |
|---|---|
| `find_station`, `analyze_weather`, `chart_weather` | 0 credits (no auth needed) |
| `generate_weather_file` — single file (epw / ddy / csv / pvsyst) | 1 credit |
| `generate_weather_file` — bundle (zip with 4+ files, AMY, all-SSP) | 2 credits |
| `generate_weather_file` — CMIP6 ensemble (per-model) | 10 credits |

Out-of-credits returns HTTP 402 with hints pointing at top-up packs and subscription upgrades. The MCP surfaces 402s as `ToolError` with a `hint` field; in agent loops this naturally routes the user to the upgrade flow.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `EPWFORGE_API_KEY` | Bearer token for `generate_weather_file` | none — read-only tools work without |
| `EPWFORGE_BASE_URL` | Override the API host (mainly for testing against a local backend) | `https://epwforge.com` |

## Behavior notes

- **Anon-safe by construction.** `find_station`, `analyze_weather`, and `chart_weather` never return EPW content; config-mode `analyze_weather` synthesizes the morphed scenario server-side and returns only stats. Only `generate_weather_file` touches credits / auth.
- **`agent_guidance` field.** Most responses include a short, judgment-shaping string the model can use to choose the right next step (e.g. "nearest station is 8 km — use it directly" vs "nearest station is 250 km — consider config-mode synthesis").
- **Inline SVG charts** rather than base64 PNG — typically 10× smaller in context. Each chart's `svg_size_kb` is reported up-front so agents can self-budget. Charts >50 KB auto-upload to Vercel Blob and return `svg_url` instead.
- **Compound events.** `events="heatwave,hothumid"` blends `hothumid`'s humidity onto the heatwave at 50%. `events="coldsnap,coldwindy"` blends wind onto the cold snap. Secondary folds into the primary stitch — not stitched separately.
- **Event placement.** Events anchor at the cell's hottest day (heat family) or coldest day (cold family), then center for the requested duration. The peak day's diurnal cycle is sustained across the event — a 30-day request gets 30 days of peak heat, not a stretched 14-day shape.
- **AR6 SSP auto-fill.** With an SSP active, unspecified event intensities auto-fill from IPCC AR6 ensemble factors for the cell's region. Cold-family events stay at intensity 5 (no future amplification) because recent observations don't yet support the AR6 ensemble's cold-side dampening. Pass `intensity_auto=false` to disable.

## Development

```bash
git clone https://github.com/guzz-labs/epwforge-mcp
cd epwforge-mcp/python
uv sync
uv run epwforge-mcp   # runs the stdio server
```

Run tests:

```bash
.venv/bin/python -m pytest tests/ -v   # 30+ tests
```

Test against a local API:

```bash
EPWFORGE_BASE_URL=http://localhost:3000 \
EPWFORGE_API_KEY=sk_live_... \
uv run epwforge-mcp
```

Version sync (before publishing):

```bash
python3 scripts/check-versions.py            # verify all 5 version strings agree
python3 scripts/check-versions.py --set 0.9.3 # bump everywhere atomically
```

## Links

- **Website:** [epwforge.com](https://epwforge.com)
- **Documentation:** [epwforge.com/docs](https://epwforge.com/docs)
- **REST API reference:** [epwforge.com/api-docs](https://epwforge.com/api-docs)
- **MCP connection guide:** [epwforge.com/mcp](https://epwforge.com/mcp)
- **Methodology + validation:** [epwforge.com/transparency](https://epwforge.com/transparency)
- **Pricing:** [epwforge.com/pricing](https://epwforge.com/pricing)
- **Hosted MCP endpoint:** `https://epwforge.com/api/mcp`
- **PyPI:** [pypi.org/project/epwforge-mcp](https://pypi.org/project/epwforge-mcp/)
- **Parent platform:** [Guzzlabs](https://guzzlabs.com)
- **Issues:** [github.com/guzz-labs/epwforge-mcp/issues](https://github.com/guzz-labs/epwforge-mcp/issues)

## License

MIT
