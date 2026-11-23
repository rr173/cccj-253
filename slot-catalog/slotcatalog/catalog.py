"""Stable-slot catalog: a locally maintainable slot directory with two read views.

Concepts
--------
* booklet (册): unique string id, owns an independent non-negative integer
  slot space.  ``slot_width`` is the fixed typesetting width of a slot label
  (slot 7 with width 4 prints as ``"0007"``) and bounds the booklet capacity
  to ``10 ** slot_width`` slots.
* slot (槽): ``reserved`` (placeholder, no part), ``occupied`` by a part, or a
  ``tombstone`` (its part was withdrawn; the part keeps the slot forever and
  the slot is never reused).
* part (件): part number unique inside its booklet; occupies exactly one
  stable slot for its whole life.  Amendments append attachments to the same
  part and never move the slot.
* replacement edge (替代边): ``old -> new`` means part ``new`` legally
  replaces part ``old``.  Both parts must already occupy slots in the same
  booklet.  Out-degree is at most one; cycles are rejected.  Edges are only
  ever interpreted lazily (at read time, through the edge index), so changing
  one edge never rewrites any historical slot or version record.
* batch (编目批次): an ordered list of operations applied atomically.
  Replacement-graph invariants are validated on the batch-final graph, so a
  batch may drop an old edge and set a new one in any order.
* version (版次): an immutable paper-edition table recording, per booklet,
  the slot -> part identity entries formally listed at publish time.  Bodies
  are never copied into version tables.

Read views
----------
* ``query_slot(version, booklet, slot)`` -- stable-slot query: what was
  printed at that slot in that paper version, plus the *current* replacement
  endpoint and either its public value or the reason it is not public.
* ``search_export([booklet])`` -- the current visible catalog: every approved,
  non-withdrawn visible endpoint exactly once, marked with its stable slot.
"""

from __future__ import annotations

from .errors import (
    AlreadyWithdrawn,
    BatchError,
    BookletExists,
    CapacityExceeded,
    CatalogError,
    DuplicateVersion,
    EdgeNotFound,
    InvalidOp,
    PartExists,
    PartNotFound,
    ReplacementCycle,
    SlotNotPrinted,
    SlotNotReserved,
    SlotOccupied,
    SlotOutOfRange,
    UnknownBooklet,
    UnknownVersion,
)

STATE_FORMAT = "slot-catalog-state/1"

RESERVED = "reserved"
OCCUPIED = "occupied"
TOMBSTONE = "tombstone"

MAX_SLOT_WIDTH = 12

_MISSING = object()

_OP_KINDS = frozenset(
    [
        "add_booklet",
        "reserve",
        "register",
        "amend",
        "withdraw",
        "set_review",
        "replace",
        "unreplace",
        "publish",
    ]
)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_str(value, field):
    if not isinstance(value, str) or not value:
        raise InvalidOp("%s must be a non-empty string" % field, field=field)
    return value


def _optional_str(value, field):
    if value is not None and not isinstance(value, str):
        raise InvalidOp("%s must be a string or null" % field, field=field)
    return value


class _Booklet:
    __slots__ = (
        "booklet_id",
        "slot_width",
        "capacity",
        "slots",
        "parts",
        "edges",
        "alloc_hint",
    )

    def __init__(self, booklet_id, slot_width):
        self.booklet_id = booklet_id
        self.slot_width = slot_width
        self.capacity = 10 ** slot_width
        self.slots = {}  # slot_no -> {"state": ..., "part": part_no | None}
        self.parts = {}  # part_no -> {"slot","title","body","attachments","withdrawn","approved"}
        self.edges = {}  # old part_no -> new part_no
        self.alloc_hint = 0  # smallest slot number that may be fresh


