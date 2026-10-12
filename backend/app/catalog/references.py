"""Find the tables a piece of PL/SQL or PL/pgSQL code uses, and how it uses them.

Not a parser. Comments are blanked (app.scan.plsql.mask) and the remaining tokens are
scanned for the places a table name can follow: FROM / JOIN lists, INSERT INTO, UPDATE,
DELETE, MERGE, TRUNCATE, DDL, `%ROWTYPE` and `SETOF`. String literals are scanned
separately, so table names inside dynamic SQL are found too (and labelled as such).
A name only counts when it resolves to a table or view in the catalog.
"""

import bisect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.scan.plsql import mask

TableKey = tuple[str, str]  # (schema, name)

_TOKEN = re.compile(r'"(?:[^"]|"")+"|[A-Za-z_][\w$#]*|%\s*ROWTYPE\b|\S', re.I)
_SUBPROGRAM = re.compile(r"^([ \t]*)(?:FUNCTION|PROCEDURE)\s+(\"[^\"]+\"|[\w$#]+)", re.I | re.M)

# Words that end a FROM list or can't be a table alias.
_CLAUSE_WORDS = {
    "SELECT", "WHERE", "GROUP", "ORDER", "HAVING", "UNION", "INTERSECT", "EXCEPT", "MINUS", "CONNECT",
    "START", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL", "ON", "USING",
    "LATERAL", "WINDOW", "LIMIT", "OFFSET", "FETCH", "FOR", "RETURNING", "RETURN", "SET", "VALUES",
    "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "INTO", "BULK", "COLLECT", "LOG", "ERRORS",
    "DEFAULT", "LOOP", "PARTITION", "SAMPLE", "MODEL", "PIVOT", "UNPIVOT", "WITH", "IS", "AS", "ONLY",
    "TABLE", "BEGIN", "DECLARE", "EXCEPTION", "IF", "ELSIF", "CASE", "OF", "DO", "NOTHING", "QUALIFY",
    "TABLESAMPLE", "FROM", "IN", "EXECUTE", "OPEN", "CLOSE", "OVERRIDING",
}
# FROM inside these calls is not a table list: EXTRACT(YEAR FROM d), TRIM(x FROM y)...
_FROM_FUNCTIONS = {"EXTRACT", "SUBSTRING", "TRIM", "OVERLAY", "POSITION", "SUBSTR"}
_DDL = {"CREATE", "ALTER", "DROP"}


@dataclass
class Reference:
    table: TableKey
    operation: str  # select | insert | update | delete | merge | truncate | ddl | lock | type
    offset: int
    dynamic: bool  # found inside a string literal (dynamic SQL)


@dataclass
class Usage:
    table: TableKey
    operations: set[str] = field(default_factory=set)
    found_by: set[str] = field(default_factory=set)  # code | dynamic_sql
    member_operations: dict[str, set[str]] = field(default_factory=dict)  # package member -> operations
    lines: list[int] = field(default_factory=list)
    subprograms: set[str] = field(default_factory=set)


@dataclass
class _Tok:
    text: str
    upper: str
    start: int
    quoted: bool = False

    @property
    def is_word(self) -> bool:
        return self.quoted or self.upper[:1].isalpha() or self.upper[:1] == "_"


def _tokens(text: str) -> list[_Tok]:
    out = []
    for m in _TOKEN.finditer(text):
        s = m.group(0)
        if s.startswith('"'):
            out.append(_Tok(s[1:-1].replace('""', '"'), s[1:-1].replace('""', '"'), m.start(), quoted=True))
        elif s.startswith("%"):
            out.append(_Tok("%ROWTYPE", "%ROWTYPE", m.start()))
        else:
            out.append(_Tok(s, s.upper(), m.start()))
    return out


Resolve = Callable[[list[_Tok]], TableKey | None]


