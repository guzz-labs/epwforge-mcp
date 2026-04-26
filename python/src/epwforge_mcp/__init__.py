"""epwforge-mcp: MCP server for EPWForge weather file generation and morphing.

This package name is reserved. The functional MCP server is in development.
Visit https://epwforge.com for launch updates.
"""

__version__ = "0.0.1"


def main() -> None:
    import sys
    print(
        "epwforge-mcp: this package name is reserved. "
        "The functional MCP server is in development.\n"
        "Visit https://epwforge.com for launch updates.",
        file=sys.stderr,
    )
    sys.exit(0)
