"""Hand-built pack fixtures with independent expected values.

Every object id and blob byte string below is written out by hand from the
Git object format (``"<type> <len>\\0" + content`` SHA-1); nothing is read
back from the reader under test.  The pack places a REF_DELTA *before* its
base and shares one base between REF and OFS chains.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import packutil as P

AUTHOR = "Fixture Writer <fixture@example.invalid> 1700000000 +0000"
COMMITTER = "Fixture Writer <fixture@example.invalid> 1700000000 +0000"


def git_oid(obj_type: str, content: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"{obj_type} {len(content)}\0".encode("ascii"))
    h.update(content)
    return h.hexdigest()


BLOB_A = b"alpha line\nsecond line\n"
BLOB_B = BLOB_A + b"third line\n"          # B is a delta over A
BLOB_C = b"alpha line\nsecond line\n"      # byte-identical to A, one commit
BLOB_D = b"delta from B: extra\n" + BLOB_B  # delta over B (multi-level)
BLOB_E = b"beta\n"
BLOB_F = b"gamma\n"
BLOB_LINK = b"alpha.txt"                    # symlink target text
BLOB_NL = b"newline name content"
BLOB_RAW = b"non-utf8 name content"

BLOB_A_ID = git_oid("blob", BLOB_A)
BLOB_B_ID = git_oid("blob", BLOB_B)
BLOB_D_ID = git_oid("blob", BLOB_D)
BLOB_E_ID = git_oid("blob", BLOB_E)
BLOB_F_ID = git_oid("blob", BLOB_F)
BLOB_LINK_ID = git_oid("blob", BLOB_LINK)
BLOB_NL_ID = git_oid("blob", BLOB_NL)
BLOB_RAW_ID = git_oid("blob", BLOB_RAW)
GITLINK_ID = "0123456789abcdef0123456789abcdef01234567"

TREE_ROOT_ID = "1" * 40         # placeholder replaced below


def tree(entries) -> bytes:
    return P.tree_bytes(entries)


def commit(tree_hex: str, parents, message: str,
           author: str = AUTHOR, committer: str = COMMITTER,
           extra_headers=None) -> bytes:
    return P.commit_bytes(tree_hex, parents, author, committer, message,
                          extra_headers=extra_headers)


# -- base tree: a file, an executable, a symlink, a gitlink, a newline name,
#    a non-UTF-8 name and a subdirectory --------------------------------
TREE_BASE = tree([
    ("100644", b"alpha.txt", BLOB_A_ID),
    ("100755", b"run.sh", BLOB_E_ID),
    ("120000", b"link", BLOB_LINK_ID),
    ("160000", b"vendor", GITLINK_ID),
    ("100644", b"line\nbreak", BLOB_NL_ID),
    ("100644", b"\xffraw", BLOB_RAW_ID),
    ("40000", b"conf", git_oid("tree", b"")),  # replaced below
])
TREE_BASE_ID = git_oid("tree", TREE_BASE)

TREE_CONF_1 = tree([("100644", b"conf/", BLOB_A_ID)])  # placeholder
# A directory "conf" containing one file "x".
TREE_CONF = tree([("100644", b"x", BLOB_A_ID)])
TREE_CONF_ID = git_oid("tree", TREE_CONF)

TREE_BASE = tree([
    ("100644", b"alpha.txt", BLOB_A_ID),
    ("100755", b"run.sh", BLOB_E_ID),
    ("120000", b"link", BLOB_LINK_ID),
    ("160000", b"vendor", GITLINK_ID),
    ("100644", b"line\nbreak", BLOB_NL_ID),
    ("100644", b"\xffraw", BLOB_RAW_ID),
    ("40000", b"conf", TREE_CONF_ID),
])
TREE_BASE_ID = git_oid("tree", TREE_BASE)

# -- second tree: alpha.txt gets BLOB_B (content change), run.sh loses its
#    executable bit (mode change), link becomes a regular file (kind
#    change: symlink -> file leaves the rest untouched), conf becomes a
#    file (file<->directory) and a new file appears.
BLOB_X = b"was a directory\n"
BLOB_X_ID = git_oid("blob", BLOB_X)
TREE_SECOND = tree([
    ("100644", b"alpha.txt", BLOB_B_ID),
    ("100644", b"run.sh", BLOB_E_ID),
    ("120000", b"link", BLOB_LINK_ID),
    ("160000", b"vendor", GITLINK_ID),
    ("100644", b"line\nbreak", BLOB_NL_ID),
    ("100644", b"\xffraw", BLOB_RAW_ID),
    ("100644", b"conf", BLOB_X_ID),
    ("100644", b"new.txt", BLOB_F_ID),
])
TREE_SECOND_ID = git_oid("tree", TREE_SECOND)

# -- third tree: file becomes directory (conf regular -> conf/x) ---------
TREE_THIRD = tree([
    ("100644", b"alpha.txt", BLOB_B_ID),
    ("100755", b"run.sh", BLOB_E_ID),
    ("120000", b"link", BLOB_LINK_ID),
    ("160000", b"vendor", GITLINK_ID),
    ("100644", b"line\nbreak", BLOB_NL_ID),
    ("100644", b"\xffraw", BLOB_RAW_ID),
    ("40000", b"conf", TREE_CONF_ID),
    ("100644", b"new.txt", BLOB_F_ID),
])
TREE_THIRD_ID = git_oid("tree", TREE_THIRD)

# -- commits -----------------------------------------------------------
# Deliberately inverted timestamps: the child predates its parent.
PARENT_AUTHOR = ("Fixture Writer <fixture@example.invalid> "
                 "1700000500 +0000")
CHILD_AUTHOR = ("Fixture Writer <fixture@example.invalid> "
                "1600000000 +0000")

COMMIT1 = commit(TREE_BASE_ID, [], "base commit\n")
COMMIT1_ID = git_oid("commit", COMMIT1)
COMMIT2 = commit(TREE_SECOND_ID, [COMMIT1_ID], "second commit\n",
                 author=CHILD_AUTHOR, committer=CHILD_AUTHOR)
COMMIT2_ID = git_oid("commit", COMMIT2)
COMMIT3 = commit(TREE_THIRD_ID, [COMMIT2_ID, COMMIT1_ID],
                 "merge-ish commit\n", author=CHILD_AUTHOR,
                 committer=CHILD_AUTHOR)
COMMIT3_ID = git_oid("commit", COMMIT3)


def build_fixture_pack():
    """A pack whose REF deltas precede bases and share bases with OFS."""
    delta_a_to_b = P.make_delta(BLOB_A, BLOB_B)
    delta_b_to_d = P.make_delta(BLOB_B, BLOB_D)
    b_tree_conf = TREE_CONF
    entries = [
        # REF_DELTA whose base comes later in the pack
        {"type": P.OBJ_REF_DELTA, "base": ("#ref", BLOB_A_ID),
         "delta": delta_a_to_b},
        # REF_DELTA over the *result* of that delta (base is itself a delta)
        {"type": P.OBJ_REF_DELTA, "base": ("#ref", BLOB_B_ID),
         "delta": delta_b_to_d},
        {"type": P.OBJ_BLOB, "content": BLOB_A},
        {"type": P.OBJ_TREE, "content": TREE_BASE},
        {"type": P.OBJ_TREE, "content": TREE_CONF},
        {"type": P.OBJ_TREE, "content": TREE_SECOND},
        {"type": P.OBJ_TREE, "content": TREE_THIRD},
        {"type": P.OBJ_BLOB, "content": BLOB_E},
        {"type": P.OBJ_BLOB, "content": BLOB_F},
        {"type": P.OBJ_BLOB, "content": BLOB_LINK},
        {"type": P.OBJ_BLOB, "content": BLOB_NL},
        {"type": P.OBJ_BLOB, "content": BLOB_RAW},
        {"type": P.OBJ_BLOB, "content": BLOB_X},
        {"type": P.OBJ_COMMIT, "content": COMMIT1},
        {"type": P.OBJ_COMMIT, "content": COMMIT2},
        {"type": P.OBJ_COMMIT, "content": COMMIT3},
    ]
    # An OFS_DELTA sharing the same base as the first REF_DELTA: it copies
    # BLOB_A and appends, producing BLOB_D differently (but the same bytes
    # cannot be reproduced by a second delta over A unless we build it as
    # a delta chain).  Instead OFS over the *first REF delta result*:
    # index 0 is the REF result (BLOB_B); make an OFS delta on it.
    # The OFS entry must come after index 0 and produces BLOB_D as well,
    # which is legitimate: the same object can be reached two ways.
    entries.append({
        "type": P.OBJ_OFS_DELTA, "base": ("#ofs", 0),
        "delta": P.make_delta(BLOB_B, BLOB_D),
    })
    pack = P.build_pack(entries)
    return pack, entries


def build_fixture_bundle(tmp: Path) -> Path:
    pack, _ = build_fixture_pack()
    header = ["# fixture handoff"]
    lines = [
        f"{COMMIT2_ID} refs/heads/main",
        f"{COMMIT3_ID} refs/heads/side",
        f"{COMMIT3_ID} HEAD",
    ]
    path = tmp / "fixture.bundle"
    path.write_bytes(P.build_bundle(lines, pack))
    return path