def _scan(text: str, resolve: Resolve, *, dynamic: bool) -> list[Reference]:
    toks = _tokens(text)
    refs: list[Reference] = []
    n = len(toks)

    def name_at(i: int) -> tuple[list[_Tok], int]:
        """Dotted name starting at token i, and the index after it."""
        parts = []
        while i < n and toks[i].is_word:
            parts.append(toks[i])
            if i + 2 < n and toks[i + 1].text == "." and toks[i + 2].is_word:
                i += 2
                continue
            i += 1
            break
        return parts, i

    def take(i: int, operation: str) -> int:
        """Resolve the table name at token i; returns the index after it."""
        while i < n and not toks[i].quoted and toks[i].upper in ("ONLY", "LATERAL"):
            i += 1
        if i >= n or not toks[i].is_word or (not toks[i].quoted and toks[i].upper in _CLAUSE_WORDS):
            return i
        parts, j = name_at(i)
        if operation == "select" and j < n and toks[j].text == "(":
            return j  # a function call: FROM generate_series(...), TABLE(...)
        key = resolve(parts)
        if key is not None:
            refs.append(Reference(key, operation, parts[0].start, dynamic))
        return j

    paren_owner: list[str] = []  # the word before each open parenthesis
    prev_kw: list[str] = []  # recent unquoted words, most recent last

    i = 0
    while i < n:
        t = toks[i]
        if t.text == "(":
            paren_owner.append(toks[i - 1].upper if i and toks[i - 1].is_word else "")
            i += 1
            continue
        if t.text == ")":
            if paren_owner:
                paren_owner.pop()
            i += 1
            continue
        if t.text == "%ROWTYPE" and i and toks[i - 1].is_word:
            # name%ROWTYPE: walk back over a dotted name
            j = i - 1
            while j >= 2 and toks[j - 1].text == "." and toks[j - 2].is_word:
                j -= 2
            key = resolve(toks[j:i:2])
            if key is not None:
                refs.append(Reference(key, "type", toks[j].start, dynamic))
            i += 1
            continue
        if t.quoted or not t.is_word:
            i += 1
            continue

        word = t.upper
        before = prev_kw[-1] if prev_kw else ""
        prev_kw = (prev_kw + [word])[-4:]

        if word in ("FROM", "JOIN", "USING"):
            if word == "FROM" and (before == "DISTINCT" or (paren_owner and paren_owner[-1] in _FROM_FUNCTIONS)):
                i += 1
                continue
            operation = "delete" if word == "FROM" and before == "DELETE" else "select"
            j = take(i + 1, operation)
            # FROM a x, b y: further tables separated by commas (aliases optional).
            while word == "FROM" and operation == "select" and j < n:
                if j < n and toks[j].is_word and (toks[j].quoted or toks[j].upper not in _CLAUSE_WORDS):
                    j += 1  # alias
                elif j < n and not toks[j].quoted and toks[j].upper == "AS" and j + 1 < n:
                    j += 2
                if j < n and toks[j].text == ",":
                    j = take(j + 1, "select")
                    continue
                break
            i = max(j, i + 1)
            continue
        if word == "INTO" and before in ("INSERT", "MERGE"):
            i = max(take(i + 1, "insert" if before == "INSERT" else "merge"), i + 1)
            continue
        if word == "UPDATE" and before not in ("FOR", "DO", "ON", "KEY", "NO", "OR", "BEFORE", "AFTER", "OF"):
            i = max(take(i + 1, "update"), i + 1)
            continue
        if word == "DELETE" and i + 1 < n and toks[i + 1].upper != "FROM" and before not in ("ON", "OR", "BEFORE", "AFTER", "FOR"):
            i = max(take(i + 1, "delete"), i + 1)  # Oracle allows DELETE t WHERE ...
            continue
        if word == "TRUNCATE":
            j = i + 1
            if j < n and toks[j].upper == "TABLE":
                j += 1
            i = max(take(j, "truncate"), i + 1)
            continue
        if word == "TABLE" and (set(prev_kw[:-1]) & _DDL or before == "LOCK"):
            j = i + 1
            # CREATE TABLE IF NOT EXISTS t / DROP TABLE IF EXISTS t
            while j < n and toks[j].upper in ("IF", "NOT", "EXISTS"):
                j += 1
            i = max(take(j, "lock" if before == "LOCK" else "ddl"), i + 1)
            continue
        if word == "SETOF":
            i = max(take(i + 1, "type"), i + 1)
            continue
        i += 1
    return refs


