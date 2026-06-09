"""Tests for the find_station `compact=True` helper.

Added 2026-06-09 per QC review P2-7: find_station response was returning
every vintage's full URL set per station, which inflates agent context
6-10× for a field most callers never use. The compactor picks the
newest TMYx and drops the rest.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _load_compact_station():
    src = (Path(__file__).parent.parent / "src" / "epwforge_mcp" / "server.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_compact_station":
            fn_src = ast.get_source_segment(src, node)
            ns: dict = {}
            exec("from typing import Any\n" + fn_src, ns)
            return ns["_compact_station"]
    raise RuntimeError("_compact_station not found")


_compact_station = _load_compact_station()


STATION = {
    "city": "Boston Logan",
    "state": "MA",
    "country": "USA",
    "lat": 42.36,
    "lon": -71.01,
    "distance_km": 0,
    "wmo": "725090",
    "files": [
        {"source": "TMYx", "period": "2009-2023", "epw_url": "https://x/a-2009-2023.epw", "ddy_url": "https://x/a-2009-2023.ddy"},
        {"source": "TMYx", "period": "2011-2025", "epw_url": "https://x/a-2011-2025.epw", "ddy_url": "https://x/a-2011-2025.ddy"},
        {"source": "TMY3", "period": "1976-2005", "epw_url": "https://x/a-tmy3.epw"},
        {"source": "CWEC2020", "period": "2020", "epw_url": "https://x/a-cwec.epw"},
    ],
}


def test_compact_picks_newest_tmyx():
    out = _compact_station(STATION)
    assert out["epw_url"] == "https://x/a-2011-2025.epw"
    assert out["ddy_url"] == "https://x/a-2011-2025.ddy"
    assert out["best_file_source"] == "TMYx"
    assert out["best_file_period"] == "2011-2025"
    assert out["files_omitted"] == 3


def test_compact_keeps_identifiers():
    out = _compact_station(STATION)
    for k in ("city", "state", "country", "lat", "lon", "distance_km", "wmo"):
        assert out[k] == STATION[k]
    assert "files" not in out


def test_compact_handles_station_without_files():
    bare = {"city": "Custom", "state": "", "country": "Unknown", "lat": 0, "lon": 0}
    out = _compact_station(bare)
    assert out == bare


def test_compact_prefers_tmyx_over_other_when_year_ties():
    s = {
        "city": "X",
        "files": [
            {"source": "TMY3", "period": "2020-2020", "epw_url": "https://x/tmy3.epw"},
            {"source": "TMYx", "period": "2020-2020", "epw_url": "https://x/tmyx.epw"},
        ],
    }
    out = _compact_station(s)
    assert out["epw_url"] == "https://x/tmyx.epw"
    assert out["best_file_source"] == "TMYx"


def test_compact_handles_unparseable_period():
    s = {
        "city": "X",
        "files": [
            {"source": "TMYx", "period": "full", "epw_url": "https://x/full.epw"},
            {"source": "TMYx", "period": "2011-2025", "epw_url": "https://x/dated.epw"},
        ],
    }
    out = _compact_station(s)
    assert out["epw_url"] == "https://x/dated.epw"  # parseable wins
