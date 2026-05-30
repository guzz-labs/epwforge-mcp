"""epwforge-mcp — MCP server for EPWForge weather file generation.

Exposes the EPWForge API as Model Context Protocol tools so AI agents
(Claude, Cursor, etc.) can find weather stations, analyze EPWs, render
charts, and generate / morph weather files for building energy simulation.

v0.2.0 — 4-tool consolidation. Read tools work WITHOUT an API key so
prospects can explore EPWForge from Claude without signing up.

Tools:
    find_station          (no auth)  Search GuzzStations catalog (17k+ stations)
    analyze_weather       (no auth)  Stats from EPW URL or synthesized config
    chart_weather         (no auth)  Diurnal / comparison SVG from URL or config
    generate_weather_file (auth)     Delivers EPW/DDY; charges credits

The 3 read tools work for anyone — no signup needed. Only
generate_weather_file requires an EPWFORGE_API_KEY (free at
https://epwforge.com/account; signup includes 5 welcome credits).

CLI:
    epwforge-mcp                  Run the stdio MCP server (default)
    epwforge-mcp install ...      One-command setup for Claude Desktop /
                                  Claude Code / Cursor
    epwforge-mcp --version        Print package version and exit
"""

__version__ = "0.8.0"


_HELP_TEXT = """\
epwforge-mcp — MCP server for the EPWForge weather-file API.

v0.2.0 — 4-tool consolidation. find_station, analyze_weather, and
chart_weather work without authentication. Only generate_weather_file
requires EPWFORGE_API_KEY.

Usage:
  epwforge-mcp                              Run the stdio MCP server (default)
  epwforge-mcp install --api-key sk_live_…  Auto-configure Claude Desktop /
                                            Claude Code / Cursor with this server
  epwforge-mcp --version                    Print package version and exit
  epwforge-mcp --help                       Print this help and exit

Environment variables:
  EPWFORGE_API_KEY    Bearer token (only needed for generate_weather_file)
  EPWFORGE_BASE_URL   Override the API host (default https://epwforge.com)

Generate an API key — free, any tier — at https://epwforge.com/account.
"""


def main() -> None:
    """Top-level CLI entry — dispatches between server (default), `install`,
    and informational flags (--version / --help / -h).
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