def _string_contents(code: str, with_strings: str) -> str:
    """The text of string literals only, everything else blanked (offsets preserved)."""
    return "".join(
        w if c != w else ("\n" if w == "\n" else " ")
        for c, w in zip(code, with_strings, strict=True)
    )


def _subprogram_spans(code: str) -> list[tuple[int, str]]:
    """(offset, name) of each top-level FUNCTION/PROCEDURE in a package body."""
    starts = [(len(m.group(1).expandtabs()), m.start(), m.group(2).strip('"')) for m in _SUBPROGRAM.finditer(code)]
    if not starts:
        return []
    top = min(s[0] for s in starts)
    return [(offset, name) for indent, offset, name in starts if indent == top]


def find_usages(source: str, resolve: Resolve, *, package_body_from: int | None = None) -> dict[TableKey, Usage]:
    """Tables used by `source`, keyed by table. `package_body_from` is the offset where an
    Oracle package body starts in `source`; hits after it are attributed to subprograms."""
    masked = mask(source)
    refs = _scan(masked.code, resolve, dynamic=False) + _scan(
        _string_contents(masked.code, masked.with_strings), resolve, dynamic=True
    )
    spans = []
    if package_body_from is not None:
        spans = [(package_body_from + o, name) for o, name in _subprogram_spans(masked.code[package_body_from:])]
    span_offsets = [o for o, _ in spans]

    usages: dict[TableKey, Usage] = {}
    for ref in sorted(refs, key=lambda r: r.offset):
        usage = usages.setdefault(ref.table, Usage(ref.table))
        usage.operations.add(ref.operation)
        usage.found_by.add("dynamic_sql" if ref.dynamic else "code")
        line = masked.line_of(ref.offset)
        if len(usage.lines) < 50 and line not in usage.lines:
            usage.lines.append(line)
        if spans and ref.offset >= spans[0][0]:
            member = spans[bisect.bisect_right(span_offsets, ref.offset) - 1][1]
            usage.subprograms.add(member)
            usage.member_operations.setdefault(member, set()).add(ref.operation)
    return usages


class Resolver:
    """Map a (possibly qualified) name in code to a catalog table.

    Unqualified names are looked up in the code's own schema, then along `search_path`,
    then through synonyms, and finally accepted if exactly one schema has that name.
    """

    def __init__(
        self,
        tables: Iterable[TableKey],
        *,
        fold: Callable[[str], str],
        search_path: Iterable[str] = (),
        synonyms: dict[TableKey, TableKey] | None = None,
    ):
        self.tables = set(tables)
        self.fold = fold
        self.search_path = list(search_path)
        self.synonyms = synonyms or {}
        self.by_name: dict[str, list[TableKey]] = {}
        for key in self.tables:
            self.by_name.setdefault(key[1], []).append(key)

    def _name(self, tok: _Tok) -> str:
        return tok.text if tok.quoted else self.fold(tok.text)

    def through_synonym(self, key: TableKey) -> TableKey | None:
        """The table or view a synonym leads to, following synonyms of synonyms. None when
        it leads elsewhere: synonyms also name sequences, packages, other databases..."""
        seen = set()
        while key in self.synonyms and key not in seen:
            seen.add(key)
            key = self.synonyms[key]
            if key in self.tables:
                return key
        return None

    def bind(self, schema: str, extra_path: Iterable[str] = ()) -> Resolve:
        path = [schema, *extra_path, *self.search_path]

        def resolve(parts: list[_Tok]) -> TableKey | None:
            if not parts:
                return None
            names = [self._name(p) for p in parts]
            if len(names) >= 2:
                key = (names[-2], names[-1])
                return key if key in self.tables else self.through_synonym(key)
            name = names[0]
            for s in path:
                if (s, name) in self.tables:
                    return (s, name)
            for owner in (schema, "PUBLIC"):
                target = self.through_synonym((owner, name))
                if target is not None:
                    return target
            candidates = self.by_name.get(name, [])
            return candidates[0] if len(candidates) == 1 else None

        return resolve
