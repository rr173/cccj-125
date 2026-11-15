"""Parser for the supported copybook subset.

Supported grammar (see README.md for the full specification)::

    <stmt> := <level> <name> [REDEFINES <name>] [PIC <picture>]
              [OCCURS <n> TIMES] [COMP-3 | DISPLAY] [SIGN TRAILING] "."

* levels 01-49 (66/77/88 are rejected)
* PIC X(n) / A(n) / 9(n) / S9(n) [V 9(m)], repeat counts optional
* COMP-3 (packed decimal) and DISPLAY (zoned) usage
* fixed OCCURS (OCCURS ... DEPENDING ON is rejected)
* REDEFINES of a previously declared sibling
* full-line comments (``*`` first) and inline ``*>`` comments
"""

from __future__ import annotations

import re
from typing import Optional

from .errors import CopybookSyntaxError
from .model import Field, Layout, Pic

_NAME_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?$")

_UNSUPPORTED_CLAUSES = {"VALUE", "JUST", "JUSTIFIED", "BLANK", "SYNC",
                        "SYNCHRONIZED", "RENAMES", "SIGN-SEPARATE"}


def parse_copybook(text: str, name: str = "RECORD", version: str = "1",
                   codec: str = "cp037") -> Layout:
    """Parse *text* into a :class:`Layout` with resolved byte offsets."""
    statements = _split_statements(text)
    if not statements:
        raise CopybookSyntaxError("copybook contains no statements")
    fields = [_parse_statement(stmt, idx) for idx, stmt in enumerate(statements)]
    root = _build_tree(fields)
    _resolve_redefines(root)
    _assign_offsets(root, 0)
    if root.byte_length <= 0:
        raise CopybookSyntaxError("record has zero length")
    return Layout(name=name, version=version, root=root,
                  record_length=root.byte_length, codec=codec)


# ---------------------------------------------------------------- statements

def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("*"):          # full-line comment
            continue
        idx = line.find("*>")                       # inline comment
        if idx != -1:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _split_statements(text: str) -> list[str]:
    """Split into statements terminated by '.', spanning line breaks."""
    text = _strip_comments(text)
    return [s.strip() for s in text.split(".") if s.strip()]


def _parse_statement(stmt: str, index: int) -> Field:
    where = f"statement {index + 1} ({stmt!r})"
    tokens = stmt.split()
    if len(tokens) < 2:
        raise CopybookSyntaxError(f"{where}: expected a level number and a name")
    try:
        level = int(tokens[0])
    except ValueError:
        raise CopybookSyntaxError(f"{where}: bad level number {tokens[0]!r}")
    if level in (66, 77, 88):
        raise CopybookSyntaxError(
            f"{where}: level {level} is not supported (subset allows 01-49)")
    if not 1 <= level <= 49:
        raise CopybookSyntaxError(f"{where}: level {level} out of range 01-49")

    name = tokens[1].upper()
    filler = name == "FILLER"
    if not filler and not _NAME_RE.match(name):
        raise CopybookSyntaxError(f"{where}: invalid field name {tokens[1]!r}")

    pic: Optional[Pic] = None
    usage = "DISPLAY"
    occurs = 1
    redefines: Optional[str] = None

    i = 2
    while i < len(tokens):
        tok = tokens[i].upper()
        if tok == "REDEFINES":
            redefines = _need(tokens, i + 1, where, "REDEFINES target").upper()
            i += 2
        elif tok in ("PIC", "PICTURE"):
            j = i + 1
            if j < len(tokens) and tokens[j].upper() == "IS":
                j += 1
            pic = _parse_pic(_need(tokens, j, where, "PICTURE string"), where)
            i = j + 1
        elif tok == "OCCURS":
            raw = _need(tokens, i + 1, where, "OCCURS count")
            if not raw.isdigit() or int(raw) < 1:
                raise CopybookSyntaxError(f"{where}: bad OCCURS count {raw!r}")
            occurs = int(raw)
            i += 2
            if i < len(tokens) and tokens[i].upper() == "TIMES":
                i += 1
            if i < len(tokens) and tokens[i].upper() == "DEPENDING":
                raise CopybookSyntaxError(
                    f"{where}: OCCURS DEPENDING ON is not supported")
        elif tok in ("COMP-3", "PACKED-DECIMAL"):
            usage = "COMP-3"
            i += 1
        elif tok in ("COMP", "COMP-4", "COMP-5", "BINARY"):
            raise CopybookSyntaxError(
                f"{where}: binary usage {tok} is not supported "
                "(subset supports DISPLAY and COMP-3)")
        elif tok == "DISPLAY":
            i += 1
        elif tok == "USAGE":
            j = i + 1
            if j < len(tokens) and tokens[j].upper() == "IS":
                j += 1
            val = _need(tokens, j, where, "USAGE value").upper()
            if val in ("COMP-3", "PACKED-DECIMAL"):
                usage = "COMP-3"
            elif val != "DISPLAY":
                raise CopybookSyntaxError(f"{where}: unsupported USAGE {val}")
            i = j + 1
        elif tok == "SIGN":
            j = i + 1
            if j < len(tokens) and tokens[j].upper() == "IS":
                j += 1
            val = _need(tokens, j, where, "SIGN value").upper()
            if val != "TRAILING":
                raise CopybookSyntaxError(
                    f"{where}: only SIGN TRAILING (the default) is supported")
            i = j + 1
            if i < len(tokens) and tokens[i].upper() == "SEPARATE":
                raise CopybookSyntaxError(
                    f"{where}: SIGN SEPARATE is not supported")
        elif tok in _UNSUPPORTED_CLAUSES:
            raise CopybookSyntaxError(f"{where}: clause {tok} is not supported")
        else:
            raise CopybookSyntaxError(f"{where}: unrecognized token {tokens[i]!r}")

    if occurs > 1 and level == 1:
        raise CopybookSyntaxError(f"{where}: OCCURS is not allowed on level 01")
    if pic is not None and usage == "COMP-3" and pic.category != "9":
        raise CopybookSyntaxError(f"{where}: COMP-3 requires a numeric PICTURE")

    return Field(name=name, level=level, pic=pic, usage=usage, occurs=occurs,
                 redefines=redefines, filler=filler)


