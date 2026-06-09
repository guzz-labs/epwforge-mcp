#!/usr/bin/env python3
"""Assert that the package version is identical across every place it's encoded.

When this fails: the version was bumped somewhere but not everywhere. The
MCP Registry reads server.json, PyPI reads pyproject.toml, the npm placeholder
reads npm/package.json, humans read the README badge. Drift between these
caused QC-review finding P1-3 on 2026-06-09 (registry showed 0.4.0 while
PyPI shipped 0.9.0).

Usage:
  python3 scripts/check-versions.py            # prints all, exits 0 / 1
  python3 scripts/check-versions.py --set X.Y  # rewrite ALL files to X.Y

Exit codes:
  0  all five versions agree
  1  drift detected (writes diff to stderr)
  2  could not read one of the files
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def read_pyproject() -> str | None:
    p = ROOT / "python" / "pyproject.toml"
    if not p.exists(): return None
    for line in p.read_text().splitlines():
        m = re.match(r'^version\s*=\s*"([^"]+)"\s*$', line)
        if m: return m.group(1)
    return None


def read_init() -> str | None:
    p = ROOT / "python" / "src" / "epwforge_mcp" / "__init__.py"
    if not p.exists(): return None
    for line in p.read_text().splitlines():
        m = re.match(r'^__version__\s*=\s*"([^"]+)"\s*$', line)
        if m: return m.group(1)
    return None


def read_server_json() -> tuple[str | None, str | None]:
    """Returns (top_level_version, packages[0].version) — both must match."""
    p = ROOT / "python" / "server.json"
    if not p.exists(): return (None, None)
    d = json.loads(p.read_text())
    pkg_version = None
    pkgs = d.get("packages") or []
    if pkgs and isinstance(pkgs, list):
        pkg_version = pkgs[0].get("version")
    return (d.get("version"), pkg_version)


def read_npm_package() -> str | None:
    p = ROOT / "npm" / "package.json"
    if not p.exists(): return None
    return json.loads(p.read_text()).get("version")


_README_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(\d+\.\d+\.\d+)", re.MULTILINE)


def read_readme() -> str | None:
    p = ROOT / "README.md"
    if not p.exists(): return None
    for line in p.read_text().splitlines():
        m = _README_STATUS_RE.match(line.strip())
        if m: return m.group(1)
    return None


SOURCES = [
    ("python/pyproject.toml",              read_pyproject),
    ("python/src/.../__init__.py",         read_init),
    ("python/server.json (top)",           lambda: read_server_json()[0]),
    ("python/server.json (packages[0])",   lambda: read_server_json()[1]),
    ("npm/package.json",                   read_npm_package),
    ("README.md Status line",              read_readme),
]


def collect() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for label, fn in SOURCES:
        try:
            out[label] = fn()
        except Exception as e:
            out[label] = f"ERROR: {e}"
    return out


def write_all(target: str) -> None:
    """Rewrite every file to `target`. Used by --set."""
    # pyproject
    p = ROOT / "python" / "pyproject.toml"
    p.write_text(re.sub(r'^(version\s*=\s*)"[^"]+"', rf'\1"{target}"', p.read_text(), flags=re.M))

    # __init__.py
    p = ROOT / "python" / "src" / "epwforge_mcp" / "__init__.py"
    p.write_text(re.sub(r'^(__version__\s*=\s*)"[^"]+"', rf'\1"{target}"', p.read_text(), flags=re.M))

    # server.json (both occurrences)
    p = ROOT / "python" / "server.json"
    d = json.loads(p.read_text())
    d["version"] = target
    if d.get("packages") and isinstance(d["packages"], list):
        for pkg in d["packages"]:
            if isinstance(pkg, dict): pkg["version"] = target
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

    # npm/package.json
    p = ROOT / "npm" / "package.json"
    d = json.loads(p.read_text())
    d["version"] = target
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

    # README Status line
    p = ROOT / "README.md"
    p.write_text(_README_STATUS_RE.sub(f"**Status:** {target}", p.read_text()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="set_version",
                    help="Rewrite all files to this version (e.g., 0.9.1).")
    args = ap.parse_args()

    if args.set_version:
        write_all(args.set_version)
        print(f"Set all to {args.set_version}.")
        return 0

    versions = collect()
    print(f"{'source':40s}  version")
    print("-" * 60)
    for k, v in versions.items():
        print(f"  {k:38s}  {v}")

    unique = set(v for v in versions.values() if v is not None and not (isinstance(v, str) and v.startswith("ERROR")))
    if any(v is None or (isinstance(v, str) and v.startswith("ERROR")) for v in versions.values()):
        print("\nFAIL: at least one source could not be read.", file=sys.stderr)
        return 2
    if len(unique) > 1:
        print(f"\nFAIL: {len(unique)} distinct versions in use → {sorted(unique)}", file=sys.stderr)
        return 1
    print(f"\nOK: all sources at {unique.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
