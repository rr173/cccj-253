"""Stable-slot catalog: locally maintainable slot directory, two read views."""

from .access import ACCESS_FORMAT, REPORT_FORMAT, AccessStats
from .catalog import Catalog, STATE_FORMAT
from .errors import BatchError, CatalogError
from .merge import BRANCH_FORMAT, merge_branches
from .scenario import run_scenario
from .sync import MIRROR_FORMAT, PACKAGE_FORMAT, SOURCE_FORMAT, Mirror, SyncSource

__all__ = [
    "AccessStats",
    "ACCESS_FORMAT",
    "REPORT_FORMAT",
    "Catalog",
    "STATE_FORMAT",
    "BRANCH_FORMAT",
    "SOURCE_FORMAT",
    "MIRROR_FORMAT",
    "PACKAGE_FORMAT",
    "BatchError",
    "CatalogError",
    "Mirror",
    "SyncSource",
    "merge_branches",
    "run_scenario",
]