def _need(tokens: list[str], i: int, where: str, what: str) -> str:
    if i >= len(tokens):
        raise CopybookSyntaxError(f"{where}: expected {what}")
    return tokens[i]


def _parse_pic(text: str, where: str) -> Pic:
    s = text.upper()
    signed = False
    if s.startswith("S"):
        signed = True
        s = s[1:]
    if not s:
        raise CopybookSyntaxError(f"{where}: empty PICTURE string")

    int_cats: list[str] = []
    frac_cats: list[str] = []
    target = int_cats
    seen_v = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "V":
            if seen_v:
                raise CopybookSyntaxError(
                    f"{where}: multiple V in PICTURE {text!r}")
            seen_v = True
            target = frac_cats
            i += 1
            continue
        if c not in "X9A":
            raise CopybookSyntaxError(
                f"{where}: unsupported PICTURE character {c!r} in {text!r} "
                "(subset supports X, A, 9, S, V)")
        count = 1
        if i + 1 < len(s) and s[i + 1] == "(":
            j = s.find(")", i + 2)
            if j == -1:
                raise CopybookSyntaxError(
                    f"{where}: unbalanced '(' in PICTURE {text!r}")
            num = s[i + 2:j]
            if not num.isdigit() or int(num) == 0:
                raise CopybookSyntaxError(
                    f"{where}: bad repeat count in PICTURE {text!r}")
            count = int(num)
            i = j + 1
        else:
            i += 1
        target.extend([c] * count)

    cats = int_cats + frac_cats
    if not cats:
        raise CopybookSyntaxError(f"{where}: PICTURE {text!r} has no positions")
    if any(c == "9" for c in cats):
        if any(c != "9" for c in cats):
            raise CopybookSyntaxError(
                f"{where}: cannot mix 9 with X/A in PICTURE {text!r}")
        return Pic(category="9", display_length=len(cats),
                   signed=signed, scale=len(frac_cats))
    if signed or frac_cats:
        raise CopybookSyntaxError(
            f"{where}: S/V not allowed with alphanumeric PICTURE {text!r}")
    return Pic(category="X", display_length=len(cats))


# ---------------------------------------------------------------------- tree

def _build_tree(fields: list[Field]) -> Field:
    root: Optional[Field] = None
    stack: list[Field] = []
    filler_seq = 0
    for f in fields:
        if f.filler:
            filler_seq += 1
            f.name = f"FILLER-{filler_seq}"
        if f.level == 1:
            if root is not None:
                raise CopybookSyntaxError(
                    "multiple 01 levels: exactly one record layout is supported")
            if f.redefines:
                raise CopybookSyntaxError(
                    "REDEFINES on level 01 is not supported")
            root = f
            stack = [f]
            continue
        if root is None:
            raise CopybookSyntaxError("first statement must be level 01")
        while stack and stack[-1].level >= f.level:
            stack.pop()
        if not stack:
            raise CopybookSyntaxError(f"{f.name}: no parent group for level "
                                      f"{f.level:02d}")
        parent = stack[-1]
        if parent.pic is not None:
            raise CopybookSyntaxError(
                f"{f.name}: {parent.name} is elementary and cannot have "
                "subordinate items")
        parent.children.append(f)
        f.parent = parent
        stack.append(f)
    if root is None:
        raise CopybookSyntaxError("no level 01 record found")
    for f in root.iter_fields():
        if f.pic is None and not f.children:
            raise CopybookSyntaxError(f"{f.name}: group item has no children")
    return root


def _resolve_redefines(root: Field) -> None:
    for f in root.iter_fields():
        if not f.redefines:
            continue
        assert f.parent is not None  # level 01 REDEFINES rejected earlier
        target = None
        for sibling in f.parent.children:
            if sibling is f:
                break
            if sibling.name == f.redefines:
                target = sibling
        if target is None:
            raise CopybookSyntaxError(
                f"{f.name}: REDEFINES target {f.redefines} not found among "
                "preceding siblings")
        f.redefines_target = target


def _assign_offsets(f: Field, start: int) -> None:
    f.offset = start
    if f.pic is not None:
        f.byte_length = _elementary_length(f)
        return
    cursor = start
    for child in f.children:
        child_start = (child.redefines_target.offset
                       if child.redefines_target is not None else cursor)
        _assign_offsets(child, child_start)
        cursor = max(cursor, child.offset + child.total_length)
    f.byte_length = cursor - start


def _elementary_length(f: Field) -> int:
    assert f.pic is not None
    if f.usage == "COMP-3":
        # two digits per byte plus one nibble for the sign
        return f.pic.display_length // 2 + 1
    return f.pic.display_length
