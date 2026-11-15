"""Relational admission engine: fixed-point elimination with proven proofs.

The engine works entirely against the on-disk SQLite staging database.
Its candidate-support representation is deliberately group-oriented rather
than graph-oriented:

* ``key_values`` holds every *readable* candidate key tuple of every
  locally valid row;
* ``ref_edges`` holds one row per reference that really has to match, with
  a ``live`` flag that says whether at least one still-kept parent carries
  the exact target tuple;
* ``edge_groups`` collapses edges with the same
  ``(child table, reference, target, target key, tuple)`` into one group,
  so a huge same-key candidate group and a diamond-shaped fan-in share one
  proof node instead of being expanded per dependent row.

Layer computation follows the same synchronous rounds the specification
defines, implemented as a *semi-naive* fixpoint: round one only touches
groups with zero initial candidates; every later round only re-tests groups
whose candidate set shrank in the previous round.  Rounds therefore do
work proportional to edges that actually change -- a closed cycle never
re-enters the loop -- and the Python layer only ever holds bounded batches
of group identifiers, never the dependency graph.

Proofs are written group by group (one node per unsupported group, with
the complete initial candidate row ids and their strictly-lower-layer
proof nodes), and kept-row witnesses are picked per matched reference with
the smallest parent row id in UTF-8 byte order.  Cycles among witnesses
are legal and never flip a kept verdict.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, Union

from .config import RelationContract
from .errors import RelationInputError
from .loading import load_inputs

#: Record dispositions.
KEPT = "kept"
LOCAL_REJECT = "local_reject"
CASCADE_REJECT = "cascade_reject"

#: Exit-code contract used by the CLI.
EXIT_OK = 0
EXIT_BUSINESS_REJECT = 1
EXIT_CANNOT_PROCESS = 2

COMPLETE_MARKER = "COMPLETE.json"
REPORT_JSON = "admission_report.json"
STAGING_DB = "admission.sqlite3"
TABLES_DIR = "tables"

#: Maximum group ids handled per Python-side proof/candidate batch; the
#: full same-key candidate list itself always stays inside SQL.
PROOF_BATCH = 1000


def run_admission(*, contract: RelationContract,
                  output_dir: Union[str, Path],
                  temp_dir: Union[str, Path]) -> dict:
    """Run one offline admission job and materialize the delivery.

    The contract names every input file, so inputs are not passed
    separately.  The output directory is created only after the complete
    fixed point and every proof has been computed, and it must not already
    exist.  A write failure removes the half-written directory, so a
    delivery without :data:`COMPLETE_MARKER` can never be mistaken for a
    finished job.
    """
    output_dir = Path(output_dir)
    temp_base = Path(temp_dir)
    temp_base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="relation-staging-",
                                    dir=temp_base))
    db_path = staging / STAGING_DB
    delivery_created = False
    try:
        db = sqlite3.connect(db_path)
        try:
            _prepare_ordinals(db, contract)
            total_identities = load_inputs(db, contract)
            round_count = _eliminate(db)
            _build_witnesses(db)
            stats = _collect_stats(db, contract)
            staging_bytes = _staging_bytes(staging)

            # Reserve the delivery path only after the verdict is final.
            try:
                output_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise RelationInputError(
                    f"output directory {output_dir} already exists; "
                    "refusing to overwrite an existing admission result")
            delivery_created = True
            written = _deliver(db, contract, output_dir, staging,
                               total_identities, round_count, staging_bytes)
        finally:
            db.close()
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if delivery_created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return written


def _prepare_ordinals(db: sqlite3.Connection,
                      contract: RelationContract) -> None:
    db.execute(
        "CREATE TABLE table_ordinals (ordinal INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL UNIQUE)")
    db.executemany(
        "INSERT INTO table_ordinals (ordinal, name) VALUES (?,?)",
        [(t.ordinal, t.name) for t in
         (contract.tables[name] for name in contract.table_order)])
    db.commit()


# ------------------------------------------------------------- elimination

def _eliminate(db: sqlite3.Connection) -> int:
    """Run the synchronous-round fixpoint and record each row's layer.

    The fixpoint uses semi-naive *batched* propagation.  SQL remains the
    source of truth for records, keys and edges, but the per-round support
    counters and the pending frontier live in compact Python maps built
    from streamed, index-backed SELECTs.  Those maps hold only reference
    identifiers -- row ids, encoded tuples, adjacency -- never record
    payloads, whole tables or decoded business-key spaces:

    * ``tuple_owners`` maps each readable target tuple to the locally
      valid rows that carry it (layer-0 rows are deliberately excluded,
      though their tuples stay available later as initial proof
      candidates);
    * ``edges_on_tuple`` / ``owner_tuples`` are the reference adjacency;
    * ``alive`` is the surviving-candidate counter per tuple.

    Every owner is processed exactly once across all rounds, so the total
    work is O(edges + key_values) even when the graph is one chain that
    needs one round per row; there is no SQL statement issued per tuple per
    round.  Each sweep is still synchronous, so the layer assigned is the
    earliest round the spec defines.  Verdicts and per-edge dead rounds
    are written back to SQL in one bulk pass for proof/witness building.
    """
    db.execute(
        "UPDATE records SET layer = 0 WHERE status = 'local_reject'")
    db.execute(
        "INSERT INTO edge_groups (table_ord, ref_name, target_ord, "
        "target_key, key_tuple) "
        "SELECT DISTINCT e.table_ord, e.ref_name, e.target_ord, "
        "e.target_key, e.key_tuple FROM ref_edges e")
    db.commit()

    local_bad = {(t, rid) for (t, rid) in db.execute(
        "SELECT table_ord, row_id FROM records "
        "WHERE status = 'local_reject'")}

    # Surviving-candidate COUNT per readable tuple, aggregated in SQL so a
    # huge same-key candidate group is never materialized as a Python set:
    # only its cardinality crosses into memory.  Layer-0 rows are excluded.
    owner_count: dict[tuple, int] = {}
    for t, key_name, key_tuple, cnt in db.execute(
            """
            SELECT k.table_ord, k.key_name, k.key_tuple, COUNT(*)
            FROM key_values k
            JOIN records r ON r.table_ord = k.table_ord
                          AND r.row_id = k.row_id
            WHERE r.status = 'pending'
            GROUP BY k.table_ord, k.key_name, k.key_tuple
            """):
        owner_count[(t, key_name, key_tuple)] = cnt

    # Per-row carried tuples (bounded by the keys one row declares, never by
    # a candidate group's size) and the reference adjacency.  The total size
    # of these maps is O(edges + key_values), spread over all tuples rather
    # than duplicated inside one large group.
    owner_carried: dict[tuple, list[tuple]] = {}
    for t, row_id, key_name, key_tuple in db.execute(
            "SELECT table_ord, row_id, key_name, key_tuple "
            "FROM key_values"):
        owner_carried.setdefault((t, row_id), []).append(
            (t, key_name, key_tuple))

    edges_on_tuple: dict[tuple, list[tuple]] = {}
    owner_tuples: dict[tuple, set[tuple]] = {}
    for t, row_id, _ref, tt, tkey, ktuple in db.execute(
            "SELECT table_ord, row_id, ref_name, target_ord, target_key, "
            "key_tuple FROM ref_edges"):
        tup = (tt, tkey, ktuple)
        owner = (t, row_id)
        edges_on_tuple.setdefault(tup, []).append(owner)
        owner_tuples.setdefault(owner, set()).add(tup)

    alive = {tup: owner_count.get(tup, 0) for tup in edges_on_tuple}

    status = {ident: ("local" if ident in local_bad else "alive")
              for ident in owner_tuples}
    layer = {ident: 0 for ident in local_bad}
    edge_dead_round: dict[tuple, int] = {}

    # Round 1: every tuple with no surviving candidate fires its edges.
    frontier: set[tuple] = set()
    round_no = 0
    fired: set[tuple] = set()
    for tup, count in alive.items():
        if count == 0:
            fired.add(tup)
            edge_dead_round[tup] = 1
            for owner in edges_on_tuple[tup]:
                if status.get(owner) == "alive":
                    status[owner] = "dead"
                    layer[owner] = 1
                    frontier.add(owner)
    if fired:
        round_no = 1

    while frontier:
        round_no += 1
        changed: dict[tuple, int] = {}
        for owner in frontier:
            for tup in owner_carried.get(owner, ()):
                if tup in alive:
                    changed[tup] = changed.get(tup, 0) + 1
        next_frontier: set[tuple] = set()
        for tup, gone in changed.items():
            alive[tup] = max(0, alive[tup] - gone)
            if alive[tup] == 0 and tup not in fired:
                fired.add(tup)
                edge_dead_round[tup] = round_no
                for owner in edges_on_tuple[tup]:
                    if status.get(owner) == "alive":
                        status[owner] = "dead"
                        layer[owner] = round_no
                        next_frontier.add(owner)
        frontier = next_frontier

    dead_rows = [(CASCADE_REJECT, layer[ident], ident[0], ident[1])
                 for ident, st in status.items() if st == "dead"]
    db.executemany(
        "UPDATE records SET status = ?, layer = ? "
        "WHERE table_ord = ? AND row_id = ?", dead_rows)
    # Every locally valid row that was not rejected survives the greatest
    # fixed point -- including rows with no real reference at all, which
    # never appear in the adjacency maps.
    db.execute(
        "UPDATE records SET status = ? WHERE status = 'pending'", (KEPT,))
    db.executemany(
        "UPDATE ref_edges SET live = 0, dead_round = ? "
        "WHERE target_ord = ? AND target_key = ? AND key_tuple = ?",
        [(rnd,) + tup for tup, rnd in edge_dead_round.items()])
    db.commit()
    _build_proofs(db)
    return max(round_no - 1, 0)


def _build_proofs(db: sqlite3.Connection) -> None:
    """Materialize one shared proof node per rejected edge group.

    A group's layer is taken directly from the round in which its edges
    died (all edges of one target tuple always flip together). Its
    candidates are the *complete* initial set of rows whose readable key
    matched the tuple; when the edge died every one of them was already
    rejected, so each candidate layer is strictly smaller, which the
    closing invariant check enforces.
    """
    # Only groups that actually killed an edge need a proof node.
    groups = db.execute(
        """
        SELECT g.group_id, MIN(e.dead_round)
        FROM edge_groups g
        JOIN ref_edges e
          ON e.table_ord = g.table_ord AND e.ref_name = g.ref_name
         AND e.target_ord = g.target_ord
         AND e.target_key = g.target_key
         AND e.key_tuple = g.key_tuple
        WHERE e.live = 0
        GROUP BY g.group_id
        ORDER BY g.group_id
        """).fetchall()
    db.executemany(
        "INSERT INTO proofs (group_id, reject_layer) VALUES (?,?)", groups)
    db.commit()

    for start in range(0, len(groups), PROOF_BATCH):
        for group_id, _layer in groups[start:start + PROOF_BATCH]:
            # Stream the complete initial candidate set one row at a time
            # straight into the proof node; even a huge same-key candidate
            # group never enters a Python container. BINARY collation orders
            # the raw UTF-8 bytes exactly like the required row_id order.
            proof_id = db.execute(
                "SELECT proof_id FROM proofs WHERE group_id = ?",
                (group_id,)).fetchone()[0]
            cursor = db.execute(
                """
                SELECT k.table_ord, k.row_id, COALESCE(r.layer, 0)
                FROM key_values k
                JOIN edge_groups g ON g.group_id = ?
                JOIN records r ON r.table_ord = k.table_ord
                              AND r.row_id = k.row_id
                WHERE k.table_ord = g.target_ord
                  AND k.key_name = g.target_key
                  AND k.key_tuple = g.key_tuple
                ORDER BY k.row_id COLLATE BINARY
                """, (group_id,))
            batch: list[tuple] = []
            cand_ord = 0
            for parent_ord, parent_row_id, parent_layer in cursor:
                batch.append((proof_id, parent_ord, parent_row_id,
                              parent_layer, cand_ord))
                cand_ord += 1
                if len(batch) >= PROOF_BATCH:
                    db.executemany(
                        "INSERT INTO proof_candidates (proof_id, parent_ord, "
                        "parent_row_id, parent_layer, cand_ord) "
                        "VALUES (?,?,?,?,?)", batch)
                    batch.clear()
            if batch:
                db.executemany(
                    "INSERT INTO proof_candidates (proof_id, parent_ord, "
                    "parent_row_id, parent_layer, cand_ord) "
                    "VALUES (?,?,?,?,?)", batch)
        db.commit()

    # Primary proof per rejected row: among its failing references take the
    # smallest layer, then the smallest reference name in UTF-8 byte order.
    primary = db.execute(
        """
        SELECT e.table_ord, e.row_id, MIN(e.dead_round) AS layer
        FROM ref_edges e
        JOIN records r ON r.table_ord = e.table_ord
                      AND r.row_id = e.row_id
        WHERE r.status = ? AND e.live = 0
        GROUP BY e.table_ord, e.row_id
        """, (CASCADE_REJECT,)).fetchall()
    rows: list[tuple] = []
    for table_ord, row_id, layer in primary:
        chosen = db.execute(
            """
            SELECT p.proof_id
            FROM ref_edges e
            JOIN edge_groups g
              ON g.table_ord = e.table_ord AND g.ref_name = e.ref_name
             AND g.target_ord = e.target_ord
             AND g.target_key = e.target_key
             AND g.key_tuple = e.key_tuple
            JOIN proofs p ON p.group_id = g.group_id
            WHERE e.table_ord = ? AND e.row_id = ?
              AND e.dead_round = ?
            ORDER BY CAST(e.ref_name AS BLOB), p.proof_id
            LIMIT 1
            """, (table_ord, row_id, layer)).fetchone()
        rows.append((table_ord, row_id, chosen[0]))
    db.executemany(
        "INSERT INTO row_proofs (table_ord, row_id, proof_id) "
        "VALUES (?,?,?)", rows)
    db.commit()
    _verify_layer_invariant(db)


def _verify_layer_invariant(db: sqlite3.Connection) -> None:
    """Defensive check: every candidate under a proof has a smaller layer."""
    bad = db.execute(
        "SELECT COUNT(*) FROM proof_candidates pc JOIN proofs p "
        "ON p.proof_id = pc.proof_id WHERE pc.parent_layer >= p.reject_layer"
    ).fetchone()[0]
    if bad:  # pragma: no cover - indicates an engine bug, not bad input
        raise RelationInputError(
            "internal proof invariant violated: a parent proof layer is "
            "not strictly smaller than its dependent proof")


def _build_witnesses(db: sqlite3.Connection) -> None:
    """For every kept row, pin one surviving parent per real reference.

    Same-key multiple parents resolve to the smallest parent row id in
    UTF-8 byte order (BINARY collation on the raw TEXT bytes).  All-null
    optional skips are delivered separately as explicit "no match needed"
    markers.  Witnesses may form cycles; that never changes the verdict.
    """
    # One correlated pick per kept row's matched reference: the smallest
    # surviving parent row id in UTF-8 byte order (BINARY on the raw
    # bytes).  The correlated subquery is index-backed and returns one
    # scalar, so the huge same-key group is never materialized in Python.
    witness_rows = db.execute(
        """
        SELECT e.table_ord, e.row_id, e.ref_name,
               e.target_ord,
               (SELECT k.row_id FROM key_values k
                JOIN records pr ON pr.table_ord = k.table_ord
                              AND pr.row_id = k.row_id
                              AND pr.status = 'kept'
                WHERE k.table_ord = e.target_ord
                  AND k.key_name = e.target_key
                  AND k.key_tuple = e.key_tuple
                ORDER BY CAST(k.row_id AS BLOB) LIMIT 1) AS parent_row_id
        FROM ref_edges e
        JOIN records cr ON cr.table_ord = e.table_ord
                       AND cr.row_id = e.row_id
        WHERE cr.status = 'kept'
        """).fetchall()
    db.executemany(
        "INSERT INTO witnesses (table_ord, row_id, ref_name, parent_ord, "
        "parent_row_id) VALUES (?,?,?,?,?)",
        witness_rows)
    db.commit()


# --------------------------------------------------------------- delivery

def _staging_bytes(staging: Path) -> int:
    return sum(p.stat().st_size for p in staging.rglob("*")
               if p.is_file())


def _collect_stats(db: sqlite3.Connection,
                   contract: RelationContract) -> dict[int, dict]:
    stats: dict[int, dict] = {}
    for table in (contract.tables[name] for name in contract.table_order):
        counts = {KEPT: 0, LOCAL_REJECT: 0, CASCADE_REJECT: 0}
        for status, n in db.execute(
                "SELECT status, COUNT(*) FROM records WHERE table_ord = ? "
                "GROUP BY status", (table.ordinal,)):
            counts[status] = n
        stats[table.ordinal] = {
            "input": sum(counts.values()), **counts}
    return stats


def _deliver(db: sqlite3.Connection, contract: RelationContract,
             out_dir: Path, staging: Path, total_identities: int,
             round_count: int, staging_bytes: int) -> dict:
    tables_meta = []
    for table in (contract.tables[name] for name in contract.table_order):
        tdir = out_dir / TABLES_DIR / f"{table.ordinal:04d}_{_safe(table.name)}"
        tdir.mkdir(parents=True, exist_ok=False)
        kept_file = _write_record_file(db, tdir / "kept.jsonl", table,
                                       KEPT)
        local_file = _write_record_file(db, tdir / "local_rejected.jsonl",
                                        table, LOCAL_REJECT)
        cascade_file = _write_record_file(
            db, tdir / "cascade_rejected.jsonl", table, CASCADE_REJECT)
        tables_meta.append({
            "ordinal": table.ordinal, "name": table.name,
            "files": list(table.files),
            "row_id_path": table.row_id_path.spec(),
            "fields": {name: decl.spec()
                       for name, decl in table.fields.items()},
            "candidate_keys": {name: list(cols) for name, cols in
                               table.candidate_keys.items()},
            "references": [ref.spec() for ref in table.references],
            "deliverables": {"kept": str(kept_file.relative_to(out_dir)),
                             "local_rejected":
                                 str(local_file.relative_to(out_dir)),
                             "cascade_rejected":
                                 str(cascade_file.relative_to(out_dir))}})

    # Copy the provenance database so a reopened process needs no inputs.
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(staging / STAGING_DB, out_dir / STAGING_DB)

    proof_count = db.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
    proof_edge_count = db.execute(
        "SELECT COUNT(*) FROM proof_candidates").fetchone()[0]
    witness_count = db.execute("SELECT COUNT(*) FROM witnesses").fetchone()[0]
    skip_count = db.execute("SELECT COUNT(*) FROM ref_skips").fetchone()[0]
    report = {
        "tool": "copybook-relation-admission",
        "schema_version": 1,
        "conclusion": ("business_rejected"
                       if any(s[LOCAL_REJECT] + s[CASCADE_REJECT]
                              for s in _collect_stats(db, contract).values())
                       else "admitted"),
        "elimination_rounds": round_count,
        "identity_count": total_identities,
        "proof_node_count": proof_count,
        "proof_edge_count": proof_edge_count,
        "witness_count": witness_count,
        "all_null_optional_count": skip_count,
        "staging": {"sqlite": STAGING_DB,
                    "staging_bytes_before_cleanup": staging_bytes,
                    "peak_rss_kb": _peak_rss_kb()},
        "tables": tables_meta,
        "counts": {meta["name"]: _collect_stats(db, contract)[meta["ordinal"]]
                   for meta in tables_meta},
    }
    (out_dir / REPORT_JSON).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    # The completion marker is the very last step: its presence is the
    # only authoritative signal that the delivery is usable.
    marker = {"status": "complete", "report": REPORT_JSON,
              "database": STAGING_DB}
    (out_dir / COMPLETE_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False) + "\n", encoding="utf-8")
    report["output_dir"] = str(out_dir)
    return report


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_"
                   for ch in name)


def _write_record_file(db: sqlite3.Connection, path: Path,
                       table, status: str) -> Path:
    cursor = db.execute(
        "SELECT payload FROM records WHERE table_ord = ? AND status = ? "
        "ORDER BY CAST(row_id AS BLOB)", (table.ordinal, status))
    with path.open("w", encoding="utf-8") as fh:
        for (payload_text,) in cursor:
            fh.write(payload_text + "\n")
    return path


def _peak_rss_kb() -> Optional[int]:
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX platforms
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
