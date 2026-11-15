"""Pack stream decoding: zlib streams, object identity and delta replay.

The pack is never fully inflated into memory.  Each entry's payload is
streamed from the pack, its Git object id is recomputed from the canonical
``"<type> <size>\\0"`` header plus real content, and the object is indexed
by that id.  Delta payloads are spilled to disk under the caller's staging
directory, so the number of delta objects does not grow peak memory.

Delta bases are fetched from the store by id or by earlier entry index --
never assumed to precede their REF target -- so REF_DELTA bases may appear
later in the pack and a base may itself be a delta.  A base object is
immutable: several targets may share it, and resolving a target never
rewrites, truncates or consumes the base.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union

from .bundle import (OBJ_OFS_DELTA, OBJ_REF_DELTA, OBJ_TAG, PackEntry,
                     PackLayout, TYPE_NAMES, read_entry_stream)
from .errors import (DeltaError, ObjectFormatError, ObjectStoreError,
                     ResourceLimitError)

#: A delta's declared target size above this is never attempted: the
#: single-object limit is enforced by the caller, but an absurd target
#: size is a corruption that should be diagnosed before allocation.
MAX_DELTA_TARGET_BYTES = 1 << 34


class Limits:
    """Hard resource limits.  Reaching a limit refuses the whole parse."""

    def __init__(self, *, max_object_bytes: int = 64 * 1024 * 1024,
                 max_total_bytes: int = 1024 * 1024 * 1024,
                 max_objects: int = 1_000_000,
                 max_delta_depth: int = 64):
        if max_object_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("object limits must be positive")
        if max_objects <= 0 or max_delta_depth < 0:
            raise ValueError("object count / delta depth limits invalid")
        self.max_object_bytes = max_object_bytes
        self.max_total_bytes = max_total_bytes
        self.max_objects = max_objects
        self.max_delta_depth = max_delta_depth

    def as_dict(self) -> dict:
        return {
            "max_object_bytes": self.max_object_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_objects": self.max_objects,
            "max_delta_depth": self.max_delta_depth,
        }


class ObjectInfo:
    """Metadata of one resolved object in the store.

    ``sha256`` is the digest of the *content* (not the object id) and is
    computed while the object is staged, so later listings and export
    records never need to keep blob bytes in memory.
    """

    __slots__ = ("object_id", "obj_type", "size", "path", "sha256")

    def __init__(self, object_id: str, obj_type: int, size: int,
                 path: Path, sha256: Optional[str] = None):
        self.object_id = object_id
        self.obj_type = obj_type
        self.size = size
        self.path = path
        self.sha256 = sha256

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.obj_type]

    def as_dict(self) -> dict:
        return {"object_id": self.object_id, "type": self.type_name,
                "size": self.size, "sha256": self.sha256}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ObjectInfo({self.object_id[:12]}, {self.type_name}, "
                f"{self.size})")


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def object_id_for(obj_type: int, content: bytes) -> str:
    """Recompute a Git object id from type + real content bytes."""
    header = f"{TYPE_NAMES[obj_type]} {len(content)}\0".encode("ascii")
    digest = hashlib.sha1()
    digest.update(header)
    digest.update(content)
    return digest.hexdigest()


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply one Git delta to *base*.

    The source size at the head of the delta and every copy/insert
    boundary are validated exactly; no instruction may read or write past
    its buffer.  The target size must be produced exactly.
    """
    if len(delta) < 2:
        raise DeltaError("delta is shorter than its source-size field")
    src_size, pos = _read_varint(delta, 0, "source size")
    if src_size != len(base):
        raise DeltaError(
            f"delta expects a {src_size}-byte base but the base object is "
            f"{len(base)} bytes")
    dst_size, pos = _read_varint(delta, pos, "target size")
    if dst_size > MAX_DELTA_TARGET_BYTES:
        raise DeltaError(
            f"delta declares an implausible {dst_size}-byte target "
            f"(cap {MAX_DELTA_TARGET_BYTES} bytes)")
    out = bytearray()
    n = len(delta)
    while pos < n:
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    if pos >= n:
                        raise DeltaError(
                            "delta copy offset runs past the instruction "
                            "stream")
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    if pos >= n:
                        raise DeltaError(
                            "delta copy size runs past the instruction "
                            "stream")
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(base):
                raise DeltaError(
                    f"delta copy [{offset}, {offset + size}) runs past "
                    f"the {len(base)}-byte base")
            if len(out) + size > dst_size:
                raise DeltaError(
                    f"delta copy would write {len(out) + size} bytes, "
                    f"past the declared target size {dst_size}")
            out += base[offset:offset + size]
        elif op != 0:
            if pos + op > n:
                raise DeltaError(
                    "delta insert runs past the instruction stream")
            if len(out) + op > dst_size:
                raise DeltaError(
                    f"delta insert would write {len(out) + op} bytes, "
                    f"past the declared target size {dst_size}")
            out += delta[pos:pos + op]
            pos += op
        else:
            raise DeltaError("delta contains a reserved zero opcode")
    if len(out) != dst_size:
        raise DeltaError(
            f"delta produced {len(out)} bytes but declares target size "
            f"{dst_size}")
    return bytes(out)


