"""Daily access statistics (日访问统计) over the current catalog.

A disclosure site must know which *currently public* items readers actually
reached.  Reader logs name the paper index a reader used -- a paper edition
(版次), a booklet (册) and a logical slot number (逻辑槽号) -- so a tally is
only meaningful when every record is interpreted through the *current*
catalog:

* a record whose ``(version, booklet, slot)`` was never formally printed in
  that paper edition counts as **unprinted** (未印入口) for its day/booklet;
* a printed entry whose replacement endpoint is currently not public
  (withdrawn or not approved) counts as **not public** (不可公开);
* every other printed entry counts towards its **current public endpoint**
  (the reader reached the live item, not the old body printed on paper);
* entries of one booklet that resolve to the same endpoint are merged;
* different dates and booklets are tallied separately.

Nothing about a non-public endpoint's body or attachments is stored or
reported; the table holds only counts.  The report also gives the total
number of valid accesses per (date, booklet) group, which by construction
equals the public-endpoint counts plus the two ungrouped counters.

Records are corrections, not an append-only stream: every record has a
globally unique record id, and later groups may replace (correct) or revoke
it.  A group is atomic: a duplicate add, a correction/revocation of a
missing id, or two ops naming one id rejects the whole group.

Maintenance is incremental.  The full derivation happens once, at
``AccessStats.open``.  Afterwards a correction batch only re-resolves the
formally printed entries whose replacement chain it actually touched (the
dirty parts plus everything upstream of them, found through a reverse edge
index and the printed-slot index) and moves *only the attribution delta* of
the records sitting on those entries.  A historical record that the
correction does not affect is never re-walked, and no other booklet's
records are scanned.  A newly published paper edition is indexed in full
(that edition is new).  Record corrections touch only the records they name
and the few buckets they leave and enter.  Loading, saving and validating
the catalog are explicitly outside the budget.  ``AccessStats.stats``
instruments the work so tests can assert on this shape.

The whole state -- catalog, records, tallies and resume indexes -- is saved
as one deterministic ``slot-catalog-access-stats/1`` document; a reopened
file answers exactly like the live object, and a failed batch leaves both
catalog and statistics untouched.
"""

from __future__ import annotations

import datetime
import hashlib
import json

from .catalog import STATE_FORMAT, Catalog
from .errors import (
    BatchError,
    CatalogError,
    DuplicateRecordOp,
    InvalidAccessRecord,
    InvalidOp,
    RecordBatchError,
    RecordExists,
    RecordNotFound,
)

ACCESS_FORMAT = "slot-catalog-access-stats/1"
REPORT_FORMAT = "slot-catalog-access-report/1"

UNPRINTED = "unprinted"
NOT_PUBLIC = "not_public"
PUBLIC = "public"

RECORD_OP_KINDS = frozenset(["add", "correct", "revoke"])

_DATE_FORMAT = "%Y-%m-%d"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical(doc):
    """Deterministic byte encoding used for the opening-state digest."""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(doc):
    return hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()


def _check_date(value, field):
    if not isinstance(value, str) or len(value) != 10:
        raise InvalidAccessRecord(
            "%s must be a YYYY-MM-DD string" % field, field=field
        )
    try:
        parsed = datetime.datetime.strptime(value, _DATE_FORMAT)
    except ValueError:
        raise InvalidAccessRecord(
            "%s must be a real YYYY-MM-DD date, got %r" % (field, value),
            field=field,
        )
    if parsed.strftime(_DATE_FORMAT) != value:
        raise InvalidAccessRecord(
            "%s must be a real YYYY-MM-DD date, got %r" % (field, value),
            field=field,
        )
    return value


def _check_id(value):
    if not isinstance(value, str) or not value:
        raise InvalidAccessRecord("record id must be a non-empty string", field="id")
    return value


