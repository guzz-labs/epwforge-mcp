"""Regression tests for server.py location-enrichment helpers.

Added 2026-06-09 in response to QC-review finding P0-1: the hosted morph
pipeline can return a named station with elevation_ft=0, and the pre-fix
_apply_station_to_location skipped the elevation backfill entirely when
the city was named (because the early `if not is_generic: return` ran
BEFORE the elevation check). These tests pin the post-fix behaviour:

  - elevation backfill always runs when missing / zero (regardless of city)
  - name backfill remains gated on is_generic
  - location_meta enriched_from annotation reflects which fields changed
  - no metadata pollution when nothing actually changed
"""
from __future__ import annotations

# _apply_station_to_location is defined as a *local* function inside another
# function in server.py — accessing it directly via import isn't possible.
# We exercise it by reaching into the closure via the module-level entry
# points that use it, or by re-importing the source. For a regression test
# the cleanest path is to re-implement the same function under test via
# textual extraction — but the simpler robust pattern is to use the public
# entry point (`_enrich_config_location`) with a mocked `_nearest_station`.
#
# Rather than monkey-patching, here we extract _apply_station_to_location
# into a top-level definition for direct testing. The fixture below pulls
# the function out by parsing server.py and exec'ing the def in isolation.
# This keeps the implementation as the single source of truth while still
# being independently testable.

import ast
from pathlib import Path


def _load_apply_station_to_location():
    """Pull the function body out of server.py and exec it in a fresh ns."""
    src = (Path(__file__).parent.parent / "src" / "epwforge_mcp" / "server.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_station_to_location":
            # Strip the leading indentation (it's nested inside another fn)
            fn_src = ast.get_source_segment(src, node)
            # Re-indent to module level
            lines = fn_src.split("\n")
            # Find leading whitespace of first line
            lead = len(lines[0]) - len(lines[0].lstrip())
            fn_src = "\n".join(line[lead:] if len(line) >= lead else line for line in lines)
            ns: dict = {"Any": object}
            # The function references `dict[str, Any]` in type hints — for
            # runtime exec we just need the names in scope.
            ns["dict"] = dict
            exec("from typing import Any\n" + fn_src, ns)
            return ns["_apply_station_to_location"]
    raise RuntimeError("_apply_station_to_location not found in server.py")


_apply_station_to_location = _load_apply_station_to_location()


# ── Test fixtures ───────────────────────────────────────────────────────────

NEAREST = {
    "city": "Boston Logan",
    "state": "MA",
    "country": "USA",
    "elevation_m": 5.0,   # → 16 ft
    "distance_km": 8.2,
}


def fresh_result(loc):
    return {"location": dict(loc)}


# ── 1. Named city + elevation_ft=0  →  elevation gets filled (the P0-1 bug)

def test_named_city_zero_elevation_gets_backfilled():
    r = fresh_result({"city": "Beverly Rgnl AP", "state": "MA", "country": "USA", "elevation_ft": 0})
    _apply_station_to_location(r, NEAREST)
    assert r["location"]["elevation_ft"] == 16, "P0-1 regression: elevation should backfill from nearest even when city is named"
    assert r["location"]["city"] == "Beverly Rgnl AP", "named city should not be overwritten"
    assert "elevation" in r["location_meta"]["enriched_from"]
    assert "city" not in r["location_meta"]["enriched_from"]


def test_named_city_missing_elevation_field_gets_backfilled():
    r = fresh_result({"city": "Beverly Rgnl AP", "state": "MA", "country": "USA"})  # no elevation_ft key
    _apply_station_to_location(r, NEAREST)
    assert r["location"]["elevation_ft"] == 16


# ── 2. Generic city + zero elevation  →  BOTH backfilled (legacy behaviour)

def test_generic_city_both_fields_backfilled():
    r = fresh_result({"city": "Custom", "state": "", "country": "Unknown", "elevation_ft": 0})
    _apply_station_to_location(r, NEAREST)
    assert r["location"]["city"] == "Boston Logan"
    assert r["location"]["state"] == "MA"
    assert r["location"]["country"] == "USA"
    assert r["location"]["elevation_ft"] == 16
    assert "city" in r["location_meta"]["enriched_from"]
    assert "elevation" in r["location_meta"]["enriched_from"]


# ── 3. Named city + correct elevation  →  no-op, no metadata pollution

def test_named_city_correct_elevation_is_noop():
    r = fresh_result({"city": "Beverly Rgnl AP", "state": "MA", "country": "USA", "elevation_ft": 33})
    _apply_station_to_location(r, NEAREST)
    assert r["location"]["elevation_ft"] == 33, "elevation already set must not be overwritten"
    assert r["location"]["city"] == "Beverly Rgnl AP"
    assert "location_meta" not in r, "no enrichment happened → no metadata annotation"


# ── 4. Generic city + correct elevation  →  only city backfilled

def test_generic_city_elevation_already_set():
    r = fresh_result({"city": "", "state": "", "country": "Unknown", "elevation_ft": 100})
    _apply_station_to_location(r, NEAREST)
    assert r["location"]["city"] == "Boston Logan"
    assert r["location"]["elevation_ft"] == 100, "elevation already set must not be overwritten"
    assert "city" in r["location_meta"]["enriched_from"]
    assert "elevation" not in r["location_meta"]["enriched_from"]


# ── 5. Nearest has no elevation_m  →  no fill attempted

def test_nearest_without_elevation_m():
    r = fresh_result({"city": "Beverly Rgnl AP", "state": "MA", "country": "USA", "elevation_ft": 0})
    nearest_no_elev = {k: v for k, v in NEAREST.items() if k != "elevation_m"}
    _apply_station_to_location(r, nearest_no_elev)
    assert r["location"]["elevation_ft"] == 0, "no elevation source → leave as-is"
    assert "location_meta" not in r
