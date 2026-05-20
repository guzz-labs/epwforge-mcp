# epwforge-mcp

> MCP server for [EPWForge](https://epwforge.com) — give Claude, Cursor, and other AI agents the ability to generate, morph, and download weather files for building energy simulation.

**Status:** Reserved name. The public MCP server is in active development. Star this repo or visit [epwforge.com](https://epwforge.com) for launch updates.

## What is EPWForge?

EPWForge generates, morphs, and serves weather files (`.epw`, `.tmy`, `.amy`) for building energy simulation tools — EnergyPlus, OpenStudio, IES VE, eQUEST, and any workflow that consumes EPW. The platform supports:

- **TMYx generation anywhere** — typical meteorological years synthesized from ERA5 reanalysis for any global lat/lon, not just airport stations
- **Climate morphing** — apply CMIP6 future-scenario deltas (SSP1-2.6, SSP2-4.5, SSP3-7.0) to baseline weather files for resilience and adaptation studies. SSP5-8.5 was deprecated per CMIP7 (deemed implausible) — use SSP3-7.0 as the high-end scenario.
- **AMY (Actual Meteorological Year)** — historical hourly weather for hindcasting, calibration, and post-occupancy analysis
- **Wildfire smoke overlays** — CAMS aerosol optical depth integrated into solar radiation channels
- **Programmatic API + UI** — pull files via REST or browse the globe interface at [epwforge.com](https://epwforge.com)

## What this MCP does

This package will expose EPWForge as Model Context Protocol tools so AI agents can request, generate, and morph weather files directly. Planned tools include:

| Tool | Purpose |
|---|---|
| `search_locations` | Find cities or coordinates with available data |
| `get_location_metadata` | Climate zone, available years, data sources |
| `generate_tmyx` | Synthesize a typical meteorological year for any lat/lon |
| `get_amy` | Pull an actual meteorological year for a specific year |
| `morph_to_future` | Apply CMIP6 climate deltas to a baseline file |
| `apply_smoke_overlay` | Layer wildfire smoke (AOD) onto solar channels |
| `download_epw` | Retrieve a generated file as bytes or a signed URL |
| `get_quota` | Check remaining tier allowance |

Authentication is by EPWForge API key (set via the `EPWFORGE_API_KEY` environment variable) — the same key that authorizes the REST API.

## Install (placeholder)

```bash
# npm
npm install -g epwforge-mcp

# pip
pip install epwforge-mcp
```

These commands install the stub today. The functional MCP server will ship on the same package names.

## Connecting to Claude

Once published, configure Claude Desktop or Claude Code by adding to your MCP config:

```json
{
  "mcpServers": {
    "epwforge": {
      "command": "npx",
      "args": ["epwforge-mcp"],
      "env": { "EPWFORGE_API_KEY": "your-key-here" }
    }
  }
}
```

Get your API key at [epwforge.com](https://epwforge.com).

## Links

- **Website:** [epwforge.com](https://epwforge.com)
- **Maker:** [Guzzlabs](https://guzzlabs.com)
- **Issues:** [github.com/guzz-labs/epwforge-mcp/issues](https://github.com/guzz-labs/epwforge-mcp/issues)

## License

MIT