def _check_slot(value):
    if _is_int(value):
        slot_no = value
    elif isinstance(value, str) and value.isdigit():
        slot_no = int(value)
    else:
        raise InvalidAccessRecord(
            "slot must be a slot number (integer or zero-padded digits)",
            field="slot",
        )
    if slot_no < 0:
        raise InvalidAccessRecord("slot must not be negative", field="slot")
    return slot_no


def _check_count(value):
    if not _is_int(value) or value < 1:
        raise InvalidAccessRecord(
            "count must be a positive integer", field="count"
        )
    return value


def _normalize_record(raw):
    """Validate one access record (also used for a correction payload)."""
    if not isinstance(raw, dict):
        raise InvalidAccessRecord("an access record must be an object")
    record_id = _check_id(raw.get("id"))
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise InvalidAccessRecord(
            "record %r: version must be a non-empty string" % record_id,
            field="version",
            id=record_id,
        )
    booklet = raw.get("booklet")
    if not isinstance(booklet, str) or not booklet:
        raise InvalidAccessRecord(
            "record %r: booklet must be a non-empty string" % record_id,
            field="booklet",
            id=record_id,
        )
    return {
        "id": record_id,
        "date": _check_date(raw.get("date"), "date"),
        "version": version,
        "booklet": booklet,
        "slot": _check_slot(raw.get("slot")),
        "count": _check_count(raw.get("count")),
    }


def _normalize_record_op(raw):
    if not isinstance(raw, dict):
        raise InvalidAccessRecord("a record op must be an object")
    kind = raw.get("op")
    if kind not in RECORD_OP_KINDS:
        raise InvalidAccessRecord(
            "record op must be one of add/correct/revoke, got %r" % (kind,),
            field="op",
        )
    record_id = _check_id(raw.get("id"))
    if kind == "revoke":
        extra = set(raw) - {"op", "id"}
        if extra:
            raise InvalidAccessRecord(
                "a revoke op carries only op/id; unexpected %s"
                % ", ".join(sorted(extra)),
                id=record_id,
            )
        return {"op": "revoke", "id": record_id}
    record = _normalize_record(raw)
    return {"op": kind, "id": record["id"], "record": record}


