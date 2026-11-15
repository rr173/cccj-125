"""Offline handoff intake: bundle -> queryable facts, then blob export.

A run does one of two things, and both are all-or-nothing:

``ingest``
    Validate the self-contained Git bundle v2, resolve every pack object
    (commit/tree/blob, OFS_DELTA and REF_DELTA in any order), rebuild the
    commit parent graph from every ref and write a self-describing
    directory:

    * ``handoff_report.json`` -- bundle digest/refs, object counts,
      limits, commit graph in parents-before-children order, per-commit
      file listings and per-parent diffs;
    * ``commits.jsonl`` -- one line per commit (identity, ordered parents,
      raw headers, message);
    * ``commits/<id>.files.jsonl`` -- the full file listing of a commit;
    * ``commits/<id>/diff-p<idx>-<parent>.jsonl`` -- the change list
      against that parent.

``export``
    Take one regular/executable blob at an exact path in one commit and
    write it as a new user-named file, plus an ``export_manifest.json``
    recording the bundle digest, commit/tree/blob ids, the raw path, the
    mode, the length and the content digest.  Nothing else is unpacked to
    disk, nothing is executed, symlinks and gitlinks are refused.

Every run stages objects under a unique subdirectory of ``temp_dir`` and
removes exactly that subdirectory in a ``finally`` block.  An output
directory that already exists is never overwritten, and the report is
written only after the complete validation succeeded: a damaged bundle
never leaves a "successful" report behind and never leaves a half
exported file at the destination.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Union

from .bundle import BundleLayout, extract_pack, parse_pack, read_bundle_layout
from .diff import diff_all_parents, file_versions
from .errors import (BundleFormatError, ExportError, HandoffQueryError)
from .graph import GitView, decode_path, encode_path, path_display
from .pack import Limits, PackStore, Staging

REPORT_JSON = "handoff_report.json"
COMMITS_JSONL = "commits.jsonl"
COMMITS_DIR = "commits"
EXPORT_MANIFEST_JSON = "export_manifest.json"

EXIT_OK = 0
EXIT_UNUSABLE = 2

DEFAULT_LIMITS = Limits()


def _resolve_limits(limits: Optional[Limits],
                    *, max_object_bytes: Optional[int],
                    max_total_bytes: Optional[int],
                    max_objects: Optional[int],
                    max_delta_depth: Optional[int]) -> Limits:
    if limits is not None:
        base = limits
    else:
        base = DEFAULT_LIMITS
    return Limits(
        max_object_bytes=(max_object_bytes if max_object_bytes is not None
                          else base.max_object_bytes),
        max_total_bytes=(max_total_bytes if max_total_bytes is not None
                         else base.max_total_bytes),
        max_objects=(max_objects if max_objects is not None
                     else base.max_objects),
        max_delta_depth=(max_delta_depth if max_delta_depth is not None
                         else base.max_delta_depth),
    )


class _Run:
    """Owns the staging directory and the validated bundle state."""

    def __init__(self, bundle_path, temp_dir, limits: Limits):
        self.bundle_path = Path(bundle_path)
        self.temp_base = Path(temp_dir)
        self.limits = limits
        self.staging_root: Optional[Path] = None
        self.staging: Optional[Staging] = None
        self.layout: Optional[BundleLayout] = None
        self.store: Optional[PackStore] = None
        self.view: Optional[GitView] = None
        self.graph = None

    def __enter__(self) -> "_Run":
        if not self.bundle_path.is_file():
            raise HandoffQueryError(
                f"bundle {self.bundle_path} does not exist")
        self.temp_base.mkdir(parents=True, exist_ok=True)
        self.staging_root = Path(tempfile.mkdtemp(
            prefix="git-handoff-", dir=self.temp_base))
        return self

    def open(self) -> "GitView":
        layout = read_bundle_layout(self.bundle_path)
        self.layout = layout
        if layout.pack_bytes == 0:
            raise BundleFormatError(
                "bundle contains no pack: a self-contained delivery must "
                "carry its objects")
        pack_path = self.staging_root / "incoming.pack"
        extract_pack(layout, pack_path)
        pack = parse_pack(pack_path, max_stream=self.limits.max_object_bytes,
                          max_total=self.limits.max_total_bytes)
        staging = Staging(self.staging_root / "objects-root",
                          max_bytes=self.limits.max_total_bytes)
        self.staging = staging
        store = PackStore(pack, staging, self.limits).load()
        self.store = store
        refs = {ref.name: ref.object_id for ref in layout.refs}
        if not refs:
            raise BundleFormatError(
                "bundle advertises no refs; nothing can be reached")
        view = GitView(store, refs)
        self.view = view
        return view

    def __exit__(self, exc_type, exc, tb):
        if self.staging_root is not None:
            shutil.rmtree(self.staging_root, ignore_errors=True)
        return False


def _bundle_summary(layout: BundleLayout) -> dict:
    return {
        "path": layout.path,
        "sha1": layout.digest,
        "size": layout.size,
        "header_bytes": layout.header_bytes,
        "pack_bytes": layout.pack_bytes,
        "pack_object_count": layout.object_count,
        "trailer_sha1": layout.trailer,
        "refs": [ref.as_dict() for ref in layout.refs],
    }


def inspect_handoff(bundle_path: Union[str, Path], *,
                    temp_dir: Union[str, Path],
                    max_object_bytes: Optional[int] = None,
                    max_total_bytes: Optional[int] = None,
                    max_objects: Optional[int] = None,
                    max_delta_depth: Optional[int] = None,
                    limits: Optional[Limits] = None,
                    commit_ids: Optional[Iterable[str]] = None,
                    include_diffs: bool = True) -> dict:
    """Validate *bundle_path* and return the full handoff report body.

    The report is in memory only; :func:`ingest_handoff` writes it.  Every
    reachable commit (or just *commit_ids*) is listed in full, and diffs
    are produced against each parent separately.
    """
    lim = _resolve_limits(limits, max_object_bytes=max_object_bytes,
                          max_total_bytes=max_total_bytes,
                          max_objects=max_objects,
                          max_delta_depth=max_delta_depth)
    with _Run(bundle_path, temp_dir, lim) as run:
        view = run.open()
        graph = view.load_graph()
        selected = _select_commits(graph, commit_ids)
        return _report_body(run, graph, selected,
                            include_diffs=include_diffs)


def _report_body(run: "_Run", graph, selected: list[str], *,
                 include_diffs: bool = True) -> dict:
    """Assemble the self-describing fact document for *selected* commits."""
    view = run.view
    commits = [graph.nodes[oid].as_dict() for oid in selected]
    listings = {oid: file_versions(graph, oid) for oid in selected}
    diffs = {}
    if include_diffs:
        diffs = {oid: diff_all_parents(view, graph, oid) for oid in selected}
    return {
        "format": "copybook-git-handoff/v1",
        "bundle": _bundle_summary(run.layout),
        "limits": run.limits.as_dict(),
        "counts": {
            "ref_count": len(run.layout.refs),
            "commit_count": len(graph.nodes),
            "listed_commit_count": len(selected),
            "object_count": len(run.store.infos),
            "delta_object_count": run.store.resolved_delta_count,
            "max_delta_depth_seen": run.store.max_depth_seen,
        },
        "resources": {
            "pack_bytes": run.layout.pack_bytes,
            "staged_bytes": run.staging.bytes_written,
            "staged_objects": len(run.store.infos),
        },
        "order": "parents-before-children, ready nodes by object-id bytes",
        # Ingesting facts says nothing about extracting a file: only
        # ``git-export`` / :func:`export_blob` can report an extraction,
        # and it does so in its own manifest.
        "extraction": {
            "performed": False,
            "note": "file contents were not extracted to the host file "
                    "system by this operation",
        },
        "commit_order": list(graph.order),
        "refs": _ref_control(graph),
        "commits": commits,
        "file_listings": listings,
        "diffs": diffs,
    }


def ingest_handoff(bundle_path: Union[str, Path], *,
                   output_dir: Union[str, Path],
                   temp_dir: Union[str, Path],
                   max_object_bytes: Optional[int] = None,
                   max_total_bytes: Optional[int] = None,
                   max_objects: Optional[int] = None,
                   max_delta_depth: Optional[int] = None,
                   limits: Optional[Limits] = None,
                   commit_ids: Optional[Iterable[str]] = None) -> dict:
    """Validate the bundle and write the handoff fact directory.

    Returns a bounded summary.  *output_dir* must not exist; a damaged
    bundle leaves no output directory behind, and the directory appears
    atomically only after the whole validation succeeded.
    """
    lim = _resolve_limits(limits, max_object_bytes=max_object_bytes,
                          max_total_bytes=max_total_bytes,
                          max_objects=max_objects,
                          max_delta_depth=max_delta_depth)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise HandoffQueryError(
            f"output directory {output_dir} already exists; refusing to "
            "overwrite a previous delivery")
    with _Run(bundle_path, temp_dir, lim) as run:
        view = run.open()
        graph = view.load_graph()
        selected = _select_commits(graph, commit_ids)
        report = _report_body(run, graph, selected)
        _write_output(output_dir, report)
        return {
            "conclusion": "ingested",
            "bundle_sha1": report["bundle"]["sha1"],
            "ref_count": report["counts"]["ref_count"],
            "commit_count": report["counts"]["commit_count"],
            "object_count": report["counts"]["object_count"],
            "listed_commit_count": report["counts"]["listed_commit_count"],
            "output_dir": str(output_dir),
            "report": str(output_dir / REPORT_JSON),
            "commits": str(output_dir / COMMITS_JSONL),
        }


def _select_commits(graph, commit_ids) -> list[str]:
    if commit_ids is None:
        return list(graph.order)
    selected = list(commit_ids)
    unknown = [oid for oid in selected if oid not in graph.nodes]
    if unknown:
        raise HandoffQueryError(
            "commit(s) not reachable from any advertised ref: "
            + ", ".join(unknown))
    rank = {oid: i for i, oid in enumerate(graph.order)}
    return sorted(selected, key=lambda o: rank[o])


def _ref_control(graph) -> list[dict]:
    """Commits that are a ref target, with every ref that reaches them."""
    rows = []
    for oid in graph.order:
        names = sorted(graph.nodes[oid].refs)
        if names:
            rows.append({"commit": oid, "refs": names})
    return rows


def _write_output(output_dir: Path, report: dict) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".handoff-out-",
                                    dir=output_dir.parent))
    try:
        (staging / COMMITS_DIR).mkdir(parents=True, exist_ok=True)
        (staging / REPORT_JSON).write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        with (staging / COMMITS_JSONL).open("w", encoding="utf-8") as fh:
            for commit in report["commits"]:
                fh.write(json.dumps(commit, ensure_ascii=False) + "\n")
        for oid, listing in report["file_listings"].items():
            path = staging / COMMITS_DIR / f"{oid}.files.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for fact in listing["files"]:
                    fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
            for diff in report["diffs"].get(oid, []):
                name = _diff_filename(diff)
                with (staging / COMMITS_DIR / name).open(
                        "w", encoding="utf-8") as fh:
                    for change in diff["changes"]:
                        fh.write(json.dumps(change, ensure_ascii=False)
                                 + "\n")
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _diff_filename(diff: dict) -> str:
    oid = diff["commit"]
    if diff["parent"] is None:
        return f"{oid}.diff-root.jsonl"
    return f"{oid}.diff-p{diff['parent_index']}-{diff['parent']}.jsonl"


def export_blob(bundle_path: Union[str, Path], *,
                commit_id: str,
                path: Union[str, Iterable[bytes]],
                destination: Union[str, Path],
                temp_dir: Union[str, Path],
                encoding: str = "b64",
                max_object_bytes: Optional[int] = None,
                max_total_bytes: Optional[int] = None,
                max_objects: Optional[int] = None,
                max_delta_depth: Optional[int] = None,
                limits: Optional[Limits] = None) -> dict:
    """Export one regular/executable blob of a commit to a new file.

    *path* is either a lossless base64 path string (``encoding="b64"``) or
    an iterable of raw byte components.  The destination must not exist;
    it is written to a temporary sibling and atomically renamed, so a
    failure never leaves a partial file and never touches existing bytes.
    Symlinks and gitlinks are queryable but never exported.
    """
    lim = _resolve_limits(limits, max_object_bytes=max_object_bytes,
                          max_total_bytes=max_total_bytes,
                          max_objects=max_objects,
                          max_delta_depth=max_delta_depth)
    if encoding not in ("b64", "raw"):
        raise ExportError("path encoding must be 'b64' or 'raw'")
    if encoding == "b64":
        if not isinstance(path, str):
            raise ExportError(
                "a base64 path must be a single string; use encoding='raw' "
                "for byte components")
        parts = decode_path(path)
    else:
        if isinstance(path, str):
            raise ExportError(
                "a raw path must be an iterable of byte components")
        parts = tuple(bytes(p) for p in path)
    if not parts:
        raise ExportError("the root tree is not a file and cannot be exported")

    dest = Path(destination)
    if dest.exists() or dest.is_symlink():
        raise ExportError(
            f"destination {dest} already exists; refusing to overwrite")
    if dest.is_dir():
        raise ExportError(f"destination {dest} is a directory")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with _Run(bundle_path, temp_dir, lim) as run:
        view = run.open()
        graph = view.load_graph()
        if commit_id not in graph.nodes:
            raise ExportError(
                f"commit {commit_id} is not reachable from any advertised "
                "ref")
        fact = view.lookup_path(commit_id, parts)
        if fact is None:
            raise ExportError(
                f"path {path_display(parts) or '<root>'} does not exist in "
                f"commit {commit_id}")
        if fact["kind"] == "gitlink":
            raise ExportError(
                f"path {path_display(parts)} is a gitlink (external commit "
                f"{fact['object_id']}); it is queryable but cannot be "
                "exported as a regular file")
        if fact["kind"] == "symlink":
            raise ExportError(
                f"path {path_display(parts)} is a symlink; only its target "
                "text is stored and it is not exported as a regular file")
        if fact["kind"] not in ("file", "executable"):
            raise ExportError(
                f"path {path_display(parts)} is a {fact['kind']}, not a "
                "regular or executable file")
        info = run.store.require(fact["object_id"])
        if info.obj_type != 3:  # OBJ_BLOB
            raise ExportError(
                f"path {path_display(parts)} points at a "
                f"{info.type_name}, not a blob")
        content = info.path.read_bytes()
        if len(content) != fact["size"]:
            raise ExportError(
                "source bundle is damaged: staged blob length does not "
                "match the tree entry")
        stored_id = _git_blob_id(content)
        if stored_id != fact["object_id"]:
            raise ExportError(
                f"source bundle is damaged: blob content hashes to "
                f"{stored_id}, expected {fact['object_id']}")
        tree_chain = _tree_chain(view, commit_id, parts)
        manifest_path = dest.parent / f"{dest.name}.export_manifest.json"
        if manifest_path.exists():
            # The manifest is a sidecar; never clobber a previous one.
            raise ExportError(
                f"export manifest {manifest_path} already exists; a new "
                "export needs a new destination name")
        actual_sha = hashlib.sha256(content).hexdigest()
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".partial", dir=dest.parent)
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        manifest = {
            "format": "copybook-git-export/v1",
            "conclusion": "exported",
            "bundle": {
                "path": str(Path(bundle_path)),
                "sha1": run.layout.digest,
                "size": run.layout.size,
                "trailer_sha1": run.layout.trailer,
            },
            "commit": commit_id,
            "tree_chain": tree_chain,
            "blob": {
                "object_id": fact["object_id"],
                "size": len(content),
                "sha256": actual_sha,
            },
            "path": {
                "b64": encode_path(parts),
                "components_b64": [c for c in fact["path"]
                                   ["components_b64"]],
                "display": path_display(parts),
            },
            "mode": fact["mode"],
            "kind": fact["kind"],
            "destination": str(dest),
        }
        _write_text_fsync(manifest_path, manifest)
        return manifest


def _write_text_fsync(path: Path, payload: dict) -> None:
    """Write the export manifest durably; never via a partial file name."""
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                    suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _git_blob_id(content: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _tree_chain(view: GitView, commit_id: str,
                parts: tuple[bytes, ...]) -> list[dict]:
    node = view.require_commit(commit_id)
    chain = [{"level": -1, "kind": "commit", "object_id": commit_id,
              "root_tree": node.tree}]
    tree_id = node.tree
    for depth, part in enumerate(parts):
        entries = view.tree_entries(tree_id)
        entry = next((e for e in entries if e.name == part), None)
        if entry is None:
            raise ExportError("path disappeared during export")
        chain.append({
            "level": depth,
            "kind": entry.kind,
            "object_id": entry.object_id,
            "mode": entry.mode,
            "name_b64": entry.name_b64,
            "tree": tree_id,
        })
        if entry.is_tree:
            tree_id = entry.object_id
    return chain
