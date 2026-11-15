"""Git object payload parsing: commits, trees and blobs.

Objects are parsed from their *real* content.  A commit is split into its
raw header block and message exactly as Git does: a header line ends at
``\\n``, and a line starting with a space continues the previous header
value (multi-line headers such as ``gpgsig``).  Unknown headers are
preserved verbatim.  The tree is parsed without ever converting a path to
text: names are kept as raw bytes and each component is also exported as
base64 for a lossless, newline-safe representation.

The commit's parent list comes only from the ``parent`` headers, in their
order.  Timestamps are never used to infer ancestry, and the root tree is
never treated as a business file.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Optional

from .errors import ObjectFormatError, TreeFormatError
from .pack import object_id_for

MODE_REGULAR = "100644"
MODE_EXECUTABLE = "100755"
MODE_DIRECTORY = "40000"
MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"
TREE_FILE_MODES = frozenset({MODE_REGULAR, MODE_EXECUTABLE,
                             MODE_SYMLINK, MODE_GITLINK})
MODE_KINDS = {
    MODE_REGULAR: "file",
    MODE_EXECUTABLE: "executable",
    MODE_SYMLINK: "symlink",
    MODE_GITLINK: "gitlink",
}


def decode_text(raw: bytes, what: str) -> str:
    """Decode a Git metadata field (not a path).

    Git stores author/committer/message bytes without a mandated encoding.
    We try UTF-8 strictly, then fall back to Latin-1 so no byte is lost;
    the caller can tell which happened via :func:`text_encoding`.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def text_encoding(raw: bytes) -> str:
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


@dataclass
class CommitHeaders:
    tree: str
    parents: list[str]
    author_raw: str
    committer_raw: str
    author: dict
    committer: dict
    extra_headers: list[dict]       # [{name, raw_value, values}]
    message: str
    message_bytes: bytes
    #: headers in original order, values with continuation lines joined
    order: list[str] = field(default_factory=list)
    #: raw ``name``/``value`` bytes in original order, continuations joined
    #: with "\n"; this is the lossless header block (minus the blank line)
    raw_headers: list[tuple[str, bytes]] = field(default_factory=list)
    #: exact bytes before the message separator
    raw_header_block: bytes = b""


def parse_identity(value: str, what: str, object_id: str) -> dict:
    """Parse ``Name <email> <ts> <tz>`` while keeping the raw header."""
    raw = value
    lt = value.rfind(" <")
    gt = value.rfind("> ")
    if lt < 0 or gt < 0 or gt < lt:
        raise ObjectFormatError(
            f"commit {object_id}: malformed {what} line {raw[:120]!r}")
    name = value[:lt]
    email = value[lt + 2:gt]
    rest = value[gt + 2:].strip()
    parts = rest.split(" ")
    if len(parts) != 2:
        raise ObjectFormatError(
            f"commit {object_id}: malformed {what} timestamp {rest!r}")
    ts_text, tz = parts
    if not (ts_text.isdigit() or (ts_text.startswith("-")
                                  and ts_text[1:].isdigit())):
        raise ObjectFormatError(
            f"commit {object_id}: malformed {what} timestamp {ts_text!r}")
    sign = -1 if tz.startswith("-") else 1
    body = tz[1:] if tz[:1] in "+-" else tz
    if len(body) != 4 or not body.isdigit():
        raise ObjectFormatError(
            f"commit {object_id}: malformed {what} timezone {tz!r}")
    offset_minutes = sign * (int(body[:2]) * 60 + int(body[2:]))
    return {
        "name": decode_text(name.encode("latin-1"), what),
        "email": decode_text(email.encode("latin-1"), what),
        "timestamp": int(ts_text),
        "tz_offset_minutes": offset_minutes,
        "tz": tz,
        "raw": raw,
    }


def parse_commit(content: bytes, object_id: str = "") -> CommitHeaders:
    """Parse a commit payload, splitting headers from the message at the
    first empty line and honouring continuation lines."""
    sep = content.find(b"\n\n")
    if sep < 0:
        header_block = content
        message = b""
    else:
        header_block = content[:sep]
        message = content[sep + 2:]
    if b"\x00" in header_block:
        raise ObjectFormatError(
            f"commit {object_id}: NUL byte in the header block")
    lines = header_block.split(b"\n") if header_block else []
    raw_headers: list[tuple[str, bytes]] = []
    current_name: Optional[str] = None
    current_value = bytearray()

    def flush():
        nonlocal current_name, current_value
        if current_name is not None:
            raw_headers.append((current_name, bytes(current_value)))
        current_name, current_value = None, bytearray()

    for line in lines:
        if line.startswith(b" "):
            if current_name is None:
                raise ObjectFormatError(
                    f"commit {object_id}: header continuation without a "
                    "preceding header")
            current_value += b"\n" + line[1:]
            continue
        flush()
        if not line:
            raise ObjectFormatError(
                f"commit {object_id}: empty line inside the header block")
        sp = line.find(b" ")
        if sp <= 0:
            raise ObjectFormatError(
                f"commit {object_id}: malformed header line "
                f"{line[:80]!r}")
        name = line[:sp].decode("ascii", "replace")
        current_name = name
        current_value = bytearray(line[sp + 1:])
    flush()

    tree = None
    parents: list[str] = []
    author_raw = None
    committer_raw = None
    extra: list[dict] = []
    order: list[str] = []
    for name, value in raw_headers:
        text = decode_text(value, name)
        order.append(name)
        if name == "tree":
            if tree is not None:
                raise ObjectFormatError(
                    f"commit {object_id}: duplicate tree header")
            tree = _require_oid(text.strip(), object_id, "tree")
        elif name == "parent":
            parents.append(_require_oid(text.strip(), object_id, "parent"))
        elif name == "author":
            if author_raw is not None:
                raise ObjectFormatError(
                    f"commit {object_id}: duplicate author header")
            author_raw = text
        elif name == "committer":
            if committer_raw is not None:
                raise ObjectFormatError(
                    f"commit {object_id}: duplicate committer header")
            committer_raw = text
        else:
            extra.append({
                "name": name,
                "raw_value": text,
                "values": text.split("\n"),
            })
    if tree is None:
        raise ObjectFormatError(
            f"commit {object_id}: missing tree header")
    if author_raw is None or committer_raw is None:
        raise ObjectFormatError(
            f"commit {object_id}: missing author or committer header")
    if len(set(parents)) != len(parents):
        raise ObjectFormatError(
            f"commit {object_id}: duplicate parent entries")
    return CommitHeaders(
        tree=tree, parents=parents, author_raw=author_raw,
        committer_raw=committer_raw,
        author=parse_identity(author_raw, "author", object_id),
        committer=parse_identity(committer_raw, "committer", object_id),
        extra_headers=extra, message=decode_text(message, "message"),
        message_bytes=message, order=order,
        raw_headers=raw_headers, raw_header_block=header_block)


