"""Exception hierarchy for the layout change impact report."""

from ..errors import CopybookError


class ImpactError(CopybookError):
    """The impact report itself cannot be produced.

    Analysis findings (overlaps, gaps, missing copybooks, duplicate field
    paths) are *data* inside the report and never raise; this exception is
    reserved for situations where no trustworthy report file can be
    written at all -- above all an output directory that already exists.
    """
