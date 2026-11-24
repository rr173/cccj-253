"""Offline merge (离线合稿) of independently cataloged branch files.

Several cataloging teams may register corrections offline against the same
base state.  Each branch file (``slot-catalog-branch/1``) names the branch,
pins the base file by SHA-256 over its original bytes, and lists the batches
the team applied, in order::

    {"format": "slot-catalog-branch/1",
     "branch": "alpha",
     "base_sha256": "<sha256 of the base state file bytes>",
     "batches": [{"batch_id": "A-1", "ops": [ ... ]}, ...]}

:func:`merge_branches` replays every branch against the base on its own,
keeping the original batch boundaries (a branch that does not apply cleanly,
or that leans on another branch's parts, is rejected), then folds the branch
final states into one merged catalog state:

* base parts keep their slots; base tombstones keep occupying theirs; base
  reservations may only be claimed by explicit-slot registrations;
* only register / amend / set_review / withdraw / replace / unreplace and
  slot-explicit reserves are merged -- publish, add_booklet and count-based
  reserves are rejected as unsupported;
* two branches registering the same part number in the same booklet is a
  conflict, even if the registered contents are identical;
* explicit reservations and explicit registration slots are fixed: same-slot
  empty reservations merge, one explicit claim occupies the slot, two
  different parts claiming one slot is a conflict;
* an implicit registration's landing slot is only a suggestion: it stays if
  the slot is still free (the highest-priority branch wins a shared
  suggestion) and moves otherwise;  moving parts take the smallest remaining
  free slot in branch-priority then in-branch registration order, never
  eating empty reservations;  insufficient capacity fails the whole merge;
* a part withdrawn inside its branch still occupies a tombstone slot under
  its own registration rule;
* amendments to base parts append after the base attachments in branch
  priority then in-branch order, duplicates kept;
* review flags, withdrawal and each replacement out-edge merge as net
  changes of the branch final state against the base: returning to the base
  value is no change, identical changes apply once, different changes to one
  out-edge (including delete vs. retarget) are a conflict;
* the merged replacement graph must satisfy the catalog rules -- branches
  that are individually acyclic but whose union cycles are rejected, naming
  the booklet and the cycle.

The base's versions, batch log and counters are carried over untouched; the
branches' local batches are not treated as having entered the main catalog
and no paper version is published by merging.  The merge is deterministic:
the same inputs always produce the same merged state and report.
"""

from __future__ import annotations

import hashlib
import json

from .catalog import OCCUPIED, RESERVED, STATE_FORMAT, TOMBSTONE, Catalog
from .errors import (
    BaseMismatch,
    BatchError,
    BranchInvalid,
    CapacityExceeded,
    EdgeConflict,
    InvalidBranchFile,
    InvalidOp,
    PartConflict,
    ReplacementCycle,
    SlotConflict,
    UnsupportedOp,
)

BRANCH_FORMAT = "slot-catalog-branch/1"
REPORT_FORMAT = "slot-catalog-merge-report/1"

# Ops the merge can fold into the base state.  ``publish`` and
# ``add_booklet`` are valid catalog ops but cannot be merged; unknown or
# malformed ops are left for the replay to reject as ``invalid_op``.
_MERGEABLE_OPS = frozenset(
    ["register", "amend", "set_review", "withdraw", "replace", "unreplace", "reserve"]
)
_NON_MERGEABLE_OPS = frozenset(["add_booklet", "publish"])

_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


class _BranchWork:
    """Replay record of one branch: final catalog plus op-level facts."""

    __slots__ = ("name", "catalog", "registrations", "reserves", "amends")

    def __init__(self, name):
        self.name = name
        self.catalog = None  # final replayed catalog
        self.registrations = []  # {"booklet","part","explicit","suggested","order"}
        self.reserves = []  # {"booklet","slot"}
        self.amends = {}  # (booklet, part) -> [attachment, ...]


