"""Field policy for the masking sample generator.

A policy declares, per *full field path*, the category of a field and the
masking action applied to it::

    {
      "key_fingerprint": "sha256:0123456789abcdef",   # optional
      "fields": [
        {"path": ["ACCT-NO"], "action": "pseudonym", "kind": "account",
         "group": "acct", "on_empty": "reject"},
        {"path": ["CUST-NAME"], "action": "synthesize", "kind": "name"},
        {"path": ["BALANCE"], "action": "bucketize", "kind": "amount",
         "boundaries": ["1000.00", "10000.00"]},
        {"path": ["OPEN-DATE"], "action": "shift", "kind": "date",
         "format": "%Y-%m-%d", "max_days": 30, "group": "bizdate"}
      ]
    }

Paths are arrays of string segments addressing the decoded ``fields``
mapping.  When a segment meets an OCCURS list, the remaining path is
applied to *every* element of the list.  Fields not listed in the policy
pass through unchanged.

Validation is strict and never silent: unknown actions or keys, malformed
or duplicated paths, missing parameters, a ``group`` where it makes no
sense, or parameters that cannot preserve uniqueness all raise
:class:`MaskPolicyError` at load time.  Paths that match nothing in the
input, paths resolving to containers, and values whose type does not fit
the declared action are rejected by the engine during the scan phase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Union

from .errors import MaskPolicyError
from .mapping import _FINGERPRINT_RE

#: Supported actions.
PSEUDONYM = "pseudonym"      # stable format-preserving fake, keyed by group
SYNTHESIZE = "synthesize"    # fragment-free synthetic free text
SHIFT = "shift"              # deterministic keyed offset (amount / date)
BUCKETIZE = "bucketize"      # range generalisation (amount / date)

ACTIONS = (PSEUDONYM, SYNTHESIZE, SHIFT, BUCKETIZE)

#: Field categories.
KIND_ACCOUNT = "account"
KIND_NAME = "name"
KIND_AMOUNT = "amount"
KIND_DATE = "date"

_KINDS = (KIND_ACCOUNT, KIND_NAME, KIND_AMOUNT, KIND_DATE)

_ACTION_KINDS = {
    PSEUDONYM: (KIND_ACCOUNT,),
    SYNTHESIZE: (KIND_NAME,),
    SHIFT: (KIND_AMOUNT, KIND_DATE),
    BUCKETIZE: (KIND_AMOUNT, KIND_DATE),
}

_NULL_ACTIONS = ("keep", "reject")
_GRANULARITIES = ("month", "quarter", "year")
_DEFAULT_DATE_FORMAT = "%Y-%m-%d"

_COMMON_KEYS = {"path", "action", "kind", "group", "on_null", "on_empty"}
_ACTION_KEYS = {
    PSEUDONYM: set(),
    SYNTHESIZE: set(),
    SHIFT: {"max_pct", "max_abs", "max_days", "format"},
    BUCKETIZE: {"boundaries", "granularity", "format"},
}

_AMOUNT_TEXT_RE = re.compile(r"^[+-]?[0-9]+(\.[0-9]+)?$")


@dataclass(frozen=True)
class FieldRule:
    """One policy line: a full field path plus its masking action."""

    path: tuple[str, ...]
    action: str
    kind: str
    group: Optional[str] = None
    on_null: str = "keep"           # keep | reject
    on_empty: str = "keep"          # keep | reject (empty strings)
    max_pct: Optional[Decimal] = None     # shift/amount: +/- percent bound
    max_abs: Optional[Decimal] = None     # shift/amount: +/- absolute bound
    max_days: Optional[int] = None        # shift/date: +/- day bound
    fmt: Optional[str] = None             # date parse/format template
    boundaries: tuple = ()                # bucketize/amount: (text, Decimal)
    granularity: Optional[str] = None     # bucketize/date

    @property
    def path_text(self) -> str:
        return "/".join(self.path)

    @property
    def scope(self) -> str:
        """Linkage scope: the group when declared, else the full path."""
        return self.group if self.group is not None else self.path_text


@dataclass(frozen=True)
class MaskPolicy:
    """A validated set of field rules plus an optional key fingerprint."""

    rules: tuple[FieldRule, ...]
    key_fingerprint: Optional[str] = None

    @classmethod
    def load(cls, path: Union[str, Path]) -> "MaskPolicy":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MaskPolicyError(f"cannot read mask policy {p}: {exc}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "MaskPolicy":
        if not isinstance(data, dict):
            raise MaskPolicyError("mask policy must be a JSON object")
        unknown = set(data) - {"key_fingerprint", "fields"}
        if unknown:
            raise MaskPolicyError(
                f"mask policy has unknown top-level key(s): "
                f"{sorted(unknown)}")
        fingerprint = data.get("key_fingerprint")
        if fingerprint is not None:
            if not isinstance(fingerprint, str) or \
                    not _FINGERPRINT_RE.match(fingerprint):
                raise MaskPolicyError(
                    "key_fingerprint must be 'sha256:' followed by 16 hex "
                    f"characters, got {fingerprint!r}")
        raw_fields = data.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            raise MaskPolicyError(
                "mask policy requires a non-empty 'fields' array")
        rules = []
        seen_paths: set = set()
        for index, raw in enumerate(raw_fields):
            rule = _parse_rule(index, raw)
            if rule.path in seen_paths:
                raise MaskPolicyError(
                    f"duplicate policy rule for path "
                    f"{'/'.join(rule.path)!r}; one path may only carry one "
                    "action")
            seen_paths.add(rule.path)
            rules.append(rule)
        return cls(tuple(rules), fingerprint)


def _parse_rule(index: int, raw: Any) -> FieldRule:
    where = f"fields[{index}]"
    if not isinstance(raw, dict):
        raise MaskPolicyError(f"{where}: rule must be a JSON object")
    action = raw.get("action")
    if action not in ACTIONS:
        raise MaskPolicyError(
            f"{where}: action must be one of {list(ACTIONS)}, got {action!r}")
    unknown = set(raw) - _COMMON_KEYS - _ACTION_KEYS[action]
    if unknown:
        raise MaskPolicyError(
            f"{where}: unknown key(s) {sorted(unknown)} for action "
            f"{action!r}")

    path = _parse_path(where, raw.get("path"))
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise MaskPolicyError(
            f"{where}: kind must be one of {list(_KINDS)}, got {kind!r}")
    if kind not in _ACTION_KINDS[action]:
        raise MaskPolicyError(
            f"{where}: action {action!r} does not accept kind {kind!r}; "
            f"expected {' or '.join(_ACTION_KINDS[action])}")

    group = raw.get("group")
    if group is not None and (not isinstance(group, str) or not group):
        raise MaskPolicyError(f"{where}: group must be a non-empty string")
    if action == PSEUDONYM and group is None:
        raise MaskPolicyError(
            f"{where}: pseudonym rules require a 'group' so the same "
            "value maps identically across every file sharing the group")
    if action == BUCKETIZE and group is not None:
        raise MaskPolicyError(
            f"{where}: bucketize does not derive from the key, so a "
            "'group' linkage scope is meaningless here")

    on_null = raw.get("on_null", "keep")
    on_empty = raw.get("on_empty", "keep")
    for name, value in (("on_null", on_null), ("on_empty", on_empty)):
        if value not in _NULL_ACTIONS:
            raise MaskPolicyError(
                f"{where}: {name} must be one of {list(_NULL_ACTIONS)}, "
                f"got {value!r}")

    fmt = raw.get("format", _DEFAULT_DATE_FORMAT)
    if kind == KIND_DATE:
        if not isinstance(fmt, str) or not fmt:
            raise MaskPolicyError(f"{where}: format must be a non-empty "
                                  "date format string")
        try:
            datetime(2024, 3, 15).strftime(fmt)
            datetime.strptime("2024-03-15", fmt)
        except (ValueError, TypeError) as exc:
            raise MaskPolicyError(
                f"{where}: unusable date format {fmt!r}: {exc}")

    max_pct = max_abs = None
    max_days = None
    boundaries: tuple = ()
    granularity = None

    if action == SHIFT and kind == KIND_AMOUNT:
        has_pct = "max_pct" in raw
        has_abs = "max_abs" in raw
        if has_pct == has_abs:
            raise MaskPolicyError(
                f"{where}: shift/amount requires exactly one of 'max_pct' "
                "or 'max_abs'")
        if has_pct:
            max_pct = _parse_decimal(where, "max_pct", raw["max_pct"])
            if not Decimal(0) < max_pct <= Decimal(100):
                raise MaskPolicyError(
                    f"{where}: max_pct must be in (0, 100], got {max_pct}")
        else:
            max_abs = _parse_decimal(where, "max_abs", raw["max_abs"])
            if max_abs <= 0:
                raise MaskPolicyError(
                    f"{where}: max_abs must be positive, got {max_abs}")
    elif action == SHIFT and kind == KIND_DATE:
        if "max_days" not in raw:
            raise MaskPolicyError(
                f"{where}: shift/date requires 'max_days'")
        max_days = raw["max_days"]
        if isinstance(max_days, bool) or not isinstance(max_days, int) \
                or max_days < 1:
            raise MaskPolicyError(
                f"{where}: max_days must be an integer >= 1, got "
                f"{max_days!r}")
    elif action == BUCKETIZE and kind == KIND_AMOUNT:
        boundaries = _parse_boundaries(where, raw.get("boundaries"))
    elif action == BUCKETIZE and kind == KIND_DATE:
        granularity = raw.get("granularity")
        if granularity not in _GRANULARITIES:
            raise MaskPolicyError(
                f"{where}: granularity must be one of "
                f"{list(_GRANULARITIES)}, got {granularity!r}")

    return FieldRule(
        path=path, action=action, kind=kind, group=group,
        on_null=on_null, on_empty=on_empty, max_pct=max_pct,
        max_abs=max_abs, max_days=max_days,
        fmt=fmt if kind == KIND_DATE else None,
        boundaries=boundaries, granularity=granularity)


def _parse_path(where: str, spec: Any) -> tuple[str, ...]:
    if not isinstance(spec, list) or not spec:
        raise MaskPolicyError(
            f"{where}: path must be a non-empty array of strings, got "
            f"{spec!r}")
    parts = []
    for piece in spec:
        if not isinstance(piece, str) or not piece:
            raise MaskPolicyError(
                f"{where}: path segments must be non-empty strings, got "
                f"{piece!r}")
        parts.append(piece)
    return tuple(parts)


def _parse_decimal(where: str, key: str, raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise MaskPolicyError(
            f"{where}: {key} must be a number or decimal string, got "
            f"{raw!r}")
    text = raw if isinstance(raw, str) else str(raw)
    if isinstance(raw, str) and not _AMOUNT_TEXT_RE.match(raw):
        raise MaskPolicyError(
            f"{where}: {key} is not a plain decimal string: {raw!r}")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - guarded by regex
        raise MaskPolicyError(f"{where}: {key} is not a decimal: {raw!r}")


def _parse_boundaries(where: str, raw: Any) -> tuple:
    if not isinstance(raw, list) or not raw:
        raise MaskPolicyError(
            f"{where}: boundaries must be a non-empty array of decimal "
            "strings")
    parsed = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise MaskPolicyError(
                f"{where}: boundary values must be decimal strings or "
                f"integers, got {item!r}")
        text = item if isinstance(item, str) else str(item)
        if isinstance(item, str) and not _AMOUNT_TEXT_RE.match(item):
            raise MaskPolicyError(
                f"{where}: boundary is not a plain decimal string: "
                f"{item!r}")
        parsed.append((text, Decimal(text)))
    for i in range(1, len(parsed)):
        if parsed[i][1] <= parsed[i - 1][1]:
            raise MaskPolicyError(
                f"{where}: boundaries must be strictly increasing; "
                f"{parsed[i - 1][0]!r} is not below {parsed[i][0]!r}")
    return tuple(parsed)
