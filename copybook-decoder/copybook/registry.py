"""Registry of parsed layouts, keyed by (copybook name, version)."""

from __future__ import annotations

from pathlib import Path

from .errors import CopybookError, ManifestError
from .manifest import Manifest
from .model import Layout
from .parser import parse_copybook


class LayoutRegistry:
    def __init__(self) -> None:
        self._layouts: dict[tuple[str, str], Layout] = {}

    def register(self, layout: Layout) -> None:
        key = (layout.name, layout.version)
        if key in self._layouts:
            raise ManifestError(
                f"duplicate layout definition for {key[0]} version {key[1]}")
        self._layouts[key] = layout

    def get(self, name: str, version: str) -> Layout:
        try:
            return self._layouts[(name, version)]
        except KeyError:
            raise CopybookError(
                f"layout {name!r} version {version!r} is not registered")

    @classmethod
    def from_manifest(cls, manifest: Manifest) -> "LayoutRegistry":
        """Parse and register every copybook referenced by a manifest."""
        registry = cls()
        for entry in manifest.entries:
            path = manifest.base_dir / entry.file
            if not path.is_file():
                raise ManifestError(f"copybook file not found: {path}")
            text = path.read_text(encoding="utf-8")
            layout = parse_copybook(text, name=entry.copybook,
                                    version=entry.version, codec=entry.codec)
            registry.register(layout)
        return registry
