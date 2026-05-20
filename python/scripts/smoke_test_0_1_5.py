"""Smoke test for the 0.1.5 additions on the live EPWForge API.

Exercises:
  - find_station enrichment (agent_guidance + next_actions + synthesis_options)
  - get_station_epw (download + zip-extract a OneBuilding TMYx)
  - tmy_period param on generate_weather_file
  - weather_basis block in responses
  - chart_diurnal_profile (renders SVG from a downloaded EPW)
  - chart_compare_scenarios (renders SVG from a fake compare result)
  - --version / --help CLI flags

Costs ~3 generation credits (1 generate_weather_file + 1 baseline TMYx for
chart_diurnal_profile via reused EPW + zero for chart_compare_scenarios).
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def run_local_server(directory: Path, port: int) -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return httpd


def _short(d: dict, *, depth: int = 1) -> dict:
    """Truncate a dict so big base64 / svg payloads don't flood stdout."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = f"<{len(v)} chars>"
        elif isinstance(v, dict) and depth > 0:
            out[k] = _short(v, depth=depth - 1)
        else:
            out[k] = v
    return out


async def main() -> None:
    if not os.environ.get("EPWFORGE_API_KEY"):
        raise SystemExit("EPWFORGE_API_KEY not set")

    from epwforge_mcp import __version__
    from epwforge_mcp.client import EPWForgeClient
    from epwforge_mcp.server import (
        chart_compare_scenarios, chart_diurnal_profile, find_station,
        generate_weather_file, get_station_epw,
    )

    print(f"epwforge-mcp version: {__version__}\n")

    # ── Step 1: --version + --help CLI flags ──
    print("[1/6] CLI: --version + --help")
    v = subprocess.run(["uv", "run", "epwforge-mcp", "--version"], capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert v.returncode == 0 and v.stdout.strip() == __version__, f"--version: {v.stdout!r} {v.stderr!r}"
    h = subprocess.run(["uv", "run", "epwforge-mcp", "--help"], capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert h.returncode == 0 and "Usage:" in h.stdout, f"--help: {h.stdout!r}"
    print(f"      ✓ --version → {v.stdout.strip()}, --help OK\n")

    # ── Step 2: find_station enrichment ──
    print("[2/6] find_station(NYC) — check enrichment fields")
    fs = await find_station(lat=40.71, lon=-74.01, max_results=3)
    print(json.dumps(_short(fs, depth=2), indent=2)[:1500])
    assert "agent_guidance" in fs and "next_actions" in fs and "synthesis_options" in fs
    assert "OneBuilding" in fs["agent_guidance"]
    assert "use_a_station" in fs["next_actions"] and "synthesize_custom" in fs["next_actions"]
    assert "tmy_period_choices" in fs["synthesis_options"]
    print("      ✓ enrichment fields present\n")

    # ── Step 3: get_station_epw (uses URL from step 2) ──
    stations = fs.get("stations", [])
    if not stations or not stations[0].get("files"):
        raise SystemExit("No stations returned — can't test get_station_epw")
    first_url = stations[0]["files"][0]["url"]
    print(f"[3/6] get_station_epw('{first_url[:80]}...')")
    tmp = Path(tempfile.gettempdir()) / "epwforge_smoke_0_1_5"
    tmp.mkdir(parents=True, exist_ok=True)
    epw_dest = tmp / "station.epw"
    gs = await get_station_epw(url=first_url, save_to=str(epw_dest))
    print(json.dumps(_short(gs, depth=2), indent=2))
    assert gs["bytes_written"] > 100_000, f"file too small: {gs['bytes_written']}"
    assert gs["weather_basis"]["type"] == "published_onebuilding_tmy"
    assert epw_dest.exists()
    epw_text_bytes = epw_dest.read_bytes()
    assert epw_text_bytes[:8].decode("utf-8", "replace").startswith("LOCATION"), "saved file is not a valid EPW"
    print("      ✓ download + zip-extract + save OK\n")

    # ── Step 4: tmy_period + weather_basis ──
    print("[4/6] generate_weather_file(NYC, tmy_period='2007-2021') — verify weather_basis vintage")
    custom = await generate_weather_file(lat=40.71, lon=-74.01, tmy_period="2007-2021", save_to=str(tmp / "custom_2007.epw"))
    print(json.dumps(_short(custom, depth=2), indent=2))
    assert custom["weather_basis"]["vintage"] == "2007-2021"
    assert custom["weather_basis"]["type"] == "synthesized_tmyx"
    print("      ✓ weather_basis reflects requested vintage\n")

    # ── Step 5: chart_diurnal_profile (re-uses the downloaded station EPW) ──
    print("[5/6] chart_diurnal_profile — serve station.epw via localhost, render SVG")
    httpd = run_local_server(tmp, 8765)
    try:
        cd = await chart_diurnal_profile(url="http://127.0.0.1:8765/station.epw", save_to=str(tmp / "diurnal.svg"))
        print(json.dumps(_short(cd, depth=2), indent=2))
        assert cd["format"] == "svg"
        svg_bytes = (tmp / "diurnal.svg").read_text()
        assert svg_bytes.startswith("<svg") and "Diurnal" in svg_bytes
        assert "January" in svg_bytes and "July" in svg_bytes
        print(f"      ✓ SVG written, {len(svg_bytes):,} chars\n")
    finally:
        httpd.shutdown()

    # ── Step 6: chart_compare_scenarios (no API call — pure render) ──
    print("[6/6] chart_compare_scenarios — render bar chart from fake compare data")
    fake_baseline = {"cooling_db_F": 87.4, "heating_db_F": 16.5, "dewpoint_F": 73.4}
    fake_scenarios = [
        {"config": {"ssp": "ssp245", "year": 2050},
         "cooling_db_F": 91.4, "cooling_db_delta_F": 4.0,
         "heating_db_F": 21.2, "heating_db_delta_F": 4.7,
         "dewpoint_F": 77.2, "dewpoint_delta_F": 3.8},
        {"config": {"ssp": "ssp370", "year": 2090, "percentile": 90, "uhi": "urban"},
         "cooling_db_F": 101.6, "cooling_db_delta_F": 14.2,
         "heating_db_F": 26.8, "heating_db_delta_F": 10.3,
         "dewpoint_F": 88.2, "dewpoint_delta_F": 14.8},
    ]
    cc = await chart_compare_scenarios(baseline=fake_baseline, scenarios=fake_scenarios, save_to=str(tmp / "deltas.svg"))
    print(json.dumps(_short(cc, depth=2), indent=2))
    out_svg = (tmp / "deltas.svg").read_text()
    assert "<svg" in out_svg and "SSP370" in out_svg and "+14.2" in out_svg
    print(f"      ✓ SVG written, {len(out_svg):,} chars\n")

    print("All 0.1.5 smoke tests passed.")
    print(f"Artifacts in {tmp}:  {[p.name for p in tmp.iterdir()]}")


if __name__ == "__main__":
    asyncio.run(main())
