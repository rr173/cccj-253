"""Offline sync channel (离线同步通道) between a catalog source and mirrors.

A disclosure mirror cannot hold the cataloging drafts and cannot re-fetch
every public body after each correction.  This module is the file-based
channel that keeps such a mirror current:

* :class:`SyncSource` opens on top of an existing ``slot-catalog-state/1``
  state and accepts the usual correction batches (paper editions included),
  reusing :meth:`Catalog.apply_batch` wholesale -- batch-atomic results and
  publish semantics are exactly the catalog's.  Every *successful* batch
  yields a new checkpoint (even when no public row changed); a failed batch
  advances nothing.  A checkpoint ``{"source", "seq"}`` names the sync
  source (a digest of the state the channel was opened from, never a
  user-filled batch id) and the count of successful batches on it.
* The mirror dataset is two tables.  ``docs`` is keyed by
  ``(booklet, part)`` and holds one search-export entry per public
  endpoint; ``entries`` is keyed by ``(version, booklet, slot)`` -- one row
  per formally printed slot whose *current* endpoint is public -- and holds
  only the endpoint key, so a body is stored once no matter how many paper
  indexes point at it.  An entry never falls back to an older chain member:
  when the endpoint stops being public the entry is gone.
* Packages are deterministic JSON documents.  A ``full`` package carries
  both tables at one checkpoint; a ``delta`` package carries only the net
  row changes between two checkpoints of the same source (deletes carry
  keys only, upserts carry target values only; rows that were made public
  and un-public again inside the window, or changed and changed back, are
  not sent).  Every package is content-addressed by a SHA-256 digest so a
  damaged or incompletely received copy is rejected, not half-installed.

Performance shape: the source may build its indexes once at open and may
process a whole new paper edition at publish; everything else is
incremental.  A correction batch touches only the docs of the parts it
names and the entries of the chains it actually affects (found through
reverse indexes), never re-walking unaffected historical editions or
scanning unrelated parts, and none of that work lives in the catalog's
batch write path.  ``SyncSource.stats`` instruments the per-batch
recompute counts so tests can assert on it.
"""

from __future__ import annotations

import hashlib
import json

from .catalog import STATE_FORMAT, Catalog
from .errors import (
    BatchError,
    CatalogError,
    CheckpointMismatch,
    EntryNotFound,
    InvalidOp,
    InvalidPackage,
    UnknownCheckpoint,
    WrongSource,
)

SOURCE_FORMAT = "slot-catalog-sync-source/1"
MIRROR_FORMAT = "slot-catalog-mirror/1"
PACKAGE_FORMAT = "slot-catalog-sync-package/1"

_HEXDIGITS = frozenset("0123456789abcdef")


def _canonical(doc):
    """Deterministic byte encoding used for digests and source ids."""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(doc):
    return hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_checkpoint(value, field):
    if (
        not isinstance(value, dict)
        or set(value) != {"source", "seq"}
        or not isinstance(value["source"], str)
        or len(value["source"]) != 64
        or any(ch not in _HEXDIGITS for ch in value["source"])
        or not _is_int(value["seq"])
        or value["seq"] < 0
    ):
        raise InvalidPackage(
            "%s must be a checkpoint object {'source': <64 hex>, 'seq': >=0}"
            % field,
            field=field,
        )
    return {"source": value["source"], "seq": value["seq"]}


# ---------------------------------------------------------------------------
# sync source
# ---------------------------------------------------------------------------