def _read_varint(data: bytes, pos: int, what: str) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise DeltaError(f"delta {what} is truncated")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift > 63:
            raise DeltaError(f"delta {what} varint is too long")
    return value, pos


class Staging:
    """One run's spill area under the caller's staging directory."""

    def __init__(self, root: Union[str, Path], *, max_bytes: int):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.deltas = self.root / "deltas"
        self.objects.mkdir(parents=True, exist_ok=False)
        self.deltas.mkdir(parents=True, exist_ok=False)
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def path_for(self, object_id: str) -> Path:
        if len(object_id) != 40 or "/" in object_id or "\\" in object_id:
            raise ObjectStoreError(
                f"refusing unsafe object id {object_id!r}")
        bucket = self.objects / object_id[:2]
        bucket.mkdir(exist_ok=True)
        return bucket / object_id

    def write(self, object_id: str, content: bytes) -> Path:
        path = self.path_for(object_id)
        if path.exists():
            if path.stat().st_size != len(content):
                raise ObjectStoreError(
                    f"object {object_id} already staged with a different "
                    "size")
            return path
        self._reserve(len(content))
        with path.open("wb") as fh:
            fh.write(content)
        return path

    def write_delta(self, index: int, payload: bytes) -> Path:
        path = self.deltas / f"{index:012d}.delta"
        self._reserve(len(payload))
        path.write_bytes(payload)
        return path

    def _reserve(self, n: int) -> None:
        if self.bytes_written + n > self.max_bytes:
            raise ResourceLimitError(
                f"staged objects would exceed the {self.max_bytes}-byte "
                "cumulative limit")
        self.bytes_written += n