def _require_oid(text: str, commit_id: str, what: str) -> str:
    if len(text) != 40 or not all(c in "0123456789abcdef"
                                  for c in text):
        raise ObjectFormatError(
            f"commit {commit_id}: {what} header is not a 40-hex SHA-1: "
            f"{text[:80]!r}")
    return text


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: bytes
    object_id: str

    @property
    def kind(self) -> str:
        if self.mode == MODE_DIRECTORY:
            return "tree"
        return MODE_KINDS[self.mode]

    @property
    def is_tree(self) -> bool:
        return self.mode == MODE_DIRECTORY

    @property
    def name_b64(self) -> str:
        return base64.b64encode(self.name).decode("ascii")

    def as_dict(self) -> dict:
        return {
            "name_b64": self.name_b64,
            "name_display": display_name(self.name),
            "mode": self.mode,
            "kind": self.kind,
            "object_id": self.object_id,
        }


def display_name(name: bytes) -> str:
    """A human-readable, safely escaped rendering of a raw path component."""
    try:
        text = name.decode("utf-8")
    except UnicodeDecodeError:
        return "b64:" + base64.b64encode(name).decode("ascii")
    out = []
    for ch in text:
        code = ord(ch)
        if ch.isprintable() and ch not in "\\":
            out.append(ch)
        elif ch == "\\":
            out.append("\\\\")
        elif code <= 0xFF:
            out.append("\\x%02x" % code)
        else:
            out.append("\\u%04x" % code)
    return "".join(out)


def parse_tree(content: bytes, object_id: str = "") -> list[TreeEntry]:
    """Parse a tree payload into raw-name entries.

    Entries must be sorted by name bytes as Git itself requires; duplicate
    names, empty names, slash-containing names and the exact components
    ``.``/``..`` are rejected.  The mode must be one of the four legal
    file modes or the directory mode.
    """
    entries: list[TreeEntry] = []
    pos = 0
    n = len(content)
    seen: set[bytes] = set()
    while pos < n:
        sp = content.find(b" ", pos)
        if sp < 0:
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} has no mode "
                "separator")
        mode_raw = content[pos:sp]
        if not mode_raw or not all(48 <= b <= 57 for b in mode_raw):
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} has a non-numeric "
                f"mode {mode_raw[:20]!r}")
        mode_text = mode_raw.decode("ascii")
        # Git also writes zero-padded 6-digit modes in some old repos.
        stripped = mode_text.lstrip("0") or "0"
        if mode_text not in TREE_FILE_MODES and mode_text != MODE_DIRECTORY:
            if stripped in TREE_FILE_MODES:
                mode_text = stripped
            elif stripped == MODE_DIRECTORY:
                mode_text = MODE_DIRECTORY
            else:
                raise TreeFormatError(
                    f"tree {object_id}: invalid entry mode {mode_text!r}")
        nul = content.find(b"\x00", sp + 1)
        if nul < 0:
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} has an unterminated "
                "name")
        name = content[sp + 1:nul]
        if not name:
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} has an empty name")
        if b"/" in name:
            raise TreeFormatError(
                f"tree {object_id}: entry name contains '/': "
                f"{name[:80]!r}")
        if name in (b".", b".."):
            raise TreeFormatError(
                f"tree {object_id}: entry name is the reserved component "
                f"{name!r}")
        if name in seen:
            raise TreeFormatError(
                f"tree {object_id}: duplicate entry name "
                f"{name[:80]!r}")
        seen.add(name)
        if nul + 21 > n:
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} is truncated "
                "before its object id")
        raw_oid = content[nul + 1:nul + 21]
        oid = raw_oid.hex()
        if len(oid) != 40:
            raise TreeFormatError(
                f"tree {object_id}: entry at byte {pos} has a truncated "
                "object id")
        entries.append(TreeEntry(mode_text, bytes(name), oid))
        pos = nul + 21
    # Git's canonical order is by raw name bytes, trees sorted as if the
    # name had a trailing '/'.  We do not require a particular order to
    # *read* a tree, but the canonical writer order is exposed for diff
    # stability; a tree that is not in canonical order is still readable.
    return entries


def tree_sort_key(entry: TreeEntry) -> bytes:
    name = entry.name + (b"/" if entry.is_tree else b"")
    return name


def blob_object_id(content: bytes) -> str:
    from .bundle import OBJ_BLOB
    return object_id_for(OBJ_BLOB, content)
