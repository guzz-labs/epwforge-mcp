"""epwforge-mcp — MCP server for EPWForge weather file generation.

Exposes the EPWForge API as Model Context Protocol tools so AI agents
(Claude, Cursor, etc.) can generate, morph, and download EPW weather
files for building energy simulation.

Tools:
    generate_weather_file  — EPW for any location (TMY/AMY/SSP + UHI + events + smoke)
    generate_design_day    — DDY file with the same options
    generate_ensemble      — Per-model CMIP6 ensemble of morphed EPWs
    find_station           — Search available weather stations near a location
    analyze_epw            — Download an EPW URL and summarize design conditions, DD, GHI
    compare_scenarios      — Sensitivity sweep over multiple configs, returns only deltas

Authentication:
    Set EPWFORGE_API_KEY in your MCP client config.
    Generate an API key at https://epwforge.com/account.

CLI:
    epwforge-mcp                  — run the stdio MCP server (default)
    epwforge-mcp install ...      — one-command setup for Claude Desktop /
                                    Claude Code / Cursor (writes the MCP
                                    config block for the user)
"""

__version__ = "0.1.4"


def main() -> None:
    """Top-level CLI entry — dispatches between server (default) and `install`."""
    import sys
    args = sys.argv[1:]
    if args and args[0] == "install":
        from .install import main_entry as install_main
        sys.exit(install_main(args[1:]))
    from .server import main as server_main
    server_main()


__all__ = ["main", "__version__"]
