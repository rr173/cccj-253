#!/usr/bin/env python3
"""Regenerate the merge example inputs and goldens (deterministic).

Builds the shared base state, two branch files pinning its SHA-256, and the
expected merge report / merged state, all next to this script.  Everything
is derived deterministically, so re-running this script after editing the
scenario below refreshes every file consistently (the branch files embed
the hash of the exact base.json bytes).

    python3 acceptance/merge_example/gen_example.py
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from slotcatalog import Catalog  # noqa: E402
from slotcatalog.merge import BRANCH_FORMAT, merge_branches  # noqa: E402

HERE = Path(__file__).resolve().parent


def dump(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def build_base():
    """The shared paper-catalog base the two teams started from."""
    cat = Catalog()
    cat.apply_batch(
        [
            {"op": "add_booklet", "booklet": "B1", "slot_width": 3},
            {"op": "reserve", "booklet": "B1", "slots": [100, 101]},
            {"op": "register", "booklet": "B1", "part": "P-001",
             "title": "件一", "body": "件一正文", "approved": True},
            {"op": "register", "booklet": "B1", "part": "P-002",
             "title": "件二", "body": "件二正文", "approved": True},
            {"op": "register", "booklet": "B1", "part": "P-003",
             "title": "件三", "body": "件三正文"},
            {"op": "publish", "version": "V1"},
        ],
        batch_id="SETUP",
    )
    cat.apply_batch(
        [
            {"op": "amend", "booklet": "B1", "part": "P-001",
             "attachment": "底稿补写一"},
            {"op": "replace", "booklet": "B1", "old": "P-002", "new": "P-003"},
        ],
        batch_id="BASE-2",
    )
    return cat.save()


# Team alpha: a new implicit part, an amendment, a review flip, an explicit
# reservation claimed by a second new part, and a new replacement edge.
ALPHA_BATCHES = [
    {
        "batch_id": "JIA-1",
        "ops": [
            {"op": "register", "booklet": "B1", "part": "P-101",
             "title": "甲新件一", "body": "甲新件一正文", "approved": True},
            {"op": "amend", "booklet": "B1", "part": "P-001",
             "attachment": "甲补写一"},
            {"op": "set_review", "booklet": "B1", "part": "P-003",
             "approved": True},
        ],
    },
    {
        "batch_id": "JIA-2",
        "ops": [
            {"op": "reserve", "booklet": "B1", "slots": [200]},
            {"op": "register", "booklet": "B1", "part": "P-102", "slot": 200,
             "title": "甲新件二", "body": "甲新件二正文", "approved": True},
            {"op": "replace", "booklet": "B1", "old": "P-001", "new": "P-101"},
        ],
    },
]

# Team beta: two implicit parts (the first collides with alpha's and must
# move), two amendments, a withdrawal, and a retargeted replacement edge.
BETA_BATCHES = [
    {
        "batch_id": "YI-1",
        "ops": [
            {"op": "register", "booklet": "B1", "part": "P-201",
             "title": "乙新件一", "body": "乙新件一正文", "approved": True},
            {"op": "register", "booklet": "B1", "part": "P-202",
             "title": "乙新件二", "body": "乙新件二正文", "approved": True},
            {"op": "amend", "booklet": "B1", "part": "P-001",
             "attachment": "乙补写一"},
            {"op": "amend", "booklet": "B1", "part": "P-001",
             "attachment": "乙补写二"},
        ],
    },
    {
        "batch_id": "YI-2",
        "ops": [
            {"op": "withdraw", "booklet": "B1", "part": "P-002"},
            {"op": "replace", "booklet": "B1", "old": "P-002", "new": "P-201"},
        ],
    },
]


def branch_doc(name, digest, batches):
    return {
        "format": BRANCH_FORMAT,
        "branch": name,
        "base_sha256": digest,
        "batches": batches,
    }


def main():
    base_raw = dump(build_base()).encode("utf-8")
    digest = hashlib.sha256(base_raw).hexdigest()
    alpha = branch_doc("alpha", digest, ALPHA_BATCHES)
    beta = branch_doc("beta", digest, BETA_BATCHES)
    state, report = merge_branches(base_raw, [alpha, beta])
    (HERE / "base.json").write_bytes(base_raw)
    (HERE / "branch-alpha.json").write_text(dump(alpha), encoding="utf-8")
    (HERE / "branch-beta.json").write_text(dump(beta), encoding="utf-8")
    (HERE / "expected.report.json").write_text(dump(report), encoding="utf-8")
    (HERE / "expected.state.json").write_text(dump(state), encoding="utf-8")
    print("regenerated merge example in %s" % HERE)
    print("base sha256: %s" % digest)
    print("moves: %s" % json.dumps(report["moves"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
