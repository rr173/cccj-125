"""Copybook decoder for legacy fixed-width EBCDIC data exchange."""

from . import audit
from .decoder import Decoder, RedefinesPolicy
from .errors import (CopybookError, CopybookSyntaxError, FieldDecodeError,
                     ManifestError, RecordLengthError)
from .manifest import LayoutRef, Manifest
from .model import Field, Layout, Pic
from .parser import parse_copybook
from .pipeline import (DecodeResult, DecodedRecord, QuarantinedLine,
                       decode_file)
from .registry import LayoutRegistry

__version__ = "0.2.0"

__all__ = [
    "CopybookError", "CopybookSyntaxError", "DecodeResult", "DecodedRecord",
    "Decoder", "Field", "FieldDecodeError", "Layout", "LayoutRef",
    "LayoutRegistry", "Manifest", "ManifestError", "Pic", "QuarantinedLine",
    "RecordLengthError", "RedefinesPolicy", "audit", "decode_file",
    "parse_copybook",
]
