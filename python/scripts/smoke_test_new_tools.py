"""Smoke test for `analyze_epw` and `compare_scenarios`.

Exercises the new tools end-to-end against the real EPWForge API.

Usage:
    EPWFORGE_API_KEY=sk_live_... uv run python scripts/smoke_test_new_tools.py

Steps:
  1. Generate a TMYx for NYC via the existing client (1 credit).
  2. Save it to /tmp and serve via http.server.
  3. Call analyze_epw with the localhost URL → verify summary stats.
  4. Call compare_scenarios with 2 small configs + baseline_url from step 2
     → verify deltas come back. (2 credits.)
  5. Total cost: 3 credits.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import os
import socketserver
import threading
import time
from pathlib import Path


def run_local_server(directory: Path, port: int) -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # Tiny pause so the bind completes before we hit it.
    time.sleep(0.2)
    return httpd


async def main() -> None:
    if not os.environ.get("EPWFORGE_API_KEY"):
        raise SystemExit("EPWFORGE_API_KEY not set")

    from epwforge_mcp.client import EPWForgeClient
    from epwforge_mcp.server import analyze_epw, compare_scenarios

    tmp = Path("/tmp/epwforge_smoke")
    tmp.mkdir(parents=True, exist_ok=True)
    epw_path = tmp / "nyc_tmyx.epw"

    # ── Step 1: generate a TMYx via the client directly ──
    print("[1/4] Generating TMYx for NYC (40.71, -74.01) …")
    client = EPWForgeClient()
    data = await client.get_json(
        "/api/epwforge",
        {"lat": 40.71, "lon": -74.01, "basis": "tmy", "format": "json"},
    )
    b64 = data["epw_base64"]
    epw_path.write_bytes(base64.b64decode(b64))
    print(f"      saved → {epw_path}  ({epw_path.stat().st_size:,} bytes)")

    # ── Step 2: serve over localhost ──
    port = 8765
    print(f"[2/4] Serving {tmp} on http://127.0.0.1:{port}/ …")
    httpd = run_local_server(tmp, port)
    url = f"http://127.0.0.1:{port}/nyc_tmyx.epw"

    try:
        # ── Step 3: analyze_epw ──
        print(f"[3/4] analyze_epw(url='{url}') …")
        summary = await analyze_epw(url=url)
        print(json.dumps(summary, indent=2))
        # Quick sanity bounds — NYC should be roughly here.
        assert 35 < summary["annual_mean_temp_F"] < 65, summary["annual_mean_temp_F"]
        assert 80 < summary["cooling_design_db_F"] < 105, summary["cooling_design_db_F"]
        assert -5 < summary["heating_design_db_F"] < 25, summary["heating_design_db_F"]
        assert summary["hdd_65_annual"] > summary["cdd_65_annual"], "NYC: HDD should exceed CDD"
        assert summary["n_hours"] == 8760, summary["n_hours"]
        assert len(summary["monthly_mean_temp_F"]) == 12
        print("      ✓ analyze_epw sanity checks passed")

        # ── Step 4: compare_scenarios ──
        configs = [
            {"lat": 40.71, "lon": -74.01, "ssp": "ssp245", "year": 2050, "percentile": 50},
            {"lat": 40.71, "lon": -74.01, "ssp": "ssp585", "year": 2090, "percentile": 90},
        ]
        print(f"[4/4] compare_scenarios(configs=<{len(configs)}>, baseline_url=<localhost EPW>) …")
        comp = await compare_scenarios(configs=configs, baseline_url=url)
        print(json.dumps(comp, indent=2))
        # Sanity: warming scenarios should have positive cooling deltas.
        for s in comp["scenarios"]:
            assert s["cooling_db_delta_F"] >= -2.0, f"unexpected cooling delta: {s}"
        print("      ✓ compare_scenarios sanity checks passed")

    finally:
        httpd.shutdown()
        await client.aclose()

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
