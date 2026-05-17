# epwforge-mcp

<!-- mcp-name: io.github.guzz-labs/epwforge-mcp -->

> MCP server for [EPWForge](https://epwforge.com) — give Claude, Cursor, and other AI agents the ability to search, analyze, chart, and generate weather files for building energy simulation.

**v0.2.0 — 4-tool consolidation.** Three of the four tools (`find_station`, `analyze_weather`, `chart_weather`) work **without an API key**, so anyone can explore EPWForge from Claude without signing up. Only `generate_weather_file` (which delivers actual EPW/DDY files) requires authentication and charges credits.

## What is EPWForge?

EPWForge generates and morphs weather files (`.epw`, `.ddy`) for building energy simulation tools — EnergyPlus, OpenStudio, IES VE, eQUEST, and any workflow that consumes EPW. The platform supports:

- **TMYx generation anywhere** — typical meteorological years synthesized from ERA5 reanalysis for any global lat/lon
- **AMY (Actual Meteorological Year)** — historical hourly weather for hindcasting and calibration
- **CMIP6 climate morphing** — apply future-scenario deltas (SSP1-2.6, SSP2-4.5, SSP3-7.0, SSP5-8.5) at 7 warming percentiles
- **Urban Heat Island adjustment** — Stewart & Oke LCZ presets (suburban / urban / dense_urban)
- **Extreme event injection** — heat waves, cold snaps, humidity events, wind events, with auto-compound blending and per-event intensity (1-10 slider, AR6-auto-fill under SSP)
- **Wildfire smoke overlays** — CAMS-derived AOD with Beer-Lambert solar attenuation
- **Per-model CMIP6 ensembles** — ~20 morphed EPWs (one per model) for inter-model uncertainty

## Tools

| Tool | Auth | Cost | What it does |
|---|---|---|---|
| `find_station` | None | Free | Search the GuzzStations catalog (17k+ weather stations). Returns matches with `epw_url` ready to pass to `analyze_weather` / `chart_weather`. |
| `analyze_weather` | None | Free | Design conditions, HDD/CDD, monthly stats, peak days. Accepts a `url` (existing EPW), `urls` (compare 2+), or `config` (synthesize on the fly with morphing/UHI/events/smoke — stats only, file never delivered). |
| `chart_weather` | None | Free | SVG chart from EPW URL(s) or a synthesized config. `chart_type="diurnal"` (monthly hourly profile) or `chart_type="comparison"` (design-condition deltas). |
| `generate_weather_file` | API key | 1–10 credits | Generates and delivers EPW or DDY. Single file (1 credit), scenarios batch up to 10 (1 credit each), or per-model CMIP6 ensemble (10 credits). |

The 3 read tools route purely through public endpoints when given a URL — pure local fetch + parse. When given a `config` they route through the hosted EPWForge MCP at `https://epwforge.com/api/mcp` so the morph/UHI/event/smoke pipeline runs on EPWForge infrastructure (the synthesized EPW never reaches the caller).

## Quick examples

### No-auth: explore Boston's future climate

```python
# 1. find a nearby station
stations = await find_station(query="Boston", max_results=3)
epw_url = stations["stations"][0]["files"][0]["epw_url"]

# 2. analyze that EPW (purely local — fetches and parses)
stats = await analyze_weather(url=epw_url)
# → cooling 89.1 F, heating 16.0 F, HDD/CDD 5265/914 ...

# 3. preview what a 2050 SSP585 + urban-UHI scenario looks like
# (routes through hosted MCP — runs the pipeline, returns stats only)
future = await analyze_weather(config={
    "lat": 42.36, "lon": -71.06,
    "ssp": "ssp585", "year": 2050, "uhi": "urban",
})
# → cooling 92.3 F (+3.2), heating 26.2 F (+10.2) ...

# 4. chart it
svg = await chart_weather(config={
    "lat": 42.36, "lon": -71.06, "ssp": "ssp585", "year": 2050,
}, chart_type="diurnal")
```

### With auth: deliver an EPW

```python
# Requires EPWFORGE_API_KEY in env
await generate_weather_file(
    lat=40.71, lon=-74.01,
    ssp="ssp585", year=2090, percentile=90,
    uhi="urban",
    events="heatwave,hothumid",
    event_duration=14,
    smoke=True, smoke_intensity=5,
    save_to="/tmp/nyc_2090_worst_case.epw",
)
# → {saved_to, bytes_written, weather_basis, ...}
```

## Install

```bash
pip install epwforge-mcp
```

Or use `uvx` (no install needed):

```bash
uvx epwforge-mcp
```

Requires Python ≥ 3.10.

## Connecting to Claude / Cursor

### Read-only (no signup)

```json
{
  "mcpServers": {
    "epwforge": {
      "command": "epwforge-mcp"
    }
  }
}
```

That's it — `find_station`, `analyze_weather`, and `chart_weather` all work immediately. `generate_weather_file` will return a clear "API key required" message if invoked.

### With API key (unlocks `generate_weather_file`)

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

**Free signup** at [epwforge.com/account](https://epwforge.com/account) gets you an API key and 5 welcome credits.

### One-command setup

```bash
# With API key
epwforge-mcp install --api-key sk_live_...

# Read-only (no key)
epwforge-mcp install --no-api-key
```

Auto-detects Claude Desktop / Claude Code / Cursor configs.

## Pricing & credit costs

Generation is the only paid surface:

| Action | Credits |
|---|---|
| Unmodified TMY download via `generate_weather_file` | 0 (free) |
| Single EPW / DDY / SSP / UHI / event-modified file | 1 |
| 4-file scenarios batch | 2 (50% bundle discount) |
| Per-model CMIP6 ensemble (~20 EPWs) | 10 |
| `find_station`, `analyze_weather`, `chart_weather` | 0 (always free) |

Plans: Free ($0, 5 lifetime credits) · Starter ($49/mo, 10) · Pro ($149/mo, 50) · Pro+ ($249/mo, 100). Full pricing at [epwforge.com/pricing](https://epwforge.com/pricing).

## Breaking changes from 0.1.x

v0.2.0 is a tool-name rewrite. The old tool surface is gone — agents calling `generate_design_day`, `generate_ensemble`, `generate_batch`, `get_station_epw`, `analyze_epw`, `compare_scenarios`, `chart_diurnal_profile`, or `chart_compare_scenarios` will get "tool not found" errors.

Migration map:

| Old tool | New equivalent |
|---|---|
| `generate_weather_file` | `generate_weather_file` (same name, same params) |
| `generate_design_day` | `generate_weather_file(format="ddy")` |
| `generate_ensemble` | `generate_weather_file(ssp=, year=, ensemble=True)` |
| `generate_batch` | `generate_weather_file(scenarios=[{...}, {...}])` |
| `get_station_epw(url)` | `analyze_weather(url=url)` for stats; `chart_weather(url=url)` for charts; or fetch the URL directly — `find_station` returns public `epw_url` fields anyone can `httpx.get()`. |
| `analyze_epw(url)` | `analyze_weather(url=url)` |
| `compare_scenarios([cfg1, cfg2])` | `analyze_weather(urls=[url1, url2])` — or `chart_weather(urls=[url1, url2], chart_type="comparison")` |
| `chart_diurnal_profile(url)` | `chart_weather(url=url, chart_type="diurnal")` |
| `chart_compare_scenarios(...)` | `chart_weather(urls=[...], chart_type="comparison")` |

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `EPWFORGE_API_KEY` | Bearer token (only needed for `generate_weather_file`) | unset |
| `EPWFORGE_BASE_URL` | Override the API host (mainly for testing) | `https://epwforge.com` |

## Behavior notes

- **File output:** `generate_weather_file` accepts `save_to` (single mode) or `save_to_dir` (ensemble / batch). When set, files write to disk and only paths/byte counts come back — keeps agent context lean. Without it, files return base64-encoded inline.
- **Compound events:** `events="heatwave,hothumid"` automatically blends humidity onto the heatwave. `events="coldsnap,coldwindy"` does the same for wind onto cold. Folded into the primary stitch — not stitched separately.
- **Event placement:** events anchor at the cell's hottest day (heat-family) or coldest day (cold-family) and are centered for the requested duration. The peak day's diurnal cycle is sustained across the event.
- **AR6 SSP auto-fill:** with an SSP scenario active, unspecified event intensities auto-fill from IPCC AR6 ensemble factors. Cold events stay at intensity 5 (no future amplification — recent obs don't support cold-side dampening). Pass `intensity_auto=False` to disable.
- **Anon morphing safety:** when `analyze_weather` or `chart_weather` is called with a `config`, the request routes to the hosted MCP at `epwforge.com/api/mcp`. The pipeline runs on EPWForge infra; the synthesized EPW never reaches the local client. Returns stats / SVG only.

## Development

```bash
git clone https://github.com/guzz-labs/epwforge-mcp
cd epwforge-mcp/python
uv sync
uv run epwforge-mcp
```

To test against a local API:

```bash
EPWFORGE_BASE_URL=http://localhost:3000 \
EPWFORGE_API_KEY=sk_live_... \
uv run epwforge-mcp
```

## Links

- **Website:** [epwforge.com](https://epwforge.com)
- **MCP landing page:** [epwforge.com/mcp](https://epwforge.com/mcp)
- **Pricing:** [epwforge.com/pricing](https://epwforge.com/pricing)
- **Maker:** [Guzzlabs](https://guzzlabs.com)
- **Issues:** [github.com/guzz-labs/epwforge-mcp/issues](https://github.com/guzz-labs/epwforge-mcp/issues)

## License

MIT
