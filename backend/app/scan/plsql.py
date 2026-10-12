"""Lightweight PL/SQL text utilities.

Not a parser: just enough lexing to blank out comments and string literals so rule
patterns don't fire on `-- COMMIT later` or `'(+)'`, while keeping every character
offset (and so every line number) identical to the original source.
"""

import bisect
import re
from dataclasses import dataclass, field

_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$#]")


@dataclass
class MaskedSource:
    original: str
    code: str  # comments and string contents blanked
    with_strings: str  # comments blanked, string literals intact
    empty_string_offsets: list[int] = field(default_factory=list)
    _line_starts: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", self.original)]

    def line_of(self, offset: int) -> int:
        """1-based line number of a character offset."""
        return bisect.bisect_right(self._line_starts, offset)


def _blank(text: str) -> str:
    """Replace everything except newlines with spaces (keeps offsets and lines)."""
    return re.sub(r"[^\n]", " ", text)


def mask(source: str) -> MaskedSource:
    code: list[str] = []
    with_strings: list[str] = []
    empty: list[int] = []
    i, n = 0, len(source)

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":  # line comment
            end = source.find("\n", i)
            end = n if end == -1 else end
            blank = _blank(source[i:end])
            code.append(blank)
            with_strings.append(blank)
            i = end
            continue

        if ch == "/" and nxt == "*":  # block comment
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            blank = _blank(source[i:end])
            code.append(blank)
            with_strings.append(blank)
            i = end
            continue

        if ch == '"':  # quoted identifier: copy through untouched
            end = source.find('"', i + 1)
            end = n if end == -1 else end + 1
            code.append(source[i:end])
            with_strings.append(source[i:end])
            i = end
            continue

        # Alternative quoting: q'[...]', Q'{...}', nq'<...>' etc.
        if ch in "qQ" and nxt == "'" and i + 2 < n and not (i and _IDENT_CHAR.match(source[i - 1]) and source[i - 1] not in "nN"):
            opener = source[i + 2]
            closer = _Q_CLOSERS.get(opener, opener)
            end = source.find(closer + "'", i + 3)
            end = n if end == -1 else end + 2
            literal = source[i:end]
            code.append(literal[:3] + _blank(literal[3:-2]) + literal[-2:])
            with_strings.append(literal)
            i = end
            continue

        if ch == "'":  # ordinary literal; '' inside is an escaped quote
            j = i + 1
            while j < n:
                if source[j] == "'":
                    if j + 1 < n and source[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            literal = source[i:end]
            if literal == "''":
                empty.append(i)
            code.append("'" + _blank(literal[1:-1]) + "'" if len(literal) >= 2 else literal)
            with_strings.append(literal)
            i = end
            continue

        code.append(ch)
        with_strings.append(ch)
        i += 1

    return MaskedSource(source, "".join(code), "".join(with_strings), empty)


_WRAPPED = re.compile(r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?)?[A-Z ]+?\s+[\w$#.\"]+\s+wrapped\s*$", re.I | re.M)


def is_wrapped(source: str) -> bool:
    """True for obfuscated (DBMS_DDL.WRAP / wrap utility) source."""
    first_lines = "\n".join(source.splitlines()[:3])
    return bool(_WRAPPED.search(first_lines))


@dataclass
class PackageDeclaration:
    name: str
    kind: str  # "variable" | "constant"
    line: int


_HEADER = re.compile(r"\bPACKAGE\s+(?:BODY\s+)?[\w$#.\"]+\s*(?:AUTHID\s+\w+\s*)?(?:ACCESSIBLE\s+BY\s*\([^)]*\)\s*)?\b(IS|AS)\b", re.I)
_NOT_STATE = re.compile(r"^(TYPE|SUBTYPE|CURSOR|PRAGMA|FUNCTION|PROCEDURE|END|BEGIN)\b", re.I)
_SEMICOLON = re.compile(";")
_DECL = re.compile(r"^([A-Za-z][\w$#]*|\"[^\"]+\")\s+(CONSTANT\s+)?(\S+)", re.I)


def package_level_declarations(masked: MaskedSource, object_type: str) -> list[PackageDeclaration]:
    """Variables and constants declared at package level (in the spec, or at the top
    of the body before the first subprogram). These are per-session state in Oracle
    and have no direct PostgreSQL equivalent."""
    if object_type not in ("PACKAGE", "PACKAGE BODY"):
        return []
    code = masked.code
    header = _HEADER.search(code)
    if not header:
        return []

    found: list[PackageDeclaration] = []
    stmt_start = header.end()
    for semicolon in _SEMICOLON.finditer(code, header.end()):
        statement = code[stmt_start:semicolon.start()]
        statement_offset = stmt_start
        stmt_start = semicolon.end()
        stripped = statement.strip()
        if not stripped:
            continue
        if _NOT_STATE.match(stripped):
            # PL/SQL requires item declarations before subprogram bodies, so in a
            # body the first FUNCTION/PROCEDURE/BEGIN ends the global section.
            if object_type == "PACKAGE BODY" and re.match(r"(FUNCTION|PROCEDURE|BEGIN)\b", stripped, re.I):
                break
            if re.match(r"END\b", stripped, re.I):
                break
            continue
        decl = _DECL.match(stripped)
        if not decl or decl.group(3).upper().startswith("EXCEPTION"):
            continue
        offset = statement_offset + (len(statement) - len(statement.lstrip()))
        found.append(
            PackageDeclaration(
                name=decl.group(1),
                kind="constant" if decl.group(2) else "variable",
                line=masked.line_of(offset),
            )
        )
    return found
