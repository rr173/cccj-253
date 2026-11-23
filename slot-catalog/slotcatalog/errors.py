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
