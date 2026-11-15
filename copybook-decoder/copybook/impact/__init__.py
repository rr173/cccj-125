"""Layout change impact report across two receiving manifests.

Before a new copybook version is added to the receiving manifest, this
subpackage answers two questions for the release review:

* which already-onboarded senders and which business dates will start
  resolving to the new layout version;
* whether a field identified by its full group path still means the same
  bytes under the old and the new layout.

It reads only manifests and copybooks -- never business data files --
and performs no record decoding or batch handoff validation.  Findings
that prevent a complete comparison (overlapping or missing coverage,
unreadable copybooks, duplicate field paths) are reported as structured
problems and force an ``unverifiable`` conclusion instead of a
seemingly complete report.
"""

from .diff import (ADDED, COMPATIBLE, INCOMPATIBLE, MOVED, REMOVED,
                   REINTERPRETED, UNCHANGED, UNVERIFIABLE, FieldChange,
                   LayoutComparison, compare_layouts)
from .engine import REPORT_JSON, REPORT_TXT, run_impact_report
from .errors import ImpactError
from .flatten import FlatLayout, LeafField, ViewCluster, flatten_layout

__all__ = [
    "ADDED", "COMPATIBLE", "FieldChange", "FlatLayout", "INCOMPATIBLE",
    "ImpactError", "LayoutComparison", "LeafField", "MOVED",
    "REINTERPRETED", "REMOVED", "REPORT_JSON", "REPORT_TXT", "UNCHANGED",
    "UNVERIFIABLE", "ViewCluster", "compare_layouts", "flatten_layout",
    "run_impact_report",
]
