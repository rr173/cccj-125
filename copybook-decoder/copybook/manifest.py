"""Receiving manifest: selects the layout version for an inbound file.

The manifest is the *only* source of version truth.  A layout is chosen by
(sender, business_date); if no entry matches, or more than one could
apply, resolution fails loudly instead of guessing.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Union

from .errors import ManifestError


@dataclass(frozen=True)
class LayoutRef:
    """One manifest entry: which copybook version applies, and when."""

    sender: str
    copybook: str
    version: str
    effective_from: date
    effective_to: Optional[date]      # inclusive; None = open-ended
    file: str                         # copybook path, relative to manifest
    codec: str = "cp037"


class Manifest:
    """A validated set of :class:`LayoutRef` entries."""

    def __init__(self, entries: list[LayoutRef], base_dir: Path):
        self.entries = entries
        self.base_dir = Path(base_dir)

    # -------------------------------------------------------------- load

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Manifest":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read manifest {p}: {exc}")
        if not isinstance(data, dict) or not isinstance(data.get("layouts"), list) \
                or not data["layouts"]:
            raise ManifestError(
                f"manifest {p}: expected a non-empty 'layouts' list")
        entries = [cls._parse_entry(i, raw)
                   for i, raw in enumerate(data["layouts"])]
        manifest = cls(entries, p.resolve().parent)
        manifest._check_overlaps()
        return manifest

    @staticmethod
    def _parse_entry(index: int, raw) -> LayoutRef:
        where = f"layouts[{index}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: entry must be an object")
        missing = [k for k in ("sender", "copybook", "version",
                               "effective_from", "file") if k not in raw]
        if missing:
            raise ManifestError(
                f"{where}: missing required keys: {', '.join(missing)}")

        def _parse_date(key: str) -> Optional[date]:
            value = raw.get(key)
            if value is None:
                return None
            try:
                return date.fromisoformat(str(value))
            except ValueError:
                raise ManifestError(
                    f"{where}: invalid {key} {value!r}; expected YYYY-MM-DD")

        eff_from = _parse_date("effective_from")
        assert eff_from is not None  # required key, checked above
        eff_to = _parse_date("effective_to")
        if eff_to is not None and eff_to < eff_from:
            raise ManifestError(
                f"{where}: effective_to {eff_to} is before "
                f"effective_from {eff_from}")
        codec = str(raw.get("codec", "cp037"))
        try:
            codecs.lookup(codec)
        except LookupError:
            raise ManifestError(f"{where}: unknown codec {codec!r}")
        return LayoutRef(sender=str(raw["sender"]),
                         copybook=str(raw["copybook"]).upper(),
                         version=str(raw["version"]),
                         effective_from=eff_from,
                         effective_to=eff_to,
                         file=str(raw["file"]),
                         codec=codec)

    def _check_overlaps(self) -> None:
        """Reject overlapping validity ranges for the same sender+copybook."""
        by_key: dict[tuple[str, str], list[LayoutRef]] = {}
        for e in self.entries:
            by_key.setdefault((e.sender, e.copybook), []).append(e)
        for (sender, copybook), entries in by_key.items():
            entries.sort(key=lambda e: e.effective_from)
            for prev, cur in zip(entries, entries[1:]):
                prev_end = prev.effective_to or date.max
                if cur.effective_from <= prev_end:
                    raise ManifestError(
                        f"conflicting layout versions for sender={sender!r} "
                        f"copybook={copybook!r}: version {prev.version} "
                        f"({prev.effective_from}..{prev.effective_to or 'open'}) "
                        f"overlaps version {cur.version} "
                        f"({cur.effective_from}..{cur.effective_to or 'open'})")

    # ----------------------------------------------------------- resolve

    def resolve(self, sender: Optional[str],
                business_date: Union[str, date, None],
                copybook: Optional[str] = None) -> LayoutRef:
        """Pick the single applicable layout version.

        Raises :class:`ManifestError` when information is missing or
        ambiguous -- a version is never guessed.
        """
        if not sender:
            raise ManifestError(
                "sender is required to select a layout version; "
                "refusing to guess")
        if business_date is None:
            raise ManifestError(
                "business_date is required to select a layout version; "
                "refusing to guess")
        if isinstance(business_date, str):
            try:
                day = date.fromisoformat(business_date)
            except ValueError:
                raise ManifestError(
                    f"invalid business_date {business_date!r}; "
                    "expected YYYY-MM-DD")
        elif isinstance(business_date, date):
            day = business_date
        else:
            raise ManifestError(
                f"invalid business_date of type {type(business_date).__name__}")

        candidates = [
            e for e in self.entries
            if e.sender == sender
            and e.effective_from <= day <= (e.effective_to or date.max)
        ]
        if copybook is not None:
            wanted = copybook.upper()
            candidates = [e for e in candidates if e.copybook == wanted]
        if not candidates:
            scope = f"sender={sender!r} business_date={day.isoformat()}"
            if copybook is not None:
                scope += f" copybook={copybook!r}"
            raise ManifestError(f"no layout version found for {scope}")
        distinct = {(c.copybook, c.version) for c in candidates}
        if len(distinct) > 1:
            raise ManifestError(
                f"conflicting layout versions for sender={sender!r} "
                f"business_date={day.isoformat()}: "
                + ", ".join(f"{cb} v{v}" for cb, v in sorted(distinct))
                + ("; pass copybook=... to disambiguate"
                   if copybook is None else ""))
        return candidates[0]
