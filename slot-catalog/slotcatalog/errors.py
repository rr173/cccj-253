"""Structured errors for the stable-slot catalog.

Every failure carries a stable machine-readable ``code`` so that scenario
files and acceptance goldens can assert on failures without matching prose.
"""


class CatalogError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "catalog_error"

    def __init__(self, message=None, **details):
        self.message = message or self.code
        self.details = details
        super().__init__(self.message)

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class InvalidOp(CatalogError):
    code = "invalid_op"


class UnknownBooklet(CatalogError):
    code = "unknown_booklet"


class BookletExists(CatalogError):
    code = "booklet_exists"


class UnknownVersion(CatalogError):
    code = "unknown_version"


class DuplicateVersion(CatalogError):
    code = "duplicate_version"


class SlotNotPrinted(CatalogError):
    """The slot was not formally listed in the requested paper version.

    Covers: never allocated, reserved-but-unprinted, withdrawn before that
    version, or already replaced (hence not listed) at that version.
    """

    code = "slot_not_printed"


class SlotOutOfRange(CatalogError):
    code = "slot_out_of_range"


class SlotOccupied(CatalogError):
    code = "slot_occupied"


class SlotNotReserved(CatalogError):
    """An explicitly chosen slot was never reserved as a placeholder."""

    code = "slot_not_reserved"


class PartExists(CatalogError):
    code = "part_exists"


class PartNotFound(CatalogError):
    """The part number is not registered in the booklet.

    A reserved-but-empty slot has no part, so it can never be linked into a
    replacement chain: any edge naming it fails with this error.
    """

    code = "part_not_found"


class AlreadyWithdrawn(CatalogError):
    code = "already_withdrawn"


class EdgeNotFound(CatalogError):
    code = "edge_not_found"


class ReplacementCycle(CatalogError):
    code = "replacement_cycle"


class CapacityExceeded(CatalogError):
    code = "capacity_exceeded"


class MergeError(CatalogError):
    """Base class for offline merge (合稿) failures."""

    code = "merge_failed"


class InvalidBranchFile(MergeError):
    """A branch file is not a well-formed slot-catalog-branch/1 document."""

    code = "invalid_branch_file"


class BaseMismatch(MergeError):
    """A branch was cataloged against different base bytes."""

    code = "base_mismatch"


class UnsupportedOp(MergeError):
    """A branch batch contains an op the offline merge does not handle."""

    code = "unsupported_op"


class BranchInvalid(MergeError):
    """A branch does not apply cleanly to the base on its own."""

    code = "branch_invalid"


class PartConflict(MergeError):
    """Two branches registered the same part number in the same booklet."""

    code = "part_conflict"


class SlotConflict(MergeError):
    """Two different parts explicitly claim the same slot."""

    code = "slot_conflict"


class EdgeConflict(MergeError):
    """Branches made different net changes to the same replacement out-edge."""

    code = "edge_conflict"


class OutputExists(MergeError):
    """The merge output path already exists and is never overwritten."""

    code = "output_exists"


class SyncError(CatalogError):
    """Base class for offline sync channel (同步通道) failures."""

    code = "sync_error"


class InvalidPackage(SyncError):
    """A sync package is corrupt, truncated or malformed.

    Covers: unparsable JSON, wrong format tag, missing or mistyped fields,
    and content-digest mismatches (i.e. packages damaged or incompletely
    received in transit).
    """

    code = "invalid_package"


class CheckpointMismatch(SyncError):
    """A package does not start from the mirror's current checkpoint.

    Also raised when a mirror's first ever package is not a full package
    and when a full package would move the mirror backwards.
    """

    code = "checkpoint_mismatch"


class WrongSource(SyncError):
    """A package belongs to a different sync source than the mirror's."""

    code = "wrong_source"


class UnknownCheckpoint(SyncError):
    """The requested start checkpoint is not on this source's timeline."""

    code = "unknown_checkpoint"


class EntryNotFound(SyncError):
    """A mirror lookup found no live entry for the printed index.

    The slot may never have been printed in that version, or its entry
    lapsed because the current replacement endpoint is not public; the
    mirror keeps no tombstones and never falls back to stale content.
    """

    code = "entry_not_found"


class BatchError(CatalogError):
    """A batch failed validation; nothing from it was applied."""

    code = "batch_failed"

    def __init__(self, batch_id, cause, op_index=None):
        self.batch_id = batch_id
        self.cause = cause
        self.op_index = op_index
        super().__init__("batch %r failed: %s" % (batch_id, cause.message))

    def to_dict(self):
        out = self.cause.to_dict()
        out["batch_id"] = self.batch_id
        if self.op_index is not None:
            out["op_index"] = self.op_index
        return out


class AccessError(CatalogError):
    """Base class for access-statistics (访问统计) failures."""

    code = "access_error"


class InvalidAccessRecord(AccessError):
    """An access record or record op has a malformed field."""

    code = "invalid_access_record"


class RecordExists(AccessError):
    """An added record id is already present."""

    code = "record_exists"


class RecordNotFound(AccessError):
    """A corrected or revoked record id is not present."""

    code = "record_not_found"


class DuplicateRecordOp(AccessError):
    """One record group names the same record id more than once."""

    code = "duplicate_record_op"


class RecordBatchError(AccessError):
    """A record group failed validation; none of its ops were applied."""

    code = "record_batch_failed"

    def __init__(self, batch_id, cause, op_index=None):
        self.batch_id = batch_id
        self.cause = cause
        self.op_index = op_index
        super().__init__("record batch %r failed: %s" % (batch_id, cause.message))

    def to_dict(self):
        out = self.cause.to_dict()
        out["batch_id"] = self.batch_id
        if self.op_index is not None:
            out["op_index"] = self.op_index
        return out