class SyncSource:
    """A single-writer sync source opened over a catalog state.

    The source owns the catalog drafts; mirrors only ever see packages.
    ``seq`` counts successful batches since the channel was opened and is
    the position half of every checkpoint this source issues.
    """

    def __init__(self):
        self.catalog = None
        self.source_id = None
        self.seq = 0
        self.docs = {}  # (booklet, part) -> search-export row
        self.entries = {}  # (version, booklet, slot) -> {"booklet","part"}
        self.printed = {}  # (booklet, part) -> set((version, slot))
        self.rev_edges = {}  # booklet -> {new_part: set(old_part)}
        self.changelog = []  # changelog[i]: row changes of batch i -> i+1
        self.stats = {"docs_recomputed": 0, "entries_recomputed": 0}

    # ------------------------------------------------------------------
    # open / persist
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, state_doc):
        """Open a fresh sync channel over a ``slot-catalog-state/1`` doc.

        The source id is a digest of the opening state, so two different
        catalogs never share a checkpoint namespace and reopening the same
        state deterministically yields the same source id.  Indexes and the
        mirror dataset are built once here, which is explicitly allowed.
        """
        if not isinstance(state_doc, dict) or state_doc.get("format") != STATE_FORMAT:
            raise InvalidOp("sync can only open a %s document" % STATE_FORMAT)
        src = cls()
        src.catalog = Catalog.load(state_doc)
        src.source_id = _digest(state_doc)
        src._rebuild_dataset()
        return src

    def _rebuild_dataset(self):
        """Full derivation of both tables plus the reverse indexes.

        Runs only at open/resume (never per batch): docs come from the
        search export, entries from resolving every formally printed slot.
        """
        cat = self.catalog
        self.docs = {}
        for row in cat.search_export()["entries"]:
            self.docs[(row["booklet"], row["part"])] = row
        self.entries = {}
        self.printed = {}
        for version_id, version in cat.versions.items():
            for booklet_id, table in version["records"].items():
                for slot_no, rec in table.items():
                    part_no = rec["part"]
                    self.printed.setdefault((booklet_id, part_no), set()).add(
                        (version_id, slot_no)
                    )
                    endpoint = self._public_endpoint(booklet_id, part_no)
                    if endpoint is not None:
                        self.entries[(version_id, booklet_id, slot_no)] = endpoint
        self.rev_edges = {}
        for booklet_id, booklet in cat._booklets.items():
            rev = {}
            for old, new in booklet.edges.items():
                rev.setdefault(new, set()).add(old)
            self.rev_edges[booklet_id] = rev

    def save(self):
        """Serialize source + channel state as one consistent document."""
        changelog = []
        for changes in self.changelog:
            changelog.append(
                {
                    "docs": [
                        [[b, p], old, new]
                        for (b, p), (old, new) in sorted(changes["docs"].items())
                    ],
                    "entries": [
                        [[v, b, s], old, new]
                        for (v, b, s), (old, new) in sorted(
                            changes["entries"].items()
                        )
                    ],
                }
            )
        return {
            "format": SOURCE_FORMAT,
            "catalog": self.catalog.save(),
            "sync": {
                "source": self.source_id,
                "seq": self.seq,
                "changelog": changelog,
            },
        }

    @classmethod
    def load(cls, doc):
        """Resume a source; accepted batches and checkpoints stay consistent.

        The catalog and the changelog are restored from the same document;
        the dataset and indexes are rebuilt from the catalog (allowed at
        open), so a resumed source answers delta requests exactly as before.
        """
        if not isinstance(doc, dict) or doc.get("format") != SOURCE_FORMAT:
            raise InvalidOp("not a %s document" % SOURCE_FORMAT)
        sync = doc.get("sync")
        if not isinstance(sync, dict):
            raise InvalidOp("sync source is missing its 'sync' section")
        src = cls.open(doc.get("catalog"))
        source_id = sync.get("source")
        seq = sync.get("seq")
        raw_log = sync.get("changelog")
        if (
            not isinstance(source_id, str)
            or len(source_id) != 64
            or any(ch not in _HEXDIGITS for ch in source_id)
        ):
            raise InvalidOp("sync source id must be 64 hex characters")
        if not _is_int(seq) or seq < 0:
            raise InvalidOp("sync seq must be a non-negative integer")
        if not isinstance(raw_log, list) or len(raw_log) != seq:
            raise InvalidOp("sync changelog does not match the checkpoint seq")
        changelog = []
        for position, changes in enumerate(raw_log):
            if not isinstance(changes, dict):
                raise InvalidOp("sync changelog entry #%d is malformed" % position)
            try:
                docs = {}
                for triple in changes.get("docs", []):
                    key, old, new = triple
                    docs[(key[0], key[1])] = (old, new)
                entries = {}
                for triple in changes.get("entries", []):
                    key, old, new = triple
                    entries[(key[0], key[1], key[2])] = (old, new)
            except (TypeError, IndexError, ValueError) as exc:
                raise InvalidOp(
                    "sync changelog entry #%d is malformed: %r" % (position, exc)
                )
            changelog.append({"docs": docs, "entries": entries})
        src.source_id = source_id
        src.seq = seq
        src.changelog = changelog
        return src

    # ------------------------------------------------------------------
    # checkpoints
    # ------------------------------------------------------------------

    def checkpoint(self):
        return {"source": self.source_id, "seq": self.seq}

    # ------------------------------------------------------------------
    # batches (catalog write path is untouched; maintenance happens after)
    # ------------------------------------------------------------------

    def apply_batch(self, ops, batch_id=None):
        """Apply one correction batch and advance the checkpoint on success.

        Batch-atomic results and publish semantics are the catalog's own;
        a failed batch leaves the checkpoint and both tables untouched.
        """
        edge_ops = []
        if isinstance(ops, list):
            for raw in ops:
                if isinstance(raw, dict) and raw.get("op") in ("replace", "unreplace"):
                    old = raw.get("old")
                    booklet_id = raw.get("booklet")
                    booklet = self.catalog._booklets.get(booklet_id)
                    prev = None
                    if booklet is not None and isinstance(old, str):
                        prev = booklet.edges.get(old)
                    edge_ops.append((raw, booklet_id, old, prev))
        result = self.catalog.apply_batch(ops, batch_id=batch_id)
        changes = self._maintain(ops, edge_ops, result["published"])
        self.changelog.append(changes)
        self.seq += 1
        return result

    def _maintain(self, ops, edge_ops, published):
        """Fold one accepted batch into both tables; return the row changes.

        Only the parts the batch names and the chains hanging off them are
        reconsidered: dirty parts come from the ops, affected chains from
        the reverse edge index, printed entries from the printed-slot
        index.  Unaffected historical entries and unrelated parts are never
        re-walked.
        """
        self.stats = {"docs_recomputed": 0, "entries_recomputed": 0}
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
        # keep the reverse edge index in step with the accepted batch
        for raw, booklet_id, old, prev in edge_ops:
            rev = self.rev_edges.setdefault(booklet_id, {})
            if prev is not None and prev in rev:
                rev[prev].discard(old)
            if raw["op"] == "replace":
                rev.setdefault(raw["new"], set()).add(old)
        # affected = dirty parts plus everything upstream of them
        affected = set(dirty)
        stack = list(dirty)
        while stack:
            key = stack.pop()
            for pred in self.rev_edges.get(key[0], {}).get(key[1], ()):
                pred_key = (key[0], pred)
                if pred_key not in affected:
                    affected.add(pred_key)
                    stack.append(pred_key)
        changes = {"docs": {}, "entries": {}}
        # docs: only the parts the batch itself named can start or stop
        # being public endpoints (endpoint-ness turns on a part's own edge)
        for key in dirty:
            booklet_id, part_no = key
            old = self.docs.get(key)
            new = self._doc_row(booklet_id, part_no)
            self.stats["docs_recomputed"] += 1
            if old != new:
                changes["docs"][key] = (old, new)
                if new is None:
                    del self.docs[key]
                else:
                    self.docs[key] = new
        # entries: only printed slots whose chain the batch touched
        entry_keys = set()
        for key in affected:
            for version_id, slot_no in self.printed.get(key, ()):
                entry_keys.add((version_id, key[0], slot_no))
        for key in entry_keys:
            old = self.entries.get(key)
            new = self._entry_value(key)
            self.stats["entries_recomputed"] += 1
            if old != new:
                changes["entries"][key] = (old, new)
                if new is None:
                    del self.entries[key]
                else:
                    self.entries[key] = new
        # a new paper edition may be indexed in full
        for version_id in published:
            self._index_published_version(version_id, changes)
        return changes

    def _doc_row(self, booklet_id, part_no):
        """The docs-table row for a part, or None when it is not a public
        endpoint (replaced, withdrawn, unapproved or unknown)."""
        booklet = self.catalog._booklets.get(booklet_id)
        if booklet is None:
            return None
        part = booklet.parts.get(part_no)
        if part is None:
            return None
        if part["withdrawn"] or not part["approved"]:
            return None
        if part_no in booklet.edges:
            return None  # replaced parts are not visible endpoints
        return {
            "booklet": booklet_id,
            "slot": part["slot"],
            "slot_label": self.catalog.slot_label(booklet_id, part["slot"]),
            "part": part_no,
            "title": part["title"],
            "value": {
                "body": part["body"],
                "attachments": list(part["attachments"]),
            },
        }

    def _public_endpoint(self, booklet_id, part_no):
        """Endpoint key of a printed part, or None when it is not public."""
        return self.catalog.public_endpoint_key(booklet_id, part_no)

    def _entry_value(self, key):
        version_id, booklet_id, slot_no = key
        version = self.catalog.versions.get(version_id)
        if version is None:
            return None
        record = version["records"].get(booklet_id, {}).get(slot_no)
        if record is None:
            return None
        return self._public_endpoint(booklet_id, record["part"])

    def _index_published_version(self, version_id, changes):
        """Index every printed slot of a freshly published paper edition."""
        version = self.catalog.versions[version_id]
        for booklet_id, table in version["records"].items():
            for slot_no, rec in table.items():
                part_no = rec["part"]
                self.printed.setdefault((booklet_id, part_no), set()).add(
                    (version_id, slot_no)
                )
                key = (version_id, booklet_id, slot_no)
                new = self._public_endpoint(booklet_id, part_no)
                self.stats["entries_recomputed"] += 1
                if new is not None:
                    changes["entries"][key] = (None, new)
                    self.entries[key] = new

    # ------------------------------------------------------------------
    # packages
    # ------------------------------------------------------------------

    def full_package(self):
        """Both tables in full at the current checkpoint (the first package)."""
        rows = {
            "docs": {"upsert": _nest_docs(self.docs), "delete": {}},
            "entries": {"upsert": _nest_entries(self.entries), "delete": {}},
        }
        return _make_package("full", None, self.checkpoint(), rows)

    def delta_package(self, from_seq, from_source=None):
        """Net row changes from one of this source's checkpoints to now.

        The window is folded from the per-checkpoint changelog: a row
        carries its value at ``from_seq`` and its value now, so anything
        published and un-published inside the window, or changed and
        changed back, simply never appears.  An empty net change still
        yields a package, so a mirror can follow the checkpoint.
        """
        if from_source is not None and from_source != self.source_id:
            raise UnknownCheckpoint(
                "checkpoint source %r is not this source" % from_source,
                source=from_source,
            )
        if not _is_int(from_seq) or from_seq < 0 or from_seq > self.seq:
            raise UnknownCheckpoint(
                "checkpoint seq %r is not on this source's timeline (0..%d)"
                % (from_seq, self.seq),
                seq=from_seq,
            )
        # fold the window: first touch gives the value at from_seq, last
        # touch the value now; a None half means "row absent" on that side
        folded = {"docs": {}, "entries": {}}
        for changes in self.changelog[from_seq:]:
            for table in ("docs", "entries"):
                for key, (old, new) in changes[table].items():
                    if key in folded[table]:
                        folded[table][key][1] = new
                    else:
                        folded[table][key] = [old, new]
        docs_upsert = {}
        docs_delete = {}
        for (booklet_id, part_no), (old, new) in folded["docs"].items():
            if old == new:
                continue  # changed and changed back inside the window
            if new is None:
                docs_delete.setdefault(booklet_id, []).append(part_no)
            else:
                docs_upsert.setdefault(booklet_id, {})[part_no] = new
        entries_upsert = {}
        entries_delete = {}
        for (version_id, booklet_id, slot_no), (old, new) in folded[
            "entries"
        ].items():
            if old == new:
                continue
            if new is None:
                entries_delete.setdefault(version_id, {}).setdefault(
                    booklet_id, []
                ).append(slot_no)
            else:
                entries_upsert.setdefault(version_id, {}).setdefault(
                    booklet_id, {}
                )[str(slot_no)] = new
        rows = {
            "docs": {
                "upsert": docs_upsert,
                "delete": {
                    b: sorted(parts) for b, parts in sorted(docs_delete.items())
                },
            },
            "entries": {
                "upsert": entries_upsert,
                "delete": {
                    v: {b: sorted(slots) for b, slots in sorted(by_b.items())}
                    for v, by_b in sorted(entries_delete.items())
                },
            },
        }
        start = {"source": self.source_id, "seq": from_seq}
        return _make_package("delta", start, self.checkpoint(), rows)


