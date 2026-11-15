"""Reachable commit graph and file-version queries.

The graph is built by traversing every ref, never by sorting commit
timestamps: a child may carry an earlier date than its parent and must
still come after it.  Emission order is a topological order in which a
commit appears only after all of its parents; ready nodes are popped in
object-id byte order.  A commit reachable from several refs is listed
once and keeps every ref that reaches it.

Trees are walked without following gitlinks (an external commit id is
reported, never entered) and without reading a symlink's target from the
host disk.  Paths are carried as a tuple of raw byte components; the
lossless representation is one base64 string per component, so a newline
or a non-UTF-8 byte in a name can never split a path.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .bundle import OBJ_BLOB, OBJ_COMMIT, OBJ_TREE
from .errors import (CommitGraphError, HandoffQueryError, TreeFormatError)
from .objects import (MODE_DIRECTORY, CommitHeaders, TreeEntry,
                      display_name, parse_commit, parse_tree)
from .pack import PackStore, object_id_for


def encode_path(parts: Iterable[bytes]) -> str:
    """Lossless path encoding: each raw component base64-encoded, joined by
    ``/``.  A literal ``/`` never appears inside a component's base64
    alphabet, so the encoding is unambiguous."""
    return "/".join(base64.b64encode(p).decode("ascii") for p in parts)


def decode_path(encoded: str) -> tuple[bytes, ...]:
    """Inverse of :func:`encode_path`; rejects malformed input.

    Every ``/`` separates components, so an empty component, missing
    padding or a non-canonical re-encoding is refused rather than
    guessed at.
    """
    if encoded == "":
        return ()
    parts = []
    for chunk in encoded.split("/"):
        if not chunk:
            raise HandoffQueryError("path contains an empty component")
        try:
            raw = base64.b64decode(chunk.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise HandoffQueryError(
                f"invalid base64 path component {chunk!r}: {exc}") from None
        if not raw:
            raise HandoffQueryError(
                "path component decodes to an empty name")
        if base64.b64encode(raw).decode("ascii") != chunk:
            raise HandoffQueryError(
                f"path component {chunk!r} is not canonical base64")
        parts.append(raw)
    return tuple(parts)


def path_display(parts: Iterable[bytes]) -> str:
    return "/".join(display_name(p) for p in parts)


def path_dict(parts: tuple[bytes, ...]) -> dict:
    return {
        "b64": encode_path(parts),
        "components_b64": [base64.b64encode(p).decode("ascii")
                           for p in parts],
        "display": path_display(parts),
    }


@dataclass
class CommitNode:
    object_id: str
    headers: CommitHeaders
    refs: list[str] = field(default_factory=list)

    @property
    def tree(self) -> str:
        return self.headers.tree

    @property
    def parents(self) -> list[str]:
        return self.headers.parents

    def as_dict(self) -> dict:
        h = self.headers
        return {
            "object_id": self.object_id,
            "tree": h.tree,
            "parents": list(h.parents),
            "refs": list(self.refs),
            "author": h.author,
            "committer": h.committer,
            "author_raw": h.author_raw,
            "committer_raw": h.committer_raw,
            "extra_headers": h.extra_headers,
            "raw_headers": [
                {"name": name,
                 "value_b64": base64.b64encode(value).decode("ascii")}
                for name, value in h.raw_headers],
            "message": h.message,
            "message_b64": base64.b64encode(h.message_bytes).decode(
                "ascii"),
            "header_order": h.order,
        }


class GitView:
    """Read-only view of commits, trees and blobs inside one bundle."""

    def __init__(self, store: PackStore, refs: dict[str, str]):
        self.store = store
        self.refs = dict(refs)
        self._commits: dict[str, CommitNode] = {}
        self._trees: dict[str, list[TreeEntry]] = {}
        self._blobs: dict[str, bytes] = {}
        self._blob_digests: dict[str, str] = {}
        self._listing_cache: dict[str, list[dict]] = {}

    # -- object helpers -------------------------------------------------
    def commit(self, object_id: str) -> CommitNode:
        node = self._commits.get(object_id)
        if node is not None:
            return node
        info = self.store.get(object_id)
        if info is None:
            raise CommitGraphError(
                f"commit {object_id} is not present in the bundle")
        if info.obj_type != OBJ_COMMIT:
            raise CommitGraphError(
                f"object {object_id} is a {info.type_name}, expected commit")
        content = info.path.read_bytes()
        if object_id_for(OBJ_COMMIT, content) != object_id:
            raise CommitGraphError(
                f"commit {object_id} content does not hash to its id")
        node = CommitNode(object_id, parse_commit(content, object_id))
        self._commits[object_id] = node
        return node

    def tree_entries(self, object_id: str) -> list[TreeEntry]:
        cached = self._trees.get(object_id)
        if cached is not None:
            return cached
        info = self.store.get(object_id)
        if info is None:
            raise CommitGraphError(
                f"tree {object_id} is not present in the bundle")
        if info.obj_type != OBJ_TREE:
            raise CommitGraphError(
                f"object {object_id} is a {info.type_name}, expected tree")
        content = info.path.read_bytes()
        if object_id_for(OBJ_TREE, content) != object_id:
            raise CommitGraphError(
                f"tree {object_id} content does not hash to its id")
        entries = parse_tree(content, object_id)
        for entry in entries:
            target = self.store.get(entry.object_id)
            expected = OBJ_TREE if entry.is_tree else (
                OBJ_BLOB if entry.kind != "gitlink" else None)
            if expected is None:
                continue  # gitlink: external id, must not be required here
            if target is None:
                raise TreeFormatError(
                    f"tree {object_id}: entry {entry.name!r} points at "
                    f"{entry.object_id}, which is not present in the "
                    "bundle")
            if target.obj_type != expected:
                raise TreeFormatError(
                    f"tree {object_id}: entry {entry.name!r} is a "
                    f"{entry.kind} but points at a {target.type_name}")
        self._trees[object_id] = entries
        return entries

    def blob(self, object_id: str) -> bytes:
        cached = self._blobs.get(object_id)
        if cached is not None:
            return cached
        info = self.store.get(object_id)
        if info is None:
            raise HandoffQueryError(
                f"blob {object_id} is not present in the bundle")
        if info.obj_type != OBJ_BLOB:
            raise HandoffQueryError(
                f"object {object_id} is a {info.type_name}, expected blob")
        content = info.path.read_bytes()
        if object_id_for(OBJ_BLOB, content) != object_id:
            raise HandoffQueryError(
                f"blob {object_id} content does not hash to its id")
        self._blobs[object_id] = content
        return content

    def blob_digest(self, object_id: str) -> str:
        digest = self._blob_digests.get(object_id)
        if digest is None:
            info = self.store.get(object_id)
            if info is not None and info.sha256 is not None:
                digest = info.sha256
            else:
                digest = hashlib.sha256(self.blob(object_id)).hexdigest()
            self._blob_digests[object_id] = digest
        return digest

    # -- reachability ---------------------------------------------------
    def resolve_ref(self, name: str) -> str:
        if name not in self.refs:
            raise HandoffQueryError(
                f"ref {name!r} is not advertised by this bundle")
        return self.refs[name]

    def require_commit(self, commit_id: str) -> CommitNode:
        return self._require_commit(commit_id)

    def load_graph(self) -> "CommitGraph":
        """Traverse every ref and build the commit parent graph."""
        roots: dict[str, list[str]] = {}
        for name in sorted(self.refs):
            oid = self.refs[name]
            info = self.store.get(oid)
            if info is None:
                raise CommitGraphError(
                    f"ref {name} points at {oid}, which is not present in "
                    "the bundle")
            if info.obj_type != OBJ_COMMIT:
                raise CommitGraphError(
                    f"ref {name} points at a {info.type_name}, but only "
                    "commit refs are supported")
            roots.setdefault(oid, []).append(name)

        nodes: dict[str, CommitNode] = {}
        stack = list(roots)
        while stack:
            oid = stack.pop()
            if oid in nodes:
                continue
            node = self.commit(oid)
            nodes[oid] = node
            for parent in node.parents:
                pinfo = self.store.get(parent)
                if pinfo is None:
                    raise CommitGraphError(
                        f"commit {oid} has parent {parent}, which is not "
                        "present in the bundle")
                if pinfo.obj_type != OBJ_COMMIT:
                    raise CommitGraphError(
                        f"commit {oid} has parent {parent} of type "
                        f"{pinfo.type_name}, expected commit")
                stack.append(parent)
        for oid, names in roots.items():
            if oid not in nodes:
                raise CommitGraphError(
                    f"ref points at commit {oid} that was not reached")
        # Root trees must exist and be trees.
        for oid in nodes:
            node = nodes[oid]
            tinfo = self.store.get(node.tree)
            if tinfo is None:
                raise CommitGraphError(
                    f"commit {oid} has tree {node.tree}, which is not "
                    "present in the bundle")
            if tinfo.obj_type != OBJ_TREE:
                raise CommitGraphError(
                    f"commit {oid} has tree {node.tree} of type "
                    f"{tinfo.type_name}, expected tree")
        _check_acyclic(nodes)
        graph = CommitGraph(self, nodes, roots, self.refs)
        # Attach every ref that reaches each commit, not just the first.
        for name in sorted(self.refs):
            for oid in graph.walk_from(self.refs[name]):
                node = nodes[oid]
                if name not in node.refs:
                    node.refs.append(name)
        for node in nodes.values():
            node.refs.sort()
        graph.order = _topo_order(nodes)
        return graph

    # -- listings -------------------------------------------------------
    def list_files(self, commit_id: str) -> list[dict]:
        """Every file fact reachable from the commit's root tree.

        The root tree itself is never reported as a file.  Trees shared by
        several commits (or a tree mounted at two paths) are cached by
        object id, but the path prefix is always passed in from the
        caller, so a cached tree never leaks another mount's prefix.
        """
        cached = self._listing_cache.get(commit_id)
        if cached is not None:
            return cached
        node = self._require_commit(commit_id)
        facts: list[dict] = []
        self._walk_tree(node.tree, (), facts, set())
        facts.sort(key=lambda f: f["path"]["b64"])
        self._listing_cache[commit_id] = facts
        return facts

    def _walk_tree(self, tree_id: str, prefix: tuple[bytes, ...],
                   out: list[dict], ancestors: set[str]) -> None:
        if tree_id in ancestors:
            raise TreeFormatError(
                f"tree {tree_id} is its own descendant (cycle) at "
                f"{path_display(prefix) or '<root>'}")
        ancestors.add(tree_id)
        try:
            for entry in self.tree_entries(tree_id):
                parts = prefix + (entry.name,)
                if entry.mode == MODE_DIRECTORY:
                    self._walk_tree(entry.object_id, parts, out, ancestors)
                else:
                    # gitlink, symlink, regular and executable are all
                    # single file facts; a gitlink is never entered.
                    out.append(self._file_fact(parts, entry))
        finally:
            ancestors.discard(tree_id)

    def _file_fact(self, parts: tuple[bytes, ...],
                   entry: TreeEntry) -> dict:
        """A file fact from staged metadata only.

        Blob bytes are never read here: size and digest were computed
        while the object was staged, so listing a large commit does not
        pull its files into memory.
        """
        if entry.kind == "gitlink":
            size = None
            digest = None
            node_type = "gitlink"
        else:
            info = self.store.get(entry.object_id)
            if info is None:  # pragma: no cover - checked by tree_entries
                raise TreeFormatError(
                    f"blob {entry.object_id} is missing from the bundle")
            size = info.size
            digest = info.sha256
            node_type = "blob"
        return {
            "path": path_dict(parts),
            "mode": entry.mode,
            "kind": entry.kind,
            "object_id": entry.object_id,
            "size": size,
            "content_sha256": digest,
            "type": node_type,
        }

    def lookup_path(self, commit_id: str,
                    parts: tuple[bytes, ...]) -> Optional[dict]:
        """Return the file fact at *parts* in *commit_id*, or None.

        A path that crosses a gitlink never descends into the external
        repository.  The root tree is not a file.
        """
        node = self._require_commit(commit_id)
        if not parts:
            return None
        tree_id = node.tree
        for index, part in enumerate(parts):
            if not part or b"/" in part or part in (b".", b".."):
                raise TreeFormatError(
                    f"path component {part!r} is not a legal tree name")
            entries = self.tree_entries(tree_id)
            match = next((e for e in entries if e.name == part), None)
            if match is None:
                return None
            last = index == len(parts) - 1
            if match.mode == MODE_DIRECTORY:
                if last:
                    return None
                tree_id = match.object_id
                continue
            if not last:
                return None
            return self._file_fact(parts, match)
        return None

    def _require_commit(self, commit_id: str) -> CommitNode:
        if len(commit_id) != 40 or not all(
                c in "0123456789abcdef" for c in commit_id):
            raise HandoffQueryError(
                f"commit id must be 40 lowercase hex characters: "
                f"{commit_id!r}")
        node = self._commits.get(commit_id)
        if node is None:
            raise HandoffQueryError(
                f"commit {commit_id} is not in the reachable set")
        return node


class CommitGraph:
    """Result of :meth:`GitView.load_graph`."""

    def __init__(self, view: GitView, nodes: dict[str, CommitNode],
                 roots: dict[str, list[str]], refs: dict[str, str]):
        self.view = view
        self.nodes = nodes
        self.roots = roots
        self.refs = refs
        self.order: list[str] = []

    def commits_in_order(self) -> list[CommitNode]:
        return [self.nodes[oid] for oid in self.order]

    def walk_from(self, start: str) -> list[str]:
        out: list[str] = []
        stack = [start]
        seen: set[str] = set()
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            out.append(oid)
            stack.extend(self.nodes[oid].parents)
        return out

def _check_acyclic(nodes: dict[str, CommitNode]) -> None:
    color: dict[str, int] = {}
    for start in nodes:
        if color.get(start) == 2:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = 1
        while stack:
            oid, idx = stack.pop()
            parents = nodes[oid].parents
            if idx < len(parents):
                stack.append((oid, idx + 1))
                parent = parents[idx]
                state = color.get(parent, 0)
                if state == 1:
                    raise CommitGraphError(
                        f"commit graph contains a cycle: {parent} is both "
                        f"an ancestor and a descendant of {oid}")
                if state == 0:
                    color[parent] = 1
                    stack.append((parent, 0))
            else:
                color[oid] = 2


def _topo_order(nodes: dict[str, CommitNode]) -> list[str]:
    indegree = {oid: len(node.parents) for oid, node in nodes.items()}
    children: dict[str, list[str]] = {oid: [] for oid in nodes}
    for oid, node in nodes.items():
        for parent in node.parents:
            children[parent].append(oid)
    ready = [oid for oid, deg in indegree.items() if deg == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        oid = heapq.heappop(ready)
        order.append(oid)
        for child in children[oid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(nodes):
        raise CommitGraphError(
            "commit graph contains a cycle; topological order is undefined")
    return order