class AccessStats:
    """Daily access tallies interpreted through the current catalog."""

    def __init__(self):
        self.catalog = None
        self.source_id = None
        self.records = {}  # record id -> normalized record dict
        self.attribution = {}  # record id -> ("public", booklet, part) | (kind,)
        self.table = {}  # (date, booklet) -> {"public": {ep: n}, "unprinted": n, "not_public": n}
        self._printed = {}  # (booklet, part) -> {(version, slot)}
        self._slots = {}  # (version, booklet) -> {slot: part}
        self._rev = {}  # booklet -> {new part: {old part}}
        self._by_entry = {}  # (version, booklet, slot) -> {record id}
        self.stats = {
            "entries_recomputed": 0,
            "records_recomputed": 0,
            "buckets_scanned": 0,
        }

    # ------------------------------------------------------------------
    # open / persist
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, state_doc):
        """Open statistics over a ``slot-catalog-state/1`` catalog document.

        This is the one full pass the requirements allow: the catalog is
        loaded and both resume indexes (printed parts and printed slots) are
        built.  Access records arrive later, through :meth:`apply_records`.
        """
        if not isinstance(state_doc, dict) or state_doc.get("format") != STATE_FORMAT:
            raise InvalidOp(
                "access statistics can only open a %s document" % STATE_FORMAT
            )
        stats = cls()
        stats.catalog = Catalog.load(state_doc)
        stats.source_id = _digest(state_doc)
        stats._build_catalog_indexes()
        return stats

    def _build_catalog_indexes(self):
        cat = self.catalog
        self._printed = {}
        self._slots = {}
        for version_id, version in cat.versions.items():
            for booklet_id, table in version["records"].items():
                by_slot = self._slots.setdefault((version_id, booklet_id), {})
                for slot_no, rec in table.items():
                    by_slot[slot_no] = rec["part"]
                    self._printed.setdefault((booklet_id, rec["part"]), set()).add(
                        (version_id, slot_no)
                    )
        self._rev = {}
        for booklet_id, booklet in cat._booklets.items():
            rev = {}
            for old, new in booklet.edges.items():
                rev.setdefault(new, set()).add(old)
            self._rev[booklet_id] = rev

    def _build_record_index(self):
        self._by_entry = {}
        for record_id, record in self.records.items():
            key = (record["version"], record["booklet"], record["slot"])
            self._by_entry.setdefault(key, set()).add(record_id)

    def save(self):
        """Serialize catalog, records, per-record attribution and tallies."""
        buckets = []
        for (date, booklet_id), bucket in sorted(self.table.items()):
            buckets.append(
                {
                    "date": date,
                    "booklet": booklet_id,
                    "public": [
                        {
                            "booklet": endpoint[0],
                            "part": endpoint[1],
                            "count": count,
                        }
                        for endpoint, count in sorted(bucket["public"].items())
                    ],
                    "unprinted": bucket["unprinted"],
                    "not_public": bucket["not_public"],
                }
            )
        attribution = {}
        for record_id, value in sorted(self.attribution.items()):
            attribution[record_id] = list(value)
        return {
            "format": ACCESS_FORMAT,
            "catalog": self.catalog.save(),
            "access": {
                "source": self.source_id,
                "records": {
                    record_id: dict(record)
                    for record_id, record in sorted(self.records.items())
                },
                "attribution": attribution,
                "buckets": buckets,
            },
        }

    @classmethod
    def load(cls, doc):
        """Resume a statistics file; the result equals the live object.

        The saved attribution is authoritative, so reopening never
        re-resolves any record through the catalog -- the restart cost does
        not grow with the number of corrected records.
        """
        if not isinstance(doc, dict) or doc.get("format") != ACCESS_FORMAT:
            raise InvalidOp("not a %s document" % ACCESS_FORMAT)
        access = doc.get("access")
        if not isinstance(access, dict):
            raise InvalidOp("access statistics are missing their 'access' section")
        stats = cls()
        stats.catalog = Catalog.load(doc.get("catalog"))
        stats.source_id = access.get("source")
        stats.records = {}
        for record_id, raw in (access.get("records") or {}).items():
            stats.records[record_id] = _normalize_record(dict(raw, id=record_id))
        stats.attribution = {}
        for record_id, value in (access.get("attribution") or {}).items():
            attribution = cls._parse_attribution(value, record_id)
            stats.attribution[record_id] = attribution
        if set(stats.attribution) != set(stats.records):
            raise InvalidOp(
                "access attribution does not match the record set"
            )
        stats.table = {}
        for bucket in access.get("buckets") or []:
            public = {}
            for row in bucket["public"]:
                public[(row["booklet"], row["part"])] = row["count"]
            stats.table[(bucket["date"], bucket["booklet"])] = {
                "public": public,
                "unprinted": bucket["unprinted"],
                "not_public": bucket["not_public"],
            }
        stats._build_catalog_indexes()
        stats._build_record_index()
        stats._verify_tallies()
        return stats

    def _verify_tallies(self):
        """Reject a file whose buckets and per-record attribution disagree.

        Counts are never re-derived from the catalog here (that would make a
        restart cost grow with history); only the saved tables are checked
        against each other, which is linear in the saved rows and catches a
        hand-edited or truncated file.
        """
        tally = {}
        for record_id, record in self.records.items():
            attribution = self.attribution[record_id]
            key = (record["date"], record["booklet"])
            bucket = tally.setdefault(
                key, {"public": {}, "unprinted": 0, "not_public": 0}
            )
            if attribution[0] == PUBLIC:
                endpoint = (attribution[1], attribution[2])
                bucket["public"][endpoint] = (
                    bucket["public"].get(endpoint, 0) + record["count"]
                )
            else:
                bucket[attribution[0]] += record["count"]
        if tally != self.table:
            raise InvalidOp(
                "access tallies do not match the per-record attribution"
            )

    @staticmethod
    def _parse_attribution(value, record_id):
        if (
            isinstance(value, list)
            and value
            and value[0] == PUBLIC
            and len(value) == 3
            and isinstance(value[1], str)
            and isinstance(value[2], str)
        ):
            return (PUBLIC, value[1], value[2])
        if isinstance(value, list) and len(value) == 1 and value[0] in (
            UNPRINTED,
            NOT_PUBLIC,
        ):
            return (value[0],)
        raise InvalidOp(
            "access attribution for record %r is malformed" % record_id
        )

    # ------------------------------------------------------------------
    # interpretation
    # ------------------------------------------------------------------

    def _classify(self, version_id, booklet_id, slot_no):
        """Current meaning of one paper index.

        Returns ``("public", booklet, endpoint_part)`` for a printed entry
        whose live endpoint is public, ``("unprinted",)`` when the paper
        edition does not formally list that slot (or names a booklet or
        edition the catalog never printed), or ``("not_public",)`` when the
        live endpoint is withdrawn or not approved.
        """
        by_slot = self._slots.get((version_id, booklet_id))
        printed = by_slot.get(slot_no) if by_slot is not None else None
        if printed is None:
            return (UNPRINTED,)
        try:
            endpoint = self.catalog.public_endpoint_key(booklet_id, printed)
        except CatalogError:
            # a hand-edited file may print a slot naming a booklet or part
            # that no longer exists; the paper index then leads nowhere
            return (UNPRINTED,)
        if endpoint is None:
            return (NOT_PUBLIC,)
        return (PUBLIC, endpoint["booklet"], endpoint["part"])

    # ------------------------------------------------------------------
    # tallies (delta updates on one bucket)
    # ------------------------------------------------------------------

    def _add_attribution(self, key, attribution, count):
        bucket = self.table.setdefault(
            key, {"public": {}, "unprinted": 0, "not_public": 0}
        )
        if attribution[0] == PUBLIC:
            endpoint = (attribution[1], attribution[2])
            bucket["public"][endpoint] = bucket["public"].get(endpoint, 0) + count
        else:
            bucket[attribution[0]] += count

    def _remove_attribution(self, key, attribution, count):
        bucket = self.table.get(key)
        if bucket is None:  # defensive; a live record always has its bucket
            return
        if attribution[0] == PUBLIC:
            endpoint = (attribution[1], attribution[2])
            remaining = bucket["public"].get(endpoint, 0) - count
            if remaining > 0:
                bucket["public"][endpoint] = remaining
            else:
                bucket["public"].pop(endpoint, None)
        else:
            bucket[attribution[0]] = max(0, bucket[attribution[0]] - count)
        if not (bucket["public"] or bucket["unprinted"] or bucket["not_public"]):
            del self.table[key]

    def _move(self, record_id, new_attribution):
        record = self.records[record_id]
        old_attribution = self.attribution[record_id]
        if old_attribution == new_attribution:
            return False
        key = (record["date"], record["booklet"])
        self._remove_attribution(key, old_attribution, record["count"])
        self._add_attribution(key, new_attribution, record["count"])
        self.attribution[record_id] = new_attribution
        return True

    # ------------------------------------------------------------------
    # record groups
    # ------------------------------------------------------------------

    def apply_records(self, ops, group_id=None):
        """Apply one atomic group of record adds, corrections and revocations.

        Every op is validated, and every id is checked for a duplicate inside
        the group, before anything is mutated, so a rejected group leaves
        catalog, records and tallies exactly as they were.
        """
        if not isinstance(ops, list):
            raise InvalidAccessRecord("record ops must be a list")
        normalized = []
        seen = set()
        for index, raw in enumerate(ops):
            try:
                op = _normalize_record_op(raw)
            except CatalogError as exc:
                raise RecordBatchError(group_id, exc, index) from exc
            record_id = op["id"]
            if record_id in seen:
                raise RecordBatchError(
                    group_id,
                    DuplicateRecordOp(
                        "record %r appears more than once in the group"
                        % record_id,
                        id=record_id,
                    ),
                    index,
                )
            seen.add(record_id)
            if op["op"] == "add" and record_id in self.records:
                raise RecordBatchError(
                    group_id,
                    RecordExists(
                        "record %r already exists" % record_id, id=record_id
                    ),
                    index,
                )
            if op["op"] in ("correct", "revoke") and record_id not in self.records:
                raise RecordBatchError(
                    group_id,
                    RecordNotFound(
                        "record %r does not exist" % record_id, id=record_id
                    ),
                    index,
                )
            normalized.append(op)
        # validation passed: snapshot-free mutation, none of it can fail
        self.stats = {
            "entries_recomputed": 0,
            "records_recomputed": 0,
            "buckets_scanned": 0,
        }
        buckets = set()
        for op in normalized:
            record_id = op["id"]
            if op["op"] != "add":
                old = self.records[record_id]
                buckets.add((old["date"], old["booklet"]))
                old_key = (old["version"], old["booklet"], old["slot"])
                self._by_entry[old_key].discard(record_id)
                if not self._by_entry[old_key]:
                    del self._by_entry[old_key]
                bucket = (old["date"], old["booklet"])
                self._remove_attribution(
                    bucket, self.attribution[record_id], old["count"]
                )
                del self.attribution[record_id]
                del self.records[record_id]
            if op["op"] != "revoke":
                record = op["record"]
                attribution = self._classify(
                    record["version"], record["booklet"], record["slot"]
                )
                self.records[record_id] = record
                self.attribution[record_id] = attribution
                new_key = (record["version"], record["booklet"], record["slot"])
                self._by_entry.setdefault(new_key, set()).add(record_id)
                self._add_attribution(
                    (record["date"], record["booklet"]), attribution, record["count"]
                )
                buckets.add((record["date"], record["booklet"]))
            self.stats["records_recomputed"] += 1
        self.stats["buckets_scanned"] = len(buckets)
        added = sum(1 for op in normalized if op["op"] == "add")
        corrected = sum(1 for op in normalized if op["op"] == "correct")
        return {
            "ok": True,
            "group_id": group_id,
            "applied": len(normalized),
            "added": added,
            "corrected": corrected,
            "revoked": len(normalized) - added - corrected,
        }

    # ------------------------------------------------------------------
    # catalog corrections (the existing batch format, publish included)
    # ------------------------------------------------------------------

    def apply_batch(self, ops, batch_id=None):
        """Apply one catalog correction batch and fold it into the tallies.

        Catalog semantics are the catalog's own (``apply_batch``); a failed
        batch changes neither the catalog nor the statistics.  On success
        only the printed entries whose chain the batch touched are
        re-resolved, and only their records' attribution deltas move.
        """
        edge_changes = []
        if isinstance(ops, list):
            for raw in ops:
                if not isinstance(raw, dict) or raw.get("op") not in (
                    "replace",
                    "unreplace",
                ):
                    continue
                booklet_id = raw.get("booklet")
                old = raw.get("old")
                booklet = self.catalog._booklets.get(booklet_id)
                prev = None
                if booklet is not None and isinstance(old, str):
                    prev = booklet.edges.get(old)
                edge_changes.append((raw, booklet_id, old, prev))
        result = self.catalog.apply_batch(ops, batch_id=batch_id)
        if isinstance(ops, list):
            # keep the reverse edge index current from the changed edges only
            for raw, booklet_id, old, prev in edge_changes:
                rev = self._rev.setdefault(booklet_id, {})
                if prev is not None:
                    rev.get(prev, set()).discard(old)
                if raw["op"] == "replace":
                    rev.setdefault(raw["new"], set()).add(old)
            self._maintain(ops, result["published"])
        return result

    def _maintain(self, ops, published):
        self.stats = {
            "entries_recomputed": 0,
            "records_recomputed": 0,
            "buckets_scanned": 0,
        }
        dirty = set()
        for raw in ops:
            kind = raw.get("op")
            booklet_id = raw.get("booklet")
            if kind in ("register", "amend", "withdraw", "set_review"):
                dirty.add((booklet_id, raw.get("part")))
            elif kind == "replace":
                dirty.add((booklet_id, raw.get("old")))
                dirty.add((booklet_id, raw.get("new")))
            elif kind == "unreplace":
                dirty.add((booklet_id, raw.get("old")))
        # affected = dirty parts plus everything upstream of them
        affected = set(dirty)
        stack = list(dirty)
        while stack:
            key = stack.pop()
            for pred in self._rev.get(key[0], {}).get(key[1], ()):
                pred_key = (key[0], pred)
                if pred_key not in affected:
                    affected.add(pred_key)
                    stack.append(pred_key)
        entry_keys = set()
        for key in affected:
            for version_id, slot_no in self._printed.get(key, ()):
                entry_keys.add((version_id, key[0], slot_no))
        self._fold_entries(entry_keys)
        for version_id in published:
            self._index_published(version_id)
        return self.stats

    def _fold_entries(self, entry_keys):
        """Re-resolve touched paper indexes and move only their records."""
        if not entry_keys:
            return
        new_attr = {key: self._classify(*key) for key in entry_keys}
        self.stats["entries_recomputed"] += len(entry_keys)
        touched = set()
        for key in entry_keys:
            touched.update(self._by_entry.get(key, ()))
        buckets = set()
        for record_id in sorted(touched):
            if record_id not in self.records:
                continue
            record = self.records[record_id]
            key = (record["version"], record["booklet"], record["slot"])
            new = new_attr[key]
            self.stats["records_recomputed"] += 1
            if self._move(record_id, new):
                buckets.add((record["date"], record["booklet"]))
        self.stats["buckets_scanned"] += len(buckets)

    def _index_published(self, version_id):
        """Index every printed slot of a freshly published paper edition."""
        version = self.catalog.versions[version_id]
        entry_keys = set()
        for booklet_id, table in version["records"].items():
            by_slot = self._slots.setdefault((version_id, booklet_id), {})
            for slot_no, rec in table.items():
                by_slot[slot_no] = rec["part"]
                self._printed.setdefault((booklet_id, rec["part"]), set()).add(
                    (version_id, slot_no)
                )
                entry_keys.add((version_id, booklet_id, slot_no))
        self._fold_entries(entry_keys)

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------

    def report(self, date=None, booklet=None):
        """Counts per (visit date, booklet), public endpoints split out.

        Only groups with a non-zero count appear; public endpoint rows are
        only emitted for non-zero counts.  ``total`` is the number of valid
        accesses in the group and always equals the public rows plus the
        unprinted and not-public counters.
        """
        if date is not None:
            _check_date(date, "date")
        if booklet is not None and (
            not isinstance(booklet, str) or not booklet
        ):
            raise InvalidOp("booklet must be a non-empty string", field="booklet")
        days = {}
        for (bucket_date, booklet_id), bucket in self.table.items():
            if date is not None and bucket_date != date:
                continue
            if booklet is not None and booklet_id != booklet:
                continue
            public = [
                {
                    "booklet": endpoint[0],
                    "part": endpoint[1],
                    "count": count,
                }
                for endpoint, count in sorted(bucket["public"].items())
                if count
            ]
            public_total = sum(row["count"] for row in public)
            total = public_total + bucket["unprinted"] + bucket["not_public"]
            if total == 0:  # zero groups are not emitted
                continue
            days.setdefault(bucket_date, []).append(
                {
                    "booklet": booklet_id,
                    "public": public,
                    "public_total": public_total,
                    "unprinted": bucket["unprinted"],
                    "not_public": bucket["not_public"],
                    "total": total,
                }
            )
        return {
            "ok": True,
            "format": REPORT_FORMAT,
            "days": [
                {"date": day, "booklets": rows}
                for day, rows in sorted(days.items())
            ],
        }

    def checkpoint(self):
        """Lightweight summary helpful when comparing two runs."""
        return {
            "source": self.source_id,
            "records": len(self.records),
            "groups": len(self.table),
        }