def _nest_docs(docs):
    out = {}
    for (booklet_id, part_no), row in docs.items():
        out.setdefault(booklet_id, {})[part_no] = row
    return out


def _nest_entries(entries):
    out = {}
    for (version_id, booklet_id, slot_no), endpoint in entries.items():
        out.setdefault(version_id, {}).setdefault(booklet_id, {})[
            str(slot_no)
        ] = endpoint
    return out


def _make_package(kind, start, stop, rows):
    package = {
        "format": PACKAGE_FORMAT,
        "kind": kind,
        "from": start,
        "to": stop,
        "rows": rows,
    }
    package["sha256"] = _digest(package)
    return package


# ---------------------------------------------------------------------------
# package validation
# ---------------------------------------------------------------------------


def _check_hex64(value, field):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in _HEXDIGITS for ch in value)
    ):
        raise InvalidPackage("%s must be 64 hex characters" % field, field=field)
    return value


def _check_endpoint_key(value, field):
    if (
        not isinstance(value, dict)
        or set(value) != {"booklet", "part"}
        or not isinstance(value["booklet"], str)
        or not value["booklet"]
        or not isinstance(value["part"], str)
        or not value["part"]
    ):
        raise InvalidPackage(
            "%s must be an endpoint key {'booklet', 'part'}" % field, field=field
        )
    return {"booklet": value["booklet"], "part": value["part"]}