def _validate_branch_doc(doc, index):
    """Normalize one branch document or raise :class:`InvalidBranchFile`."""
    where = "branch file #%d" % (index + 1)
    if not isinstance(doc, dict) or doc.get("format") != BRANCH_FORMAT:
        raise InvalidBranchFile(
            "%s is not a %s document" % (where, BRANCH_FORMAT), index=index
        )
    name = doc.get("branch")
    if not isinstance(name, str) or not name:
        raise InvalidBranchFile(
            "%s: 'branch' must be a non-empty string" % where, index=index
        )
    declared = doc.get("base_sha256")
    if (
        not isinstance(declared, str)
        or len(declared) != 64
        or any(ch not in _HEXDIGITS for ch in declared)
    ):
        raise InvalidBranchFile(
            "%s: 'base_sha256' must be 64 hexadecimal characters" % where,
            branch=name,
        )
    batches = doc.get("batches")
    if not isinstance(batches, list):
        raise InvalidBranchFile(
            "%s: 'batches' must be a list" % where, branch=name
        )
    normalized = []
    for pos, batch in enumerate(batches):
        if isinstance(batch, list):
            batch = {"ops": batch}
        if not isinstance(batch, dict) or not isinstance(batch.get("ops"), list):
            raise InvalidBranchFile(
                "%s: batch #%d needs an 'ops' list" % (where, pos + 1),
                branch=name,
            )
        batch_id = batch.get("batch_id")
        if batch_id is not None and not isinstance(batch_id, str):
            raise InvalidBranchFile(
                "%s: batch #%d has a non-string 'batch_id'" % (where, pos + 1),
                branch=name,
            )
        normalized.append({"batch_id": batch_id, "ops": batch["ops"]})
    return {"name": name, "sha": declared.lower(), "batches": normalized}


def _check_mergeable(branch_name, batch_id, batch_index, op_index, raw):
    kind = raw.get("op") if isinstance(raw, dict) else None
    details = {
        "branch": branch_name,
        "batch_index": batch_index,
        "op_index": op_index,
        "op": kind,
    }
    if batch_id is not None:
        details["batch_id"] = batch_id
    if kind in _NON_MERGEABLE_OPS:
        raise UnsupportedOp(
            "branch %r batch #%d op #%d: %r cannot be merged (only "
            "register/amend/set_review/withdraw/replace/unreplace and "
            "slot-explicit reserves are merged)"
            % (branch_name, batch_index, op_index, kind),
            **details
        )
    if kind == "reserve" and "count" in raw:
        raise UnsupportedOp(
            "branch %r batch #%d op #%d: count-based reserves cannot be "
            "merged; reserve explicit slots instead"
            % (branch_name, batch_index, op_index),
            **details
        )


def _replay_branch(base_doc, branch):
    """Apply one branch to a fresh copy of the base, batch by batch."""
    work = _BranchWork(branch["name"])
    cat = Catalog.load(base_doc)
    order = 0
    for batch_index, batch in enumerate(branch["batches"]):
        ops = batch["ops"]
        batch_id = batch["batch_id"]
        for op_index, raw in enumerate(ops):
            _check_mergeable(work.name, batch_id, batch_index, op_index, raw)
        try:
            cat.apply_batch(ops, batch_id=batch_id)
        except BatchError as exc:
            details = {
                "branch": work.name,
                "batch_id": exc.batch_id,
                "cause": exc.cause.to_dict(),
            }
            if exc.op_index is not None:
                details["op_index"] = exc.op_index
            raise BranchInvalid(
                "branch %r does not apply to the base on its own: batch %r "
                "failed: %s" % (work.name, exc.batch_id, exc.cause.message),
                **details
            )
        for raw in ops:
            kind = raw["op"]
            if kind == "register":
                booklet_id = raw["booklet"]
                part_no = raw["part"]
                landed = cat._booklets[booklet_id].parts[part_no]["slot"]
                work.registrations.append(
                    {
                        "booklet": booklet_id,
                        "part": part_no,
                        "explicit": raw.get("slot"),
                        "suggested": landed,
                        "order": order,
                    }
                )
                order += 1
            elif kind == "reserve":
                for slot_no in raw["slots"]:
                    work.reserves.append({"booklet": raw["booklet"], "slot": slot_no})
            elif kind == "amend":
                key = (raw["booklet"], raw["part"])
                work.amends.setdefault(key, []).append(raw["attachment"])
    work.catalog = cat
    return work


