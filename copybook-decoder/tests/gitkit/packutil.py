"""Raw Git pack construction helpers for tests and fixture generators.

These build bytes by hand so tests can place REF_DELTA objects *before*
their bases, mix REF/OFS chains, and corrupt specific fields without
going through git.  Only the standard library is used.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7


def object_id(obj_type: int, content: bytes) -> str:
    name = {OBJ_COMMIT: "commit", OBJ_TREE: "tree", OBJ_BLOB: "blob",
            OBJ_TAG: "tag"}[obj_type]
    h = hashlib.sha1()
    h.update(f"{name} {len(content)}\0".encode("ascii"))
    h.update(content)
    return h.hexdigest()


def _entry_header(obj_type: int, size: int) -> bytes:
    out = bytearray()
    c = (obj_type << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


def _ofs_distance(distance: int) -> bytes:
    out = bytearray([distance & 0x7F])
    distance >>= 7
    while distance:
        distance -= 1
        out.insert(0, 0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(out)


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def make_delta(base: bytes, target: bytes) -> bytes:
    """A simple but valid delta: copy the common prefix, insert the rest."""
    src_len = len(base)
    dst_len = len(target)
    out = bytearray()
    out += encode_varint(src_len)
    out += encode_varint(dst_len)
    prefix = 0
    limit = min(len(base), len(target))
    while prefix < limit and base[prefix] == target[prefix]:
        prefix += 1
    pos = 0
    if prefix:
        # copy [0, prefix) using offset+size bytes
        out.append(0x80 | 0x01 | 0x10 | 0x20)  # offset byte 0, size b0,b1
        out += bytes([0x00])
        out += bytes([prefix & 0xFF, (prefix >> 8) & 0x7F])
        pos = prefix
    remaining = target[pos:]
    for start in range(0, len(remaining), 127):
        chunk = remaining[start:start + 127]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def build_pack(entries, *, trailer_override: bytes = None,
               object_count_override: int = None,
               version: int = 2) -> bytes:
    """Assemble a pack.

    Each entry is a dict with ``type`` and either ``content`` (for
    non-delta objects) or ``base`` (``("#ref", oid)`` / ``("#ofs", index)``)
    plus ``delta`` bytes.  ``index`` is assigned in list order, so a
    REF_DELTA can precede its base.
    """
    body = bytearray(b"PACK")
    body += struct.pack(">II", version,
                        len(entries) if object_count_override is None
                        else object_count_override)
    offsets = []
    for entry in entries:
        offsets.append(len(body))
        if entry["type"] in (OBJ_OFS_DELTA, OBJ_REF_DELTA):
            payload = entry["delta"]
            body += _entry_header(
                entry["type"],
                entry.get("declared_size", len(payload)))
            if entry["type"] == OBJ_OFS_DELTA:
                base_index = entry["base"][1]
                if not 0 <= base_index < len(offsets) - 1:
                    raise ValueError(
                        f"OFS_DELTA base index {base_index} is not an "
                        "earlier entry")
                distance = offsets[-1] - offsets[base_index]
                body += _ofs_distance(distance)
            else:
                body += bytes.fromhex(entry["base"][1])
        else:
            payload = entry["content"]
            body += _entry_header(
                entry["type"], entry.get("declared_size", len(payload)))
        body += zlib.compress(payload)
    if trailer_override is not None:
        trailer = trailer_override
    else:
        trailer = hashlib.sha1(bytes(body)).digest()
    return bytes(body) + trailer


def finalize_bundle(header: bytes, pack_trailer_override: bytes = None,
                    *, extra_tail: bytes = b"", pack_bytes=None) -> bytes:
    """Append a correct pack trailer to *header + pack_bytes*.

    *pack_trailer_override* lets a test write a deliberately wrong trailer
    while keeping the rest of the file well framed.
    """
    data = header + pack_bytes
    trailer = (hashlib.sha1(pack_bytes).digest()
               if pack_trailer_override is None else pack_trailer_override)
    return data + trailer + extra_tail


def build_bundle(header_lines, pack_bytes, *, trailer_override=None,
                 extra_tail=b"") -> bytes:
    header = b"# v2 git bundle\n"
    for line in header_lines:
        header += line if isinstance(line, bytes) else line.encode()
        header += b"\n"
    header += b"\n"
    # The bundle's trailing 20 bytes are the pack's own SHA-1 (covering the
    # pack bytes only), exactly as git writes them.
    return header + pack_bytes + extra_tail


def tree_bytes(entries) -> bytes:
    """entries: iterable of (mode_str, name_bytes, oid_hex)."""
    out = bytearray()
    for mode, name, oid in entries:
        out += mode.encode() + b" " + name + b"\x00" + bytes.fromhex(oid)
    return bytes(out)


def commit_bytes(tree_hex: str, parents, author: str, committer: str,
                 message: str, extra_headers=None,
                 message_bytes=None) -> bytes:
    out = bytearray()
    out += f"tree {tree_hex}\n".encode()
    for parent in parents:
        out += f"parent {parent}\n".encode()
    out += f"author {author}\n".encode()
    out += f"committer {committer}\n".encode()
    for name, value in (extra_headers or []):
        out += name.encode() + b" " + value.encode() + b"\n"
    out += b"\n"
    out += message.encode() if message_bytes is None else message_bytes
    return bytes(out)