def _check_doc_row(value, field):
    if not isinstance(value, dict):
        raise InvalidPackage("%s must be a document row" % field, field=field)
    if set(value) != {"booklet", "slot", "slot_label", "part", "title", "value"}:
        raise InvalidPackage(
            "%s must carry exactly booklet/slot/slot_label/part/title/value"
            % field,
            field=field,
        )
    if (
        not isinstance(value["booklet"], str)
        or not value["booklet"]
        or not isinstance(value["part"], str)
        or not value["part"]
        or not _is_int(value["slot"])
        or value["slot"] < 0
        or not isinstance(value["slot_label"], str)
        or (value["title"] is not None and not isinstance(value["title"], str))
    ):
        raise InvalidPackage("%s has malformed identity fields" % field, field=field)
    body = value["value"]
    if (
        not isinstance(body, dict)
        or set(body) != {"body", "attachments"}
        or (body["body"] is not None and not isinstance(body["body"], str))
        or not isinstance(body["attachments"], list)
        or any(not isinstance(a, str) for a in body["attachments"])
    ):
        raise InvalidPackage("%s has a malformed value" % field, field=field)
    return value


def validate_package(doc):
    """Check a package document end to end; return it normalized.

    Anything damaged or incompletely received -- unparsable caller side,
    wrong tag, missing or mistyped fields, digest mismatch -- raises
    :class:`InvalidPackage` before any mirror state is consulted.
    """
    if not isinstance(doc, dict) or doc.get("format") != PACKAGE_FORMAT:
        raise InvalidPackage("not a %s document" % PACKAGE_FORMAT)
    kind = doc.get("kind")
    if kind not in ("full", "delta"):
        raise InvalidPackage("package kind must be 'full' or 'delta'", kind=kind)
    declared = _check_hex64(doc.get("sha256"), "sha256")
    body = {key: value for key, value in doc.items() if key != "sha256"}
    if _digest(body) != declared:
        raise InvalidPackage(
            "package content digest mismatch (damaged or incomplete package)"
        )
    stop = _check_checkpoint(doc.get("to"), "to")
    if kind == "full":
        if doc.get("from") is not None:
            raise InvalidPackage("a full package must not carry a 'from'")
        start = None
    else:
        start = _check_checkpoint(doc.get("from"), "from")
        if start["source"] != stop["source"]:
            raise InvalidPackage("delta endpoints name different sources")
        if start["seq"] > stop["seq"]:
            raise InvalidPackage("delta goes backwards")
    rows = doc.get("rows")
    if not isinstance(rows, dict) or set(rows) != {"docs", "entries"}:
        raise InvalidPackage("package rows must have exactly 'docs' and 'entries'")
    normalized = {"docs": {"upsert": {}, "delete": {}}, "entries": {"upsert": {}, "delete": {}}}
    docs = rows.get("docs")
    if not isinstance(docs, dict) or set(docs) != {"upsert", "delete"}:
        raise InvalidPackage("docs rows must have exactly 'upsert' and 'delete'")
    for booklet_id, by_part in docs["upsert"].items():
        if not isinstance(booklet_id, str) or not isinstance(by_part, dict):
            raise InvalidPackage("docs upsert must map booklet -> part -> row")
        for part_no, row in by_part.items():
            _check_doc_row(row, "docs upsert row")
            if row["booklet"] != booklet_id or row["part"] != part_no:
                raise InvalidPackage("docs upsert row does not match its key")
            normalized["docs"]["upsert"].setdefault(booklet_id, {})[part_no] = row
    for booklet_id, parts in docs["delete"].items():
        if (
            not isinstance(booklet_id, str)
            or not isinstance(parts, list)
            or any(not isinstance(p, str) for p in parts)
        ):
            raise InvalidPackage("docs delete must map booklet -> [part]")
        if booklet_id in normalized["docs"]["upsert"] and any(
            p in normalized["docs"]["upsert"][booklet_id] for p in parts
        ):
            raise InvalidPackage("the same docs key is both upserted and deleted")
        normalized["docs"]["delete"][booklet_id] = list(parts)
    entries = rows.get("entries")
    if not isinstance(entries, dict) or set(entries) != {"upsert", "delete"}:
        raise InvalidPackage("entries rows must have exactly 'upsert' and 'delete'")
    for version_id, by_booklet in entries["upsert"].items():
        if not isinstance(version_id, str) or not isinstance(by_booklet, dict):
            raise InvalidPackage(
                "entries upsert must map version -> booklet -> slot -> endpoint"
            )
        for booklet_id, by_slot in by_booklet.items():
            if not isinstance(booklet_id, str) or not isinstance(by_slot, dict):
                raise InvalidPackage("entries upsert has a malformed booklet level")
            for slot_str, endpoint in by_slot.items():
                if not isinstance(slot_str, str) or not slot_str.isdigit():
                    raise InvalidPackage("entries upsert slot keys must be digits")
                _check_endpoint_key(endpoint, "entries upsert endpoint")
                if endpoint["booklet"] != booklet_id:
                    raise InvalidPackage(
                        "entries upsert endpoint escapes its booklet"
                    )
                normalized["entries"]["upsert"].setdefault(
                    version_id, {}
                ).setdefault(booklet_id, {})[slot_str] = endpoint
    for version_id, by_booklet in entries["delete"].items():
        if not isinstance(version_id, str) or not isinstance(by_booklet, dict):
            raise InvalidPackage("entries delete must map version -> booklet -> [slot]")
        for booklet_id, slots in by_booklet.items():
            if not isinstance(booklet_id, str) or not isinstance(slots, list):
                raise InvalidPackage("entries delete has a malformed booklet level")
            if any(not _is_int(s) or s < 0 for s in slots):
                raise InvalidPackage("entries delete slots must be non-negative ints")
            upserted = normalized["entries"]["upsert"].get(version_id, {}).get(
                booklet_id, {}
            )
            if any(str(s) in upserted for s in slots):
                raise InvalidPackage(
                    "the same entries key is both upserted and deleted"
                )
            normalized["entries"]["delete"].setdefault(version_id, {})[
                booklet_id
            ] = list(slots)
    return {
        "format": PACKAGE_FORMAT,
        "kind": kind,
        "from": start,
        "to": stop,
        "rows": normalized,
        "sha256": declared,
    }


