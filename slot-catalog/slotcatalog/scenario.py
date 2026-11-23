"""Scenario runner: executes declarative JSON scenarios against a Catalog.

A scenario is a deterministic, re-runnable acceptance document:

    {
      "format": "slot-catalog-scenario/1",
      "name": "example",
      "steps": [
        {"batch":  {"batch_id": "SETUP", "ops": [ ... ]}},
        {"publish": "V1"},
        {"query_slot": {"version": "V1", "booklet": "B1", "slot": 0}},
        {"search_export": {"booklet": "B1"}}
      ]
    }

Every step produces one result object; failing batches/queries are captured
as ``{"ok": false, "error": {...}}`` results so scenarios can assert on
rejections too.  The runner never stops at the first failure.
"""

from __future__ import annotations

from .catalog import Catalog
from .errors import BatchError, CatalogError, InvalidOp

SCENARIO_FORMAT = "slot-catalog-scenario/1"
RESULTS_FORMAT = "slot-catalog-results/1"


def run_scenario(scenario, catalog=None):
    """Run every step of ``scenario`` and return a results document."""
    if not isinstance(scenario, dict):
        raise InvalidOp("scenario must be an object")
    steps = scenario.get("steps")
    if not isinstance(steps, list):
        raise InvalidOp("scenario needs a 'steps' list")
    cat = catalog if catalog is not None else Catalog()
    results = [_run_step(cat, step) for step in steps]
    return {"format": RESULTS_FORMAT, "name": scenario.get("name"), "results": results}


def _run_step(cat, step):
    if not isinstance(step, dict) or len(step) != 1:
        raise InvalidOp("each step must be an object with exactly one key")
    (kind, payload), = step.items()
    if kind == "batch":
        if isinstance(payload, list):
            payload = {"ops": payload}
        try:
            result = cat.apply_batch(
                payload.get("ops", []), batch_id=payload.get("batch_id")
            )
        except BatchError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        return {"step": "batch", "result": result}
    if kind == "publish":
        version = payload if isinstance(payload, str) else (payload or {}).get("version")
        op = {"op": "publish"}
        if version is not None:
            op["version"] = version
        try:
            result = cat.apply_batch([op])
        except BatchError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        return {"step": "publish", "result": result}
    if kind == "query_slot":
        payload = payload or {}
        try:
            result = cat.query_slot(
                payload.get("version"), payload.get("booklet"), payload.get("slot")
            )
        except CatalogError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        return {"step": "query_slot", "result": result}
    if kind == "search_export":
        payload = payload or {}
        try:
            result = cat.search_export(payload.get("booklet"))
        except CatalogError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        return {"step": "search_export", "result": result}
    raise InvalidOp("unknown step %r" % (kind,))