class PackStore:
    """Index and resolve every object in one validated pack."""

    def __init__(self, pack: PackLayout, staging: Staging, limits: Limits):
        self.pack = pack
        self.staging = staging
        self.limits = limits
        self.infos: dict[str, ObjectInfo] = {}
        self.by_index: dict[int, ObjectInfo] = {}
        #: first pack index for each object id (REF_DELTA chain tracking)
        self._id_index: dict[str, int] = {}
        self._delta_paths: dict[int, Path] = {}
        self._ofs_base: dict[int, int] = {}
        self._ref_base: dict[int, str] = {}
        self.max_depth_seen = 0
        self.resolved_delta_count = 0
        self.staged_bytes = 0

    # -- public lookups -------------------------------------------------
    def get(self, object_id: str) -> Optional[ObjectInfo]:
        return self.infos.get(object_id)

    def require(self, object_id: str, obj_type: Optional[int] = None
                ) -> ObjectInfo:
        info = self.infos.get(object_id)
        if info is None:
            raise ObjectStoreError(
                f"object {object_id} is not present in the bundle")
        if obj_type is not None and info.obj_type != obj_type:
            raise ObjectStoreError(
                f"object {object_id} is a {info.type_name}, expected "
                f"{TYPE_NAMES[obj_type]}")
        return info

    def read(self, object_id: str) -> bytes:
        return self.require(object_id).path.read_bytes()

    # -- parsing --------------------------------------------------------
    def load(self) -> "PackStore":
        if len(self.pack.entries) > self.limits.max_objects:
            raise ResourceLimitError(
                f"pack has {len(self.pack.entries)} objects, exceeding the "
                f"{self.limits.max_objects}-object limit")
        total = 0
        for entry in self.pack.entries:
            if entry.obj_type == OBJ_TAG:
                raise ObjectFormatError(
                    f"pack object {entry.index} is a tag: only commit, "
                    "tree and blob objects are supported")
            if total + entry.size > self.limits.max_total_bytes:
                raise ResourceLimitError(
                    "cumulative inflated object bytes exceed the "
                    f"{self.limits.max_total_bytes}-byte limit")
            total += entry.size
            payload = read_entry_stream(
                self.pack.path, entry,
                max_bytes=self.limits.max_object_bytes)
            if entry.obj_type == OBJ_OFS_DELTA:
                base_offset = entry.offset - entry.base_distance
                base_index = self.pack.offset_index.get(base_offset)
                if base_index is None:
                    raise DeltaError(
                        f"OFS_DELTA object {entry.index} at offset "
                        f"{entry.offset} points at {base_offset}, which is "
                        "not an object boundary in this pack")
                if base_index >= entry.index:
                    raise DeltaError(
                        f"OFS_DELTA object {entry.index} points forward "
                        f"to object {base_index}: delta cycle")
                self._ofs_base[entry.index] = base_index
                self._delta_paths[entry.index] = self.staging.write_delta(
                    entry.index, payload)
            elif entry.obj_type == OBJ_REF_DELTA:
                self._ref_base[entry.index] = entry.base_id
                self._delta_paths[entry.index] = self.staging.write_delta(
                    entry.index, payload)
            else:
                self._stage_with_id(entry, payload)
        self._resolve_deltas()
        self.staged_bytes = self.staging.bytes_written
        return self

    def _stage_with_id(self, entry: PackEntry, content: bytes) -> ObjectInfo:
        if len(content) > self.limits.max_object_bytes:
            raise ResourceLimitError(
                f"object {entry.index} exceeds the "
                f"{self.limits.max_object_bytes}-byte single-object limit")
        object_id = object_id_for(entry.obj_type, content)
        path = self.staging.write(object_id, content)
        info = ObjectInfo(object_id, entry.obj_type, len(content), path,
                          content_sha256(content))
        self._register(entry.index, info)
        return info

    def _register(self, index: int, info: ObjectInfo) -> None:
        existing = self.infos.get(info.object_id)
        if existing is not None:
            if (existing.obj_type != info.obj_type
                    or existing.size != info.size):
                raise ObjectStoreError(
                    f"object id {info.object_id} appears twice with "
                    "different type or size")
            self.by_index[index] = existing
            return
        self.infos[info.object_id] = info
        self.by_index[index] = info
        self._id_index.setdefault(info.object_id, index)

    def _resolve_deltas(self) -> None:
        pending = dict(self._delta_paths)
        while pending:
            progress = False
            for index in sorted(pending):
                base = self._known_base(index)
                if base is None:
                    continue
                base_info, base_content = base
                delta_path = pending.pop(index)
                delta = delta_path.read_bytes()
                delta_path.unlink()
                result = apply_delta(base_content, delta)
                if len(result) > self.limits.max_object_bytes:
                    raise ResourceLimitError(
                        f"object {index} expands to {len(result)} bytes, "
                        f"past the {self.limits.max_object_bytes}-byte "
                        "single-object limit")
                object_id = object_id_for(base_info.obj_type, result)
                path = self.staging.write(object_id, result)
                info = ObjectInfo(object_id, base_info.obj_type,
                                  len(result), path,
                                  content_sha256(result))
                self._register(index, info)
                self.resolved_delta_count += 1
                progress = True
            if not progress:
                raise self._unresolved(pending)
        self._delta_paths.clear()
        self._compute_depths()

    def _known_base(self, index: int
                    ) -> Optional[tuple[ObjectInfo, bytes]]:
        depth = self._chain_depth(index)
        if depth > self.limits.max_delta_depth:
            raise ResourceLimitError(
                f"delta chain at object {index} exceeds the "
                f"{self.limits.max_delta_depth}-level limit")
        self.max_depth_seen = max(self.max_depth_seen, depth)
        if index in self._ofs_base:
            info = self.by_index.get(self._ofs_base[index])
            if info is None:
                return None
            return info, info.path.read_bytes()
        base_id = self._ref_base[index]
        info = self.infos.get(base_id)
        if info is None:
            return None
        return info, info.path.read_bytes()

    def _chain_depth(self, index: int) -> int:
        depth = 1
        seen = {index}
        current = index
        while True:
            if current in self._ofs_base:
                current = self._ofs_base[current]
            else:
                base_id = self._ref_base.get(current)
                if base_id is None:
                    break
                current = self._id_index.get(base_id)
                if current is None:
                    break
            if current in seen:
                raise DeltaError(
                    f"delta dependency cycle detected at object {current}")
            seen.add(current)
            depth += 1
            if depth > self.limits.max_delta_depth:
                raise ResourceLimitError(
                    f"delta chain at object {index} exceeds the "
                    f"{self.limits.max_delta_depth}-level limit")
        return depth

    def _compute_depths(self) -> None:
        depth: dict[int, int] = {}
        for entry in self.pack.entries:
            idx = entry.index
            if idx not in self._ofs_base and idx not in self._ref_base:
                depth[idx] = 0
                continue
            chain = 0
            current = idx
            seen: set[int] = set()
            while current in self._ofs_base or current in self._ref_base:
                if current in seen:
                    raise DeltaError(
                        f"delta dependency cycle detected at object "
                        f"{current}")
                seen.add(current)
                if current in self._ofs_base:
                    current = self._ofs_base[current]
                else:
                    base_id = self._ref_base[current]
                    current = self._id_index.get(base_id)
                    if current is None:
                        raise DeltaError(
                            f"REF_DELTA base {base_id} is missing")
                chain += 1
                if chain > self.limits.max_delta_depth:
                    raise ResourceLimitError(
                        f"delta chain at object {idx} exceeds the "
                        f"{self.limits.max_delta_depth}-level limit")
            depth[idx] = chain
        self.max_depth_seen = max(depth.values(), default=0)

    def _unresolved(self, pending: dict) -> DeltaError:
        missing = sorted({self._ref_base[i] for i in pending
                          if i in self._ref_base
                          and self.infos.get(self._ref_base[i]) is None})
        if missing:
            return DeltaError(
                "REF_DELTA base object(s) missing from the bundle: "
                + ", ".join(missing))
        return DeltaError(
            "delta chain cannot be resolved: a base is itself an "
            "unresolved delta (cycle)")