def merge_branches(base_bytes, branch_docs):
    """Merge branch files into the base state.

    ``base_bytes`` are the original bytes of the shared base state file;
    every branch must pin its SHA-256.  ``branch_docs`` are the parsed
    branch documents in priority order (highest first).  Returns the merged
    ``slot-catalog-state/1`` document plus a merge report listing every
    relocated registration.  Raises a :class:`CatalogError` subclass on
    failure; nothing is written by this function.
    """
    if not isinstance(base_bytes, (bytes, bytearray)):
        raise InvalidOp("base state must be given as bytes")
    base_bytes = bytes(base_bytes)
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    try:
        base_doc = json.loads(base_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidOp("base state is not valid JSON: %s" % exc)
    if not isinstance(base_doc, dict) or base_doc.get("format") != STATE_FORMAT:
        raise InvalidOp("base is not a %s document" % STATE_FORMAT)
    if not isinstance(branch_docs, list) or len(branch_docs) < 2:
        raise InvalidOp("merge needs at least two branch files")
    branches = [
        _validate_branch_doc(doc, index) for index, doc in enumerate(branch_docs)
    ]
    seen = set()
    for branch in branches:
        if branch["name"] in seen:
            raise InvalidBranchFile(
                "duplicate branch name %r" % branch["name"], branch=branch["name"]
            )
        seen.add(branch["name"])
    for branch in branches:
        if branch["sha"] != base_sha:
            raise BaseMismatch(
                "branch %r was cataloged against different base bytes"
                % branch["name"],
                branch=branch["name"],
                expected=branch["sha"],
                actual=base_sha,
            )
    try:
        Catalog.load(base_doc)
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidOp("base state is malformed: %r" % (exc,))
    works = [_replay_branch(base_doc, branch) for branch in branches]
    names = [work.name for work in works]

    # Two branches registering the same part number in the same booklet is a
    # conflict, even if the registered contents are identical.
    owner = {}  # (booklet, part) -> branch index
    for i, work in enumerate(works):
        for reg in work.registrations:
            key = (reg["booklet"], reg["part"])
            if key in owner:
                j = owner[key]
                raise PartConflict(
                    "part %r in booklet %r was registered by both branch %r "
                    "and branch %r" % (key[1], key[0], names[j], names[i]),
                    booklet=key[0],
                    part=key[1],
                    branches=[names[j], names[i]],
                )
            owner[key] = i

    # Two different parts explicitly claiming the same slot is a conflict.
    claim_of = {}  # (booklet, slot) -> (branch index, part)
    for i, work in enumerate(works):
        for reg in work.registrations:
            if reg["explicit"] is None:
                continue
            key = (reg["booklet"], reg["explicit"])
            if key in claim_of:
                j, other = claim_of[key]
                raise SlotConflict(
                    "slot %d in booklet %r is explicitly claimed by both "
                    "part %r (branch %r) and part %r (branch %r)"
                    % (key[1], key[0], other, names[j], reg["part"], names[i]),
                    booklet=key[0],
                    slot=key[1],
                    claims=[
                        {"branch": names[j], "part": other},
                        {"branch": names[i], "part": reg["part"]},
                    ],
                )
            claim_of[key] = (i, reg["part"])

    out = Catalog.load(base_doc)
    base_cat = Catalog.load(base_doc)
    moves = []
    for booklet_id in sorted(base_cat._booklets):
        _merge_booklet(
            booklet_id,
            base_cat._booklets[booklet_id],
            out._booklets[booklet_id],
            works,
            names,
            out,
            moves,
        )
    report = {
        "ok": True,
        "format": REPORT_FORMAT,
        "base_sha256": base_sha,
        "branches": list(names),
        "moves": moves,
    }
    return out.save(), report


def _merge_booklet(booklet_id, base_b, out_b, works, names, out_cat, moves):
    """Fold every branch's final state for one booklet into ``out_b``."""
    explicit_reserves = set()
    for work in works:
        for res in work.reserves:
            if res["booklet"] == booklet_id:
                explicit_reserves.add(res["slot"])

    claims = {}  # slot -> (branch index, part); uniqueness already checked
    implicit = []  # (branch index, registration)
    for i, work in enumerate(works):
        for reg in work.registrations:
            if reg["booklet"] != booklet_id:
                continue
            if reg["explicit"] is not None:
                claims[reg["explicit"]] = (i, reg["part"])
            else:
                implicit.append((i, reg))

    # Fixed slots never move: every base slot, every explicit reservation,
    # every explicit registration landing spot.
    fixed = set(base_b.slots) | explicit_reserves | set(claims)

    # Implicit landing slots are only suggestions: keep the ones that are
    # still free (highest-priority branch wins a shared suggestion), move
    # the rest.
    by_slot = {}
    for i, reg in implicit:
        by_slot.setdefault(reg["suggested"], []).append((i, reg))
    stayers = {}  # slot -> (branch index, registration)
    movers = []  # (branch index, registration)
    for slot_no, group in by_slot.items():
        group.sort(key=lambda item: (item[0], item[1]["order"]))
        if slot_no in fixed:
            movers.extend(group)
        else:
            stayers[slot_no] = group[0]
            movers.extend(group[1:])
    movers.sort(key=lambda item: (item[0], item[1]["order"]))

    # Moving parts take the smallest remaining free slot in branch-priority
    # then in-branch registration order, never eating empty reservations.
    assigned = {}  # part -> slot
    used = fixed | set(stayers)
    cand = 0
    for i, reg in movers:
        while cand in used:
            cand += 1
        if cand >= base_b.capacity:
            raise CapacityExceeded(
                "booklet %r is full (%d slots): no free slot for part %r "
                "from branch %r"
                % (booklet_id, base_b.capacity, reg["part"], names[i]),
                booklet=booklet_id,
            )
        assigned[reg["part"]] = cand
        used.add(cand)
        moves.append(
            {
                "branch": names[i],
                "booklet": booklet_id,
                "part": reg["part"],
                "from_slot": reg["suggested"],
                "to_slot": cand,
                "from_slot_label": out_cat.slot_label(booklet_id, reg["suggested"]),
                "to_slot_label": out_cat.slot_label(booklet_id, cand),
            }
        )

    def final_slot(reg):
        if reg["explicit"] is not None:
            return reg["explicit"]
        return assigned.get(reg["part"], reg["suggested"])

    # Empty explicit reservations survive as reservations (same-slot empty
    # reservations merge into one).
    for slot_no in sorted(explicit_reserves):
        if slot_no not in claims:
            out_b.slots[slot_no] = {"state": RESERVED, "part": None}

    # New parts land on their merged slots; a part withdrawn inside its
    # branch still occupies a tombstone slot under its registration rule.
    for i, work in enumerate(works):
        final_b = work.catalog._booklets[booklet_id]
        for reg in work.registrations:
            if reg["booklet"] != booklet_id:
                continue
            part_no = reg["part"]
            src = final_b.parts[part_no]
            slot_no = final_slot(reg)
            out_b.slots[slot_no] = {
                "state": TOMBSTONE if src["withdrawn"] else OCCUPIED,
                "part": part_no,
            }
            out_b.parts[part_no] = {
                "slot": slot_no,
                "title": src["title"],
                "body": src["body"],
                "attachments": list(src["attachments"]),
                "withdrawn": src["withdrawn"],
                "approved": src["approved"],
            }

    # Base parts: amendments append after the base attachments in branch
    # priority then in-branch order (duplicates kept); review and withdrawal
    # merge as net changes of the branch final state against the base.
    for part_no, base_rec in base_b.parts.items():
        out_rec = out_b.parts[part_no]
        for work in works:
            added = work.amends.get((booklet_id, part_no))
            if added:
                out_rec["attachments"].extend(added)
        for work in works:
            final_rec = work.catalog._booklets[booklet_id].parts[part_no]
            if final_rec["approved"] != base_rec["approved"]:
                out_rec["approved"] = final_rec["approved"]
            if final_rec["withdrawn"] != base_rec["withdrawn"]:
                out_rec["withdrawn"] = final_rec["withdrawn"]
                if final_rec["withdrawn"]:
                    out_b.slots[out_rec["slot"]]["state"] = TOMBSTONE

    # Replacement out-edges merge as net changes against the base: identical
    # changes apply once, different changes to one out-edge are a conflict.
    olds = set(base_b.edges)
    for work in works:
        olds |= set(work.catalog._booklets[booklet_id].edges)
    merged_edges = dict(base_b.edges)
    for old in sorted(olds):
        base_target = base_b.edges.get(old)
        targets = []
        seen_targets = set()
        changes = []
        for i, work in enumerate(works):
            branch_target = work.catalog._booklets[booklet_id].edges.get(old)
            if branch_target != base_target:
                changes.append({"branch": names[i], "target": branch_target})
                if branch_target not in seen_targets:
                    seen_targets.add(branch_target)
                    targets.append(branch_target)
        if not targets:
            continue
        if len(targets) > 1:
            raise EdgeConflict(
                "replacement edge from part %r in booklet %r has conflicting "
                "changes: %s"
                % (
                    old,
                    booklet_id,
                    ", ".join(
                        "%s -> %s" % (c["branch"], c["target"]) for c in changes
                    ),
                ),
                booklet=booklet_id,
                part=old,
                changes=changes,
            )
        target = targets[0]
        if target is None:
            merged_edges.pop(old, None)
        else:
            merged_edges[old] = target
    out_b.edges = merged_edges

    # The merged replacement graph must satisfy the catalog rules; branches
    # that are individually acyclic may still cycle when combined.
    try:
        Catalog._validate_replacement_graph(out_b)
    except ReplacementCycle as exc:
        cycle = list(exc.details.get("cycle", []))
        raise ReplacementCycle(
            "replacement cycle in booklet %r: %s"
            % (booklet_id, " -> ".join(cycle)),
            booklet=booklet_id,
            cycle=cycle,
        )

    hint = 0
    while hint in out_b.slots:
        hint += 1
    out_b.alloc_hint = hint
    out_cat._invalidate(booklet_id)
