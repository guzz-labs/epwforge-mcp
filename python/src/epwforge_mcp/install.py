"""`epwforge-mcp install` — one-command setup for popular MCP clients.

Auto-detects installed clients (Claude Desktop, Claude Code, Cursor, Cline)
and merges an `epwforge` entry into each config file without clobbering
other MCP servers the user has already configured.

Usage:
    uvx epwforge-mcp install --api-key sk_live_...
    uvx epwforge-mcp install --api-key sk_live_... --client claude-desktop
    uvx epwforge-mcp install --api-key sk_live_... --base-url https://staging.epwforge.com
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClientTarget:
    name: str         # human label
    slug: str         # CLI selector
    path: Path        # absolute path to JSON config
    key: str          # top-level key under which mcpServers lives ("mcpServers" for all current clients)


def _claude_desktop_path() -> Path:
    """Path to Claude Desktop config on the current OS."""
    sysname = platform.system()
    if sysname == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sysname == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude/claude_desktop_config.json"
    # Linux — Claude Desktop isn't officially supported, but config landing here is the convention
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def detect_clients() -> list[ClientTarget]:
    """Return every MCP-aware client whose config file exists on this machine."""
    candidates = [
        ClientTarget("Claude Desktop", "claude-desktop", _claude_desktop_path(), "mcpServers"),
        ClientTarget("Claude Code", "claude-code", Path.home() / ".claude.json", "mcpServers"),
        ClientTarget("Cursor", "cursor", Path.home() / ".cursor/mcp.json", "mcpServers"),
    ]
    return [c for c in candidates if c.path.exists() or c.path.parent.exists()]


def _epwforge_server_block(api_key: str, base_url: str | None) -> dict:
    env = {"EPWFORGE_API_KEY": api_key}
    if base_url:
        env["EPWFORGE_BASE_URL"] = base_url
    return {
        "command": "uvx",
        "args": ["epwforge-mcp"],
        "env": env,
    }


def _merge_into_config(path: Path, api_key: str, base_url: str | None, server_key: str) -> str:
    """Merge an epwforge entry into the given client config file. Returns a status string."""
    # Read existing config (or start fresh if missing). Tolerate empty files.
    data: dict = {}
    existed = path.exists()
    if existed and path.stat().st_size > 0:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return f"SKIP: {path} is not valid JSON ({e}). Refusing to overwrite."

    servers = data.setdefault(server_key, {})
    if not isinstance(servers, dict):
        return f"SKIP: {path} has a non-object `{server_key}` field; refusing to overwrite."

    was_present = "epwforge" in servers
    servers["epwforge"] = _epwforge_server_block(api_key, base_url)

    # Make parent directory if needed (Claude Code's ~/.claude.json parent may not exist on a fresh machine).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if not existed:
        return f"CREATED: {path}"
    return f"{'UPDATED' if was_present else 'ADDED'}: {path}"


def install(api_key: str, base_url: str | None = None, client_slug: str | None = None) -> int:
    """Run the install. Returns a process exit code."""
    if not api_key or not api_key.startswith("sk_"):
        print(
            "epwforge-mcp install: --api-key must be your EPWForge API key "
            "(starts with `sk_`). Generate one at https://epwforge.com/account.",
            file=sys.stderr,
        )
        return 2

    detected = detect_clients()
    if client_slug:
        detected = [c for c in detected if c.slug == client_slug]
        if not detected:
            print(
                f"epwforge-mcp install: --client {client_slug!r} not found on this machine. "
                f"Known slugs: claude-desktop, claude-code, cursor.",
                file=sys.stderr,
            )
            return 1

    if not detected:
        print(
            "epwforge-mcp install: no supported MCP clients detected (looked for Claude Desktop, "
            "Claude Code, Cursor). Open one of them at least once so its config directory exists, "
            "then re-run.",
            file=sys.stderr,
        )
        return 1

    print("epwforge-mcp install: writing to detected MCP clients\n")
    for target in detected:
        status = _merge_into_config(target.path, api_key, base_url, target.key)
        print(f"  {target.name:<16} {status}")
    print()
    print("Done. Restart the client(s) above for the change to take effect.")
    print("Then ask the assistant something like:")
    print('  > "Generate a TMYx weather file for Lisbon, save it to ~/Downloads."')
    return 0


def main_entry(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="epwforge-mcp install",
        description="Install the EPWForge MCP server into popular MCP clients.",
    )
    parser.add_argument("--api-key", required=False, help="EPWForge API key (sk_live_...). If omitted, reads EPWFORGE_API_KEY env var.")
    parser.add_argument("--base-url", help="Override the EPWForge API host (mainly for testing).")
    parser.add_argument(
        "--client",
        help="Install only into a specific client (claude-desktop | claude-code | cursor). Default: every detected client.",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("EPWFORGE_API_KEY", "")
    return install(api_key=api_key, base_url=args.base_url, client_slug=args.client)