# ---------------------------------------------------------------------------
# mirror
# ---------------------------------------------------------------------------


class Mirror:
    """The disclosure mirror: two tables plus a checkpoint, nothing else.

    Install and read paths never touch the source's drafts; everything the
    mirror knows arrives inside packages it verified and installed.
    """

    def __init__(self):
        self.source = None
        self.seq = None
        self.docs = {}  # (booklet, part) -> search-export row
        self.entries = {}  # (version, booklet, slot) -> {"booklet","part"}

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def save(self):
        return {
            "format": MIRROR_FORMAT,
            "checkpoint": {"source": self.source, "seq": self.seq},
            "docs": _nest_docs(self.docs),
            "entries": _nest_entries(self.entries),
        }

    @classmethod
    def load(cls, doc):
        if not isinstance(doc, dict) or doc.get("format") != MIRROR_FORMAT:
            raise InvalidOp("not a %s document" % MIRROR_FORMAT)
        checkpoint = doc.get("checkpoint")
        if (
            not isinstance(checkpoint, dict)
            or not isinstance(checkpoint.get("source"), str)
            or not _is_int(checkpoint.get("seq"))
        ):
            raise InvalidOp("mirror checkpoint is malformed")
        mirror = cls()
        mirror.source = checkpoint["source"]
        mirror.seq = checkpoint["seq"]
        for booklet_id, by_part in doc.get("docs", {}).items():
            for part_no, row in by_part.items():
                mirror.docs[(booklet_id, part_no)] = row
        for version_id, by_booklet in doc.get("entries", {}).items():
            for booklet_id, by_slot in by_booklet.items():
                for slot_str, endpoint in by_slot.items():
                    mirror.entries[(version_id, booklet_id, int(slot_str))] = endpoint
        return mirror

    def checkpoint(self):
        return {"source": self.source, "seq": self.seq}

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self, package_doc):
        """Install one package; return a report.

        Both tables and the checkpoint move to the package's target as one
        step.  Re-installing the package that brought the mirror here is a
        no-op; packages from another source, packages that do not start
        from the current checkpoint, and damaged or incomplete packages are
        rejected with the installed data kept.
        """
        package = validate_package(package_doc)
        stop = package["to"]
        if self.source is None:
            if package["kind"] != "full":
                raise CheckpointMismatch(
                    "the first package a mirror installs must be a full package",
                    checkpoint=None,
                )
        else:
            if stop["source"] != self.source:
                raise WrongSource(
                    "package is from source %s, this mirror follows %s"
                    % (stop["source"], self.source),
                    expected=self.source,
                    actual=stop["source"],
                )
            if stop["seq"] == self.seq:
                # re-installing the package that brought us here changes
                # nothing: the target state is already installed
                return {
                    "ok": True,
                    "installed": False,
                    "reason": "already_installed",
                    "kind": package["kind"],
                    "checkpoint": self.checkpoint(),
                    "docs": len(self.docs),
                    "entries": len(self.entries),
                }
            if package["kind"] == "delta":
                start = package["from"]
                if start["seq"] != self.seq:
                    raise CheckpointMismatch(
                        "delta starts at seq %d but the mirror is at seq %d"
                        % (start["seq"], self.seq),
                        expected=self.seq,
                        actual=start["seq"],
                    )
            elif stop["seq"] < self.seq:
                raise CheckpointMismatch(
                    "full package targets seq %d but the mirror is at seq %d"
                    % (stop["seq"], self.seq),
                    expected=self.seq,
                    actual=stop["seq"],
                )
        docs = dict(self.docs)
        entries = dict(self.entries)
        if package["kind"] == "full":
            docs = {}
            entries = {}
        rows = package["rows"]
        for booklet_id, parts in rows["docs"]["delete"].items():
            for part_no in parts:
                docs.pop((booklet_id, part_no), None)
        for booklet_id, by_part in rows["docs"]["upsert"].items():
            for part_no, row in by_part.items():
                docs[(booklet_id, part_no)] = row
        for version_id, by_booklet in rows["entries"]["delete"].items():
            for booklet_id, slots in by_booklet.items():
                for slot_no in slots:
                    entries.pop((version_id, booklet_id, slot_no), None)
        for version_id, by_booklet in rows["entries"]["upsert"].items():
            for booklet_id, by_slot in by_booklet.items():
                for slot_str, endpoint in by_slot.items():
                    entries[(version_id, booklet_id, int(slot_str))] = endpoint
        # no entry may point at a document the mirror does not hold
        dangling = sorted(
            key for key, target in entries.items() if (target["booklet"], target["part"]) not in docs
        )
        if dangling:
            raise InvalidPackage(
                "package would leave %d entr%s pointing at missing documents"
                % (len(dangling), "y" if len(dangling) == 1 else "ies"),
                dangling=[list(key) for key in dangling[:5]],
            )
        self.docs = docs
        self.entries = entries
        self.source = stop["source"]
        self.seq = stop["seq"]
        return {
            "ok": True,
            "installed": True,
            "kind": package["kind"],
            "checkpoint": self.checkpoint(),
            "docs": len(self.docs),
            "entries": len(self.entries),
        }

    # ------------------------------------------------------------------
    # read views (served from the two tables only)
    # ------------------------------------------------------------------

    def query(self, version_id, booklet_id, slot):
        """Resolve a printed index (version, booklet, slot) on the mirror.

        A live entry yields its endpoint key and the current public
        document; a lapsed or never-printed slot yields ``entry_not_found``
        and never falls back to stale cached content.
        """
        if isinstance(slot, str) and slot.isdigit():
            slot_no = int(slot)
        elif _is_int(slot) and slot >= 0:
            slot_no = slot
        else:
            raise InvalidOp(
                "slot must be an integer or a zero-padded digit string",
                field="slot",
            )
        key = (version_id, booklet_id, slot_no)
        endpoint = self.entries.get(key)
        if endpoint is None:
            raise EntryNotFound(
                "no live entry for version %r booklet %r slot %r"
                % (version_id, booklet_id, slot),
                version=version_id,
                booklet=booklet_id,
                slot=slot_no,
            )
        doc = self.docs.get((endpoint["booklet"], endpoint["part"]))
        if doc is None:  # install rejects such packages; stay defensive
            raise EntryNotFound(
                "entry for version %r booklet %r slot %r points at a missing "
                "document" % (version_id, booklet_id, slot),
                version=version_id,
                booklet=booklet_id,
                slot=slot_no,
            )
        return {
            "ok": True,
            "version": version_id,
            "booklet": booklet_id,
            "slot": slot_no,
            "endpoint": dict(endpoint),
            "value": doc,
        }

    def export(self, booklet_id=None):
        """The mirror's search view: every public endpoint exactly once."""
        rows = [
            row
            for (booklet, _part), row in self.docs.items()
            if booklet_id is None or booklet == booklet_id
        ]
        rows.sort(key=lambda row: (row["booklet"], row["slot"]))
        return {
            "ok": True,
            "type": "search_export",
            "count": len(rows),
            "entries": rows,
        }
