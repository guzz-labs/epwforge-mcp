"""epwforge-mcp — MCP server for EPWForge weather file generation.

Exposes the EPWForge API as Model Context Protocol tools so AI agents
(Claude, Cursor, etc.) can generate, morph, and download EPW weather
files for building energy simulation.

Tools:
    generate_weather_file       — Synthesize EPW from ERA5 (TMY/AMY/SSP + UHI + events + smoke)
    generate_design_day         — DDY file with the same options
    generate_ensemble           — Per-model CMIP6 ensemble of morphed EPWs
    generate_batch              — Generate up to 10 EPWs in parallel into a save_to_dir
    get_station_epw             — Download a published OneBuilding/GuzzStation TMY file
    find_station                — Search OneBuilding/GuzzStation library near a location
    analyze_epw                 — Download an EPW URL and summarize design conditions, DD, GHI
    compare_scenarios           — Sensitivity sweep over multiple configs, returns only deltas
    chart_diurnal_profile       — SVG chart: monthly Max/Avg/Min hourly temperature
    chart_compare_scenarios     — SVG chart: bar chart of design-condition deltas

Authentication:
    Set EPWFORGE_API_KEY in your MCP client config.
    Generate an API key at https://epwforge.com/account.

CLI:
    epwforge-mcp                  — run the stdio MCP server (default)
    epwforge-mcp install ...      — one-command setup for Claude Desktop /
                                    Claude Code / Cursor (writes the MCP
                                    config block for the user)
"""

__version__ = "0.1.6"


_HELP_TEXT = """\
epwforge-mcp — MCP server for the EPWForge weather-file API.

Usage:
  epwforge-mcp                              Run the stdio MCP server (default)
  epwforge-mcp install --api-key sk_live_…  Auto-configure Claude Desktop /
                                            Claude Code / Cursor with this server
  epwforge-mcp --version                    Print package version and exit
  epwforge-mcp --help                       Print this help and exit

Environment variables (when running the server):
  EPWFORGE_API_KEY    Bearer token for the EPWForge API (required)
  EPWFORGE_BASE_URL   Override the API host (default https://epwforge.com)

Generate an API key at https://epwforge.com/account.
"""


def main() -> None:
    """Top-level CLI entry — dispatches between server (default), `install`,
    and informational flags (--version / --help / -h).

    Informational flags are handled BEFORE the server dispatch so they don't
    require EPWFORGE_API_KEY to be set (the server-side validator would
    otherwise reject `epwforge-mcp --version` with a confusing error).
    """
    import sys
    args = sys.argv[1:]
    if args and args[0] in {"--version", "-V"}:
        print(__version__)
        sys.exit(0)
    if args and args[0] in {"--help", "-h"}:
        print(_HELP_TEXT)
        sys.exit(0)
    if args and args[0] == "install":
        from .install import main_entry as install_main
        sys.exit(install_main(args[1:]))
    from .server import main as server_main
    server_main()


__all__ = ["main", "__version__"]
