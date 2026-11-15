"""Field-path configuration for the settlement processor.

The processor never reaches into a fixed copybook layout: it only walks the
``fields`` mapping of each decoded JSONL event through *field paths* --
non-empty arrays of string keys, with numeric parts addressing OCCURS
lists -- exactly like the audit and mask subpackages.

The nine business routes (event_id, account, currency, at, kind,
object_id, amount, due_on, target_id) are all required keys in the config.
Paths used only by some event kinds still have to be declared; the values
behind them are simply ignored when an event does not need them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .errors import SettleConfigError

#: Every route the config must declare.
ROUTES = ("event_id", "account", "currency", "at", "kind", "object_id",
          "amount", "due_on", "target_id")


@dataclass(frozen=True)
class FieldPath:
    """A string-array path into an event's ``fields`` mapping."""

    parts: tuple[str, ...]

    @classmethod
    def parse(cls, spec: Any) -> "FieldPath":
        if not isinstance(spec, list) or not spec:
            raise SettleConfigError(
                "field path must be a non-empty array of strings, "
                f"got {spec!r}")
        parts: list[str] = []
        for piece in spec:
            if not isinstance(piece, str) or not piece:
                raise SettleConfigError(
                    "field path segments must be non-empty strings, got "
                    f"{piece!r} in {spec!r}")
            parts.append(piece)
        return cls(tuple(parts))

    def get(self, fields: Any) -> Any:
        """Value at the path, or ``None`` when any hop is missing."""
        current = fields
        for piece in self.parts:
            if isinstance(current, dict):
                if piece not in current:
                    return None
                current = current[piece]
            elif isinstance(current, list):
                try:
                    idx = int(piece)
                except ValueError:
                    return None
                if not 0 <= idx < len(current):
                    return None
                current = current[idx]
            else:
                return None
        return current

    def as_text(self, fields: Any) -> Optional[str]:
        value = self.get(fields)
        if value is None:
            return None
        return str(value)

    def spec(self) -> list[str]:
        return list(self.parts)


@dataclass(frozen=True)
class SettleConfig:
    """Validated field routes for the nine business fields."""

    paths: dict[str, FieldPath]

    @classmethod
    def load(cls, path: Union[str, Path]) -> "SettleConfig":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettleConfigError(
                f"cannot read settlement config {p}: {exc}")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "SettleConfig":
        if not isinstance(data, dict):
            raise SettleConfigError("settlement config must be a JSON object")
        paths_raw = data.get("paths")
        if not isinstance(paths_raw, dict):
            raise SettleConfigError(
                "settlement config requires a 'paths' object")
        missing = [name for name in ROUTES if name not in paths_raw]
        if missing:
            raise SettleConfigError(
                f"paths is missing required routes: {', '.join(missing)}")
        unknown = [name for name in paths_raw if name not in ROUTES]
        if unknown:
            raise SettleConfigError(
                f"paths contains unknown routes: {', '.join(unknown)}")
        paths = {name: FieldPath.parse(paths_raw[name]) for name in ROUTES}
        return cls(paths=paths)

    def get(self, route: str) -> FieldPath:
        return self.paths[route]
