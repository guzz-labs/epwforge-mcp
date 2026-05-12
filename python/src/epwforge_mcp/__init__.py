"""epwforge-mcp — MCP server for EPWForge weather file generation.

Exposes the EPWForge API as Model Context Protocol tools so AI agents
(Claude, Cursor, etc.) can generate, morph, and download EPW weather
files for building energy simulation.

Tools:
    generate_weather_file  — EPW for any location (TMY/AMY/SSP + UHI + events + smoke)
    generate_design_day    — DDY file with the same options
    generate_ensemble      — Per-model CMIP6 ensemble of morphed EPWs
    find_station           — Search available weather stations near a location

Authentication:
    Set EPWFORGE_API_KEY in your MCP client config.
    Generate an API key at https://epwforge.com/account.
"""

__version__ = "0.1.0"

from .server import main

__all__ = ["main", "__version__"]
