#!/usr/bin/env python3
"""Generate 04_long_suffix.scenario.json (deterministic, no randomness).

Builds one booklet with a 2000-part replacement chain p0 -> p1 -> ... ->
p1999 plus 1000 fan-in entrances q_i -> p0, then exercises: one edge flip at
the tail (every upstream entrance re-points, printed history untouched) and
one mid-chain cut (only the upstream side re-points).
"""

import json
from pathlib import Path

CHAIN = 2000
FAN = 1000


def main():
    here = Path(__file__).resolve().parent
    build = [{"op": "add_booklet", "booklet": "BIG", "slot_width": 4}]
    for i in range(CHAIN):
        build.append(
            {
                "op": "register",
                "booklet": "BIG",
                "part": "p%d" % i,
                "body": "body-p%d" % i,
                "approved": True,
            }
        )
    for i in range(FAN):
        build.append(
            {
                "op": "register",
                "booklet": "BIG",
                "part": "q%d" % i,
                "body": "body-q%d" % i,
                "approved": True,
            }
        )
    build.append(
        {"op": "register", "booklet": "BIG", "part": "Z", "body": "body-Z", "approved": True}
    )
    # slot assignment is deterministic: p{i}->i, q{i}->CHAIN+i, Z->CHAIN+FAN
    edges = [
        {"op": "replace", "booklet": "BIG", "old": "p%d" % i, "new": "p%d" % (i + 1)}
        for i in range(CHAIN - 1)
    ]
    edges += [
        {"op": "replace", "booklet": "BIG", "old": "q%d" % i, "new": "p0"}
        for i in range(FAN)
    ]
    mid = CHAIN // 2
    steps = [
        {"batch": {"batch_id": "BUILD", "ops": build}},
        {"publish": "V1"},
        {"batch": {"batch_id": "CHAIN", "ops": edges}},
        # reader holds V1 (all slots printed); entrances resolve through live edges
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": 0}},
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": CHAIN + 7}},
        # flip the tail edge: thousands of entrances share the new suffix
        {
            "batch": {
                "batch_id": "FLIP-TAIL",
                "ops": [
                    {
                        "op": "replace",
                        "booklet": "BIG",
                        "old": "p%d" % (CHAIN - 1),
                        "new": "Z",
                    }
                ],
            }
        },
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": 0}},
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": CHAIN + 7}},
        # cut the chain in the middle: upstream entrances end at the cut point
        {
            "batch": {
                "batch_id": "CUT-MID",
                "ops": [{"op": "unreplace", "booklet": "BIG", "old": "p%d" % mid}],
            }
        },
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": 3}},
        {"query_slot": {"version": "V1", "booklet": "BIG", "slot": mid + 1}},
        # only the two qualified endpoints remain visible, each exactly once
        {"search_export": {"booklet": "BIG"}},
    ]
    doc = {"format": "slot-catalog-scenario/1", "name": "04-long-suffix", "steps": steps}
    out = here / "04_long_suffix.scenario.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote %s (%d steps, %d parts)" % (out.name, len(steps), CHAIN + FAN + 1))


if __name__ == "__main__":
    main()
