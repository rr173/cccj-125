"""Full-path file changes between a commit and each of its parents.

No rename detection is attempted: files are compared by their complete
raw-component path.  Each change carries independent ``content`` and
``mode`` flags, so a file that changes both shows both.  A root commit is
compared against an empty directory (everything is an addition).  A merge
is compared against *every* parent separately, keeping the parent index
and parent id; there is no synthetic combined parent.  When one side is a
file and the other is a directory of the same name, the file leaf is
deleted and the leaves under the new directory are added.

Two paths that point at the same blob are still two file facts.  Trees
and blobs are cached by object id only; the path is never cached with
them, so a tree mounted under several prefixes cannot leak a prefix.
"""

from __future__ import annotations

import base64
from typing import Optional

from ..gitkit.graph import CommitGraph, GitView, path_dict
from ..gitkit.errors import HandoffQueryError

KIND_ADDED = "added"
KIND_DELETED = "deleted"
KIND_MODIFIED = "modified"
KIND_UNCHANGED = "unchanged"


def file_map(view: GitView, commit_id: str) -> dict[tuple[bytes, ...],
                                                   dict]:
    """``raw path tuple -> file fact`` for one commit."""
    out: dict[tuple[bytes, ...], dict] = {}
    for fact in view.list_files(commit_id):
        raw = tuple(base64.b64decode(c) for c in fact["path"]["components_b64"])
        out[raw] = fact
    return out


def diff_against(
    view: GitView,
    commit_id: str,
    parent_id: Optional[str],
    *,
    parent_index: Optional[int],
    include_unchanged: bool = False,
) -> dict:
    """Changes from *parent_id* (None = empty directory) to *commit_id*."""
    target = file_map(view, commit_id)
    if parent_id is None:
        base: dict[tuple[bytes, ...], dict] = {}
    else:
        base = file_map(view, parent_id)
    changes: list[dict] = []
    for raw in sorted(set(base) | set(target)):
        old = base.get(raw)
        new = target.get(raw)
        if old is None:
            changes.append(_change(KIND_ADDED, raw, None, new))
            continue
        if new is None:
            changes.append(_change(KIND_DELETED, raw, old, None))
            continue
        content_changed = old["object_id"] != new["object_id"]
        mode_changed = old["mode"] != new["mode"]
        if not content_changed and not mode_changed:
            if include_unchanged:
                changes.append(_change(KIND_UNCHANGED, raw, old, new))
            continue
        changes.append({
            "change": KIND_MODIFIED,
            "path": path_dict(raw),
            "before": _side(old),
            "after": _side(new),
            "content_changed": content_changed,
            "mode_changed": mode_changed,
            "old_content_changed": content_changed,
            "new_content_changed": content_changed,
        })
    return {
        "commit": commit_id,
        "parent_index": parent_index,
        "parent": parent_id,
        "change_count": len(changes),
        "changes": changes,
    }


def diff_all_parents(view: GitView, graph: CommitGraph,
                     commit_id: str) -> list[dict]:
    """One diff row per parent (or one against empty for a root commit)."""
    node = graph.nodes.get(commit_id)
    if node is None:
        raise HandoffQueryError(
            f"commit {commit_id} is not in the reachable set")
    if not node.parents:
        return [diff_against(view, commit_id, None, parent_index=None)]
    return [diff_against(view, commit_id, parent, parent_index=index)
            for index, parent in enumerate(node.parents)]


def file_versions(graph: CommitGraph, commit_id: str) -> dict:
    """The complete file listing of one commit as a report body."""
    facts = graph.view.list_files(commit_id)
    return {
        "commit": commit_id,
        "file_count": len(facts),
        "files": facts,
    }


def _change(kind: str, raw: tuple[bytes, ...], old: Optional[dict],
            new: Optional[dict]) -> dict:
    if kind == KIND_ADDED:
        return {
            "change": KIND_ADDED,
            "path": path_dict(raw),
            "before": None,
            "after": _side(new),
            "content_changed": True,
            "mode_changed": False,
        }
    return {
        "change": KIND_DELETED,
        "path": path_dict(raw),
        "before": _side(old),
        "after": None,
        "content_changed": True,
        "mode_changed": False,
    }


def _side(fact: dict) -> dict:
    return {
        "mode": fact["mode"],
        "kind": fact["kind"],
        "type": fact["type"],
        "object_id": fact["object_id"],
        "size": fact["size"],
        "content_sha256": fact["content_sha256"],
    }