class Catalog:
    """The whole disclosure system: booklets, batches, versions, read views."""

    def __init__(self):
        self._booklets = {}
        self.versions = {}  # version_id -> {"seq": int, "records": {booklet: {slot: {"part","title"}}}}
        self.batches = []  # audit log of batch summaries
        self._batch_seq = 0
        self._version_seq = 0
        self._endpoint_cache = {}  # booklet_id -> {part_no: endpoint part_no}
        self.follow_steps = 0  # instrumentation: edge hops performed by resolve_endpoint

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_booklet(self, booklet_id):
        booklet = self._booklets.get(booklet_id)
        if booklet is None:
            raise UnknownBooklet(
                "unknown booklet %r" % (booklet_id,), booklet=booklet_id
            )
        return booklet

    @staticmethod
    def _get_part(booklet, part_no):
        _require_str(part_no, "part")
        part = booklet.parts.get(part_no)
        if part is None:
            raise PartNotFound(
                "part %r is not registered in booklet %r"
                % (part_no, booklet.booklet_id),
                part=part_no,
                booklet=booklet.booklet_id,
            )
        return part

    @staticmethod
    def _check_slot_no(booklet, slot_no):
        if not _is_int(slot_no):
            raise InvalidOp("slot must be an integer", field="slot")
        if slot_no < 0 or slot_no >= booklet.capacity:
            raise SlotOutOfRange(
                "slot %r outside 0..%d for booklet %r"
                % (slot_no, booklet.capacity - 1, booklet.booklet_id),
                slot=slot_no,
                booklet=booklet.booklet_id,
            )

    @staticmethod
    def _alloc_slot(booklet):
        hint = booklet.alloc_hint
        while hint in booklet.slots:
            hint += 1
        if hint >= booklet.capacity:
            raise CapacityExceeded(
                "booklet %r is full (%d slots)"
                % (booklet.booklet_id, booklet.capacity),
                booklet=booklet.booklet_id,
            )
        booklet.alloc_hint = hint + 1
        booklet.slots[hint] = {"state": RESERVED, "part": None}
        return hint

    def _invalidate(self, booklet_id):
        self._endpoint_cache.pop(booklet_id, None)

    def slot_label(self, booklet_id, slot_no):
        """Fixed-width typesetting label of a slot, e.g. ``"0007"``."""
        booklet = self._get_booklet(booklet_id)
        return "%0*d" % (booklet.slot_width, slot_no)

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------

    def apply_batch(self, ops, batch_id=None):
        """Apply a cataloging batch atomically.

        Ops run in order; replacement-graph invariants (both endpoints occupy
        slots, same booklet, acyclic) are validated on the batch-final graph.
        On any failure every already-applied op is rolled back and a
        ``BatchError`` is raised.
        """
        if not isinstance(ops, list):
            raise InvalidOp("ops must be a list")
        self._batch_seq += 1
        if batch_id is None:
            batch_id = "BATCH-%d" % self._batch_seq
        _require_str(batch_id, "batch_id")
        undo = []
        published = []
        touched = set()
        error_index = None
        try:
            pending_publish = []
            for index, raw in enumerate(ops):
                error_index = index
                op = self._check_op(raw)
                kind = op["op"]
                if kind == "publish":
                    pending_publish.append(index)
                elif kind == "add_booklet":
                    self._apply_add_booklet(op, undo)
                else:
                    booklet = self._get_booklet(op.get("booklet"))
                    if kind == "reserve":
                        self._apply_reserve(booklet, op, undo)
                    elif kind == "register":
                        self._apply_register(booklet, op, undo)
                    elif kind == "amend":
                        self._apply_amend(booklet, op, undo)
                    elif kind == "withdraw":
                        self._apply_withdraw(booklet, op, undo)
                    elif kind == "set_review":
                        self._apply_set_review(booklet, op, undo)
                    elif kind == "replace":
                        touched.add(booklet.booklet_id)
                        self._apply_replace(booklet, op, undo)
                    elif kind == "unreplace":
                        touched.add(booklet.booklet_id)
                        self._apply_unreplace(booklet, op, undo)
            error_index = None
            for booklet_id in touched:
                self._validate_replacement_graph(self._booklets[booklet_id])
            for index in pending_publish:
                error_index = index
                published.append(self._apply_publish(ops[index], undo))
            error_index = None
        except CatalogError as exc:
            for revert in reversed(undo):
                revert()
            self.batches.append(
                {
                    "seq": self._batch_seq,
                    "batch_id": batch_id,
                    "ops": len(ops),
                    "ok": False,
                    "published": [],
                    "error": exc.code,
                }
            )
            raise BatchError(batch_id, exc, error_index) from exc
        self.batches.append(
            {
                "seq": self._batch_seq,
                "batch_id": batch_id,
                "ops": len(ops),
                "ok": True,
                "published": list(published),
                "error": None,
            }
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "applied": len(ops),
            "published": published,
        }

    @staticmethod
    def _check_op(raw):
        if not isinstance(raw, dict):
            raise InvalidOp("op must be an object")
        kind = raw.get("op")
        if kind not in _OP_KINDS:
            raise InvalidOp("unknown op %r" % (kind,), op=kind)
        return raw

    # ------------------------------------------------------------------
    # ops
    # ------------------------------------------------------------------

    def _apply_add_booklet(self, op, undo):
        booklet_id = _require_str(op.get("booklet"), "booklet")
        width = op.get("slot_width")
        if not _is_int(width) or not 1 <= width <= MAX_SLOT_WIDTH:
            raise InvalidOp(
                "slot_width must be an int in 1..%d" % MAX_SLOT_WIDTH,
                field="slot_width",
            )
        if booklet_id in self._booklets:
            raise BookletExists(
                "booklet %r already exists" % booklet_id, booklet=booklet_id
            )
        self._booklets[booklet_id] = _Booklet(booklet_id, width)
        undo.append(lambda: self._booklets.pop(booklet_id, None))

    def _apply_reserve(self, booklet, op, undo):
        has_count = "count" in op
        has_slots = "slots" in op
        if has_count == has_slots:
            raise InvalidOp("reserve needs exactly one of 'count' or 'slots'")
        added = []

        def revert():
            for slot_no in added:
                booklet.slots.pop(slot_no, None)
                if slot_no < booklet.alloc_hint:
                    booklet.alloc_hint = slot_no

        undo.append(revert)
        if has_count:
            count = op["count"]
            if not _is_int(count) or count < 1:
                raise InvalidOp("count must be a positive int", field="count")
            for _ in range(count):
                added.append(self._alloc_slot(booklet))
        else:
            slots = op["slots"]
            if not isinstance(slots, list) or not slots:
                raise InvalidOp("slots must be a non-empty list", field="slots")
            seen = set()
            for slot_no in slots:
                self._check_slot_no(booklet, slot_no)
                if slot_no in seen:
                    raise InvalidOp(
                        "duplicate slot %d in reserve" % slot_no, slot=slot_no
                    )
                seen.add(slot_no)
                if slot_no in booklet.slots:
                    raise SlotOccupied(
                        "slot %d already exists" % slot_no, slot=slot_no
                    )
                added.append(slot_no)
            for slot_no in added:
                booklet.slots[slot_no] = {"state": RESERVED, "part": None}

    def _apply_register(self, booklet, op, undo):
        part_no = _require_str(op.get("part"), "part")
        if part_no in booklet.parts:
            raise PartExists(
                "part %r already registered in booklet %r"
                % (part_no, booklet.booklet_id),
                part=part_no,
                booklet=booklet.booklet_id,
            )
        title = _optional_str(op.get("title"), "title")
        body = _optional_str(op.get("body"), "body")
        attachments = op.get("attachments", [])
        if not isinstance(attachments, list) or any(
            not isinstance(a, str) for a in attachments
        ):
            raise InvalidOp(
                "attachments must be a list of strings", field="attachments"
            )
        approved = op.get("approved", False)
        if not isinstance(approved, bool):
            raise InvalidOp("approved must be a boolean", field="approved")
        if op.get("slot") is not None:
            slot_no = op["slot"]
            self._check_slot_no(booklet, slot_no)
            rec = booklet.slots.get(slot_no)
            if rec is None:
                raise SlotNotReserved(
                    "slot %d was never reserved; a first appearance may only "
                    "claim a previously reserved slot" % slot_no,
                    slot=slot_no,
                )
            if rec["state"] != RESERVED:
                raise SlotOccupied(
                    "slot %d is already taken by part %r" % (slot_no, rec["part"]),
                    slot=slot_no,
                )
            prior = rec

            def revert():
                booklet.slots[slot_no] = prior
                booklet.parts.pop(part_no, None)

            undo.append(revert)
            booklet.slots[slot_no] = {"state": OCCUPIED, "part": part_no}
        else:
            slot_no = self._alloc_slot(booklet)

            def revert():
                booklet.slots.pop(slot_no, None)
                booklet.parts.pop(part_no, None)
                if slot_no < booklet.alloc_hint:
                    booklet.alloc_hint = slot_no

            undo.append(revert)
            booklet.slots[slot_no] = {"state": OCCUPIED, "part": part_no}
        booklet.parts[part_no] = {
            "slot": slot_no,
            "title": title,
            "body": body,
            "attachments": list(attachments),
            "withdrawn": False,
            "approved": approved,
        }

    def _apply_amend(self, booklet, op, undo):
        part = self._get_part(booklet, op.get("part"))
        attachment = _require_str(op.get("attachment"), "attachment")
        part["attachments"].append(attachment)
        undo.append(lambda: part["attachments"].pop())

    def _apply_withdraw(self, booklet, op, undo):
        part_no = _require_str(op.get("part"), "part")
        part = self._get_part(booklet, part_no)
        if part["withdrawn"]:
            raise AlreadyWithdrawn(
                "part %r already withdrawn" % part_no, part=part_no
            )
        slot_no = part["slot"]

        def revert():
            part["withdrawn"] = False
            booklet.slots[slot_no]["state"] = OCCUPIED

        undo.append(revert)
        part["withdrawn"] = True
        booklet.slots[slot_no]["state"] = TOMBSTONE

    def _apply_set_review(self, booklet, op, undo):
        part = self._get_part(booklet, op.get("part"))
        approved = op.get("approved")
        if not isinstance(approved, bool):
            raise InvalidOp("approved must be a boolean", field="approved")
        old = part["approved"]
        if old == approved:
            return
        part["approved"] = approved

        def revert():
            part["approved"] = old

        undo.append(revert)

    def _apply_replace(self, booklet, op, undo):
        old = _require_str(op.get("old"), "old")
        new = _require_str(op.get("new"), "new")
        # Both parties must already occupy slots; a reserved-but-empty slot has
        # no part and therefore can never be linked into a replacement chain.
        self._get_part(booklet, old)
        self._get_part(booklet, new)
        prev = booklet.edges.get(old, _MISSING)
        booklet_id = booklet.booklet_id

        def revert():
            if prev is _MISSING:
                booklet.edges.pop(old, None)
            else:
                booklet.edges[old] = prev
            self._invalidate(booklet_id)

        undo.append(revert)
        booklet.edges[old] = new
        self._invalidate(booklet_id)

    def _apply_unreplace(self, booklet, op, undo):
        old = _require_str(op.get("old"), "old")
        if old not in booklet.edges:
            raise EdgeNotFound(
                "part %r has no replacement edge" % old, part=old
            )
        prev = booklet.edges.pop(old)
        booklet_id = booklet.booklet_id

        def revert():
            booklet.edges[old] = prev
            self._invalidate(booklet_id)

        undo.append(revert)
        self._invalidate(booklet_id)

    def _apply_publish(self, op, undo):
        version_id = op.get("version")
        if version_id is None:
            version_id = "V%d" % (self._version_seq + 1)
        _require_str(version_id, "version")
        if version_id in self.versions:
            raise DuplicateVersion(
                "version %r already published" % version_id, version=version_id
            )
        self._version_seq += 1
        records = {}
        for booklet_id, booklet in self._booklets.items():
            table = {}
            for slot_no in sorted(booklet.slots):
                slot = booklet.slots[slot_no]
                if slot["state"] != OCCUPIED:
                    continue  # reserved placeholders and tombstones are not listed
                part_no = slot["part"]
                if part_no in booklet.edges:
                    continue  # replaced parts have exited the visible catalog
                part = booklet.parts[part_no]
                table[slot_no] = {"part": part_no, "title": part["title"]}
            records[booklet_id] = table
        self.versions[version_id] = {"seq": self._version_seq, "records": records}

        def revert():
            del self.versions[version_id]
            self._version_seq -= 1

        undo.append(revert)
        return version_id

    # ------------------------------------------------------------------
    # replacement graph
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_replacement_graph(booklet):
        edges = booklet.edges
        color = {}  # part_no -> 1 (on current walk) / 2 (settled)
        for start in edges:
            if color.get(start) == 2:
                continue
            path = []
            node = start
            while node is not None and color.get(node) is None:
                color[node] = 1
                path.append(node)
                node = edges.get(node)
            if node is not None and color.get(node) == 1:
                cycle = path[path.index(node):] + [node]
                raise ReplacementCycle(
                    "replacement cycle: " + " -> ".join(cycle), cycle=cycle
                )
            for item in path:
                color[item] = 2

    def resolve_endpoint(self, booklet_id, part_no):
        """Follow replacement edges (via the edge index) to the chain endpoint.

        Results are memoized per booklet and the memo is dropped whenever an
        edge of that booklet changes, so flipping one edge is O(1) in stored
        state and re-resolution is lazy.
        """
        booklet = self._get_booklet(booklet_id)
        if part_no not in booklet.parts:
            raise PartNotFound(
                "part %r is not registered in booklet %r" % (part_no, booklet_id),
                part=part_no,
                booklet=booklet_id,
            )
        cache = self._endpoint_cache.setdefault(booklet_id, {})
        path = []
        node = part_no
        limit = len(booklet.edges) + 1
        while node not in cache:
            nxt = booklet.edges.get(node)
            if nxt is None:
                break
            path.append(node)
            node = nxt
            self.follow_steps += 1
            if len(path) > limit:  # defensive: batches already reject cycles
                raise ReplacementCycle(
                    "replacement cycle detected while resolving",
                    booklet=booklet_id,
                )
        end = cache.get(node, node)
        cache.setdefault(node, end)
        for item in path:
            cache[item] = end
        return end

    # ------------------------------------------------------------------
    # read views
    # ------------------------------------------------------------------

    def query_slot(self, version_id, booklet_id, slot):
        """Stable-slot query.

        Returns the identity printed at ``slot`` in paper version
        ``version_id``, plus the *current* replacement endpoint and either its
        public value or the reason it is not public.  Only the endpoint
        identity is reported, never the intermediate chain.
        """
        version = self.versions.get(version_id)
        if version is None:
            raise UnknownVersion(
                "unknown version %r" % (version_id,), version=version_id
            )
        booklet = self._get_booklet(booklet_id)
        slot_no = self._parse_slot(booklet, slot)
        printed = version["records"].get(booklet_id, {}).get(slot_no)
        if printed is None:
            raise SlotNotPrinted(
                "slot %s was not formally listed in version %r of booklet %r"
                % (self.slot_label(booklet_id, slot_no), version_id, booklet_id),
                version=version_id,
                booklet=booklet_id,
                slot=slot_no,
            )
        part_no = printed["part"]
        endpoint = self.resolve_endpoint(booklet_id, part_no)
        ep = booklet.parts[endpoint]
        current = {
            "replaced": endpoint != part_no,
            "endpoint": {
                "part": endpoint,
                "slot": ep["slot"],
                "slot_label": self.slot_label(booklet_id, ep["slot"]),
                "title": ep["title"],
            },
        }
        if ep["withdrawn"]:
            current["status"] = "not_public"
            current["reason"] = "withdrawn"
        elif not ep["approved"]:
            current["status"] = "not_public"
            current["reason"] = "not_approved"
        else:
            current["status"] = "public"
            current["value"] = {
                "body": ep["body"],
                "attachments": list(ep["attachments"]),
            }
        return {
            "ok": True,
            "version": version_id,
            "booklet": booklet_id,
            "slot": slot_no,
            "slot_label": self.slot_label(booklet_id, slot_no),
            "printed": {"part": part_no, "title": printed["title"]},
            "current": current,
        }

    def _parse_slot(self, booklet, slot):
        if _is_int(slot):
            slot_no = slot
        elif isinstance(slot, str) and slot.isdigit():
            slot_no = int(slot)
        else:
            raise InvalidOp(
                "slot must be an integer or a zero-padded digit string",
                field="slot",
            )
        if slot_no < 0 or slot_no >= booklet.capacity:
            raise SlotOutOfRange(
                "slot %r outside 0..%d for booklet %r"
                % (slot, booklet.capacity - 1, booklet.booklet_id),
                slot=slot,
                booklet=booklet.booklet_id,
            )
        return slot_no

    def search_export(self, booklet_id=None):
        """Current visible catalog for the search side.

        Every approved, non-withdrawn visible endpoint appears exactly once,
        marked with the stable slot it owns.  Replaced parts and tombstones
        are excluded.  This traversal only happens when explicitly requested.
        """
        if booklet_id is None:
            booklets = [self._booklets[k] for k in sorted(self._booklets)]
        else:
            booklets = [self._get_booklet(booklet_id)]
        entries = []
        for booklet in booklets:
            for part_no, part in booklet.parts.items():
                if part["withdrawn"] or not part["approved"]:
                    continue
                if part_no in booklet.edges:
                    continue  # replaced -> not a visible endpoint
                entries.append(
                    {
                        "booklet": booklet.booklet_id,
                        "slot": part["slot"],
                        "slot_label": self.slot_label(
                            booklet.booklet_id, part["slot"]
                        ),
                        "part": part_no,
                        "title": part["title"],
                        "value": {
                            "body": part["body"],
                            "attachments": list(part["attachments"]),
                        },
                    }
                )
        entries.sort(key=lambda e: (e["booklet"], e["slot"]))
        return {
            "ok": True,
            "type": "search_export",
            "count": len(entries),
            "entries": entries,
        }

    def part_info(self, booklet_id, part_no):
        """Introspection helper: current standing of one part."""
        booklet = self._get_booklet(booklet_id)
        part = self._get_part(booklet, part_no)
        return {
            "booklet": booklet_id,
            "part": part_no,
            "slot": part["slot"],
            "slot_label": self.slot_label(booklet_id, part["slot"]),
            "title": part["title"],
            "withdrawn": part["withdrawn"],
            "approved": part["approved"],
            "attachments": len(part["attachments"]),
            "replaced_by": booklet.edges.get(part_no),
            "endpoint": self.resolve_endpoint(booklet_id, part_no),
        }

    # ------------------------------------------------------------------
    # convenience wrappers (all mutations go through apply_batch)
    # ------------------------------------------------------------------

    def add_booklet(self, booklet_id, slot_width):
        return self.apply_batch(
            [
                {
                    "op": "add_booklet",
                    "booklet": booklet_id,
                    "slot_width": slot_width,
                }
            ]
        )

    def publish_version(self, version_id=None):
        op = {"op": "publish"}
        if version_id is not None:
            op["version"] = version_id
        return self.apply_batch([op])["published"][0]

    # ------------------------------------------------------------------
    # persistence (deterministic JSON documents)
    # ------------------------------------------------------------------

    def save(self):
        """Serialize the whole catalog to a JSON-able document."""
        booklets = {}
        for booklet_id, booklet in self._booklets.items():
            booklets[booklet_id] = {
                "slot_width": booklet.slot_width,
                "slots": {
                    str(slot_no): {"state": slot["state"], "part": slot["part"]}
                    for slot_no, slot in sorted(booklet.slots.items())
                },
                "parts": {
                    part_no: {
                        "slot": part["slot"],
                        "title": part["title"],
                        "body": part["body"],
                        "attachments": list(part["attachments"]),
                        "withdrawn": part["withdrawn"],
                        "approved": part["approved"],
                    }
                    for part_no, part in sorted(booklet.parts.items())
                },
                "edges": dict(sorted(booklet.edges.items())),
            }
        versions = {}
        for version_id, version in self.versions.items():
            versions[version_id] = {
                "seq": version["seq"],
                "records": {
                    booklet_id: {
                        str(slot_no): {"part": rec["part"], "title": rec["title"]}
                        for slot_no, rec in sorted(table.items())
                    }
                    for booklet_id, table in version["records"].items()
                },
            }
        return {
            "format": STATE_FORMAT,
            "booklets": booklets,
            "versions": versions,
            "batches": [dict(batch) for batch in self.batches],
            "counters": {
                "batch_seq": self._batch_seq,
                "version_seq": self._version_seq,
            },
        }

    @classmethod
    def load(cls, data):
        """Rebuild a catalog from :meth:`save` output."""
        if not isinstance(data, dict) or data.get("format") != STATE_FORMAT:
            raise InvalidOp("not a %s document" % STATE_FORMAT)
        cat = cls()
        for booklet_id, bd in data["booklets"].items():
            booklet = _Booklet(booklet_id, bd["slot_width"])
            for slot_str, slot in bd["slots"].items():
                booklet.slots[int(slot_str)] = {
                    "state": slot["state"],
                    "part": slot["part"],
                }
            for part_no, rec in bd["parts"].items():
                booklet.parts[part_no] = {
                    "slot": rec["slot"],
                    "title": rec["title"],
                    "body": rec["body"],
                    "attachments": list(rec["attachments"]),
                    "withdrawn": bool(rec["withdrawn"]),
                    "approved": bool(rec["approved"]),
                }
            booklet.edges = dict(bd["edges"])
            hint = 0
            while hint in booklet.slots:
                hint += 1
            booklet.alloc_hint = hint
            cat._booklets[booklet_id] = booklet
        for version_id, vd in data["versions"].items():
            records = {}
            for booklet_id, table in vd["records"].items():
                records[booklet_id] = {
                    int(slot_str): {"part": rec["part"], "title": rec["title"]}
                    for slot_str, rec in table.items()
                }
            cat.versions[version_id] = {"seq": vd["seq"], "records": records}
        cat.batches = [dict(batch) for batch in data.get("batches", [])]
        counters = data.get("counters", {})
        cat._batch_seq = counters.get("batch_seq", len(cat.batches))
        cat._version_seq = counters.get("version_seq", len(cat.versions))
        return cat
