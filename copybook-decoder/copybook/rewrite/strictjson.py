"""Strict JSON loading for correction orders.

The correction order is a machine-to-machine contract, so the loader is
deliberately stricter than :func:`json.loads`:

* duplicate object members are rejected (the later value never silently
  overwrites the earlier one);
* ``NaN``, ``Infinity`` and ``-Infinity`` tokens are rejected;
* every JSON number is checked to be an integer -- floats and
  big-decimal notation are rejected instead of being rounded.

Schema-level type checks (bools are not integers, non-empty strings,
hex shape, ...) live in :mod:`copybook.rewrite.order`, next to the
order model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

from .errors import RewriteOrderError


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise RewriteOrderError(
                f"duplicate JSON object member {key!r}; duplicate members "
                "are rejected instead of being silently overwritten")
        seen.add(key)
    return dict(pairs)


def _reject_floats(value: Any) -> Any:
    # json's parse_float/parse_constant hooks fire while parsing; raising
    # here turns the offending token into a hard, whole-job failure.
    raise RewriteOrderError(
        f"JSON number/token {value!r} is not an integer; floating point "
        "values, NaN and Infinity are not accepted in a correction order")


def load_strict_json(path: Union[str, Path]) -> Any:
    """Read *path* as UTF-8 JSON with the strictness rules above."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RewriteOrderError(f"cannot read correction order {p}: {exc}")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates,
                          parse_float=_reject_floats,
                          parse_constant=_reject_floats)
    except RewriteOrderError:
        raise
    except json.JSONDecodeError as exc:
        raise RewriteOrderError(
            f"correction order {p} is not valid JSON: {exc}") from exc
