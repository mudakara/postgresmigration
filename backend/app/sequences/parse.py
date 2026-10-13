"""Find which column a sequence fills, from PL/pgSQL / SQL source.

Code migrated from Oracle rarely uses column defaults: a BEFORE INSERT trigger assigns
`NEW.id := nextval('seq')`, or procedures write `INSERT INTO t (id, ...) VALUES
(nextval('seq'), ...)`. Both are recognised here. Comments are ignored; string literals
are kept because the sequence name is one.
"""

import re
from dataclasses import dataclass

from app.scan.plsql import mask

Name = list[str]  # dotted name parts, already case-folded

_NEXTVAL = r"nextval\s*\(\s*'([^']+)'(?:\s*::\s*regclass)?\s*\)"
_NEW_COL = r"NEW\s*\.\s*(\"[^\"]+\"|[A-Za-z_][\w$]*)"
_TRIGGER_ASSIGN = re.compile(
    _NEW_COL + r"\s*:?=\s*(?:COALESCE\s*\(\s*NEW\s*\.\s*(?:\"[^\"]+\"|[\w$]+)\s*,\s*)?" + _NEXTVAL, re.I
)
_TRIGGER_SELECT_INTO = re.compile(
    r"SELECT\s+" + _NEXTVAL + r"\s+INTO\s+(?:STRICT\s+)?" + _NEW_COL, re.I
)
_INSERT = re.compile(
    r"INSERT\s+INTO\s+((?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)"
    r"\s*(?:AS\s+[\w$]+\s*)?\(([^()]*)\)\s*(VALUES\s*\(|SELECT\b)",
    re.I,
)
_NEXTVAL_IN = re.compile(_NEXTVAL, re.I)
_SELECT_END = re.compile(r"\s*(?:(?:FROM|WHERE|UNION|ON\s+CONFLICT|RETURNING)\b|;)", re.I)


@dataclass(frozen=True)
class SequenceUse:
    sequence: tuple[str, ...]  # name parts as written: ("seq",) or ("schema", "seq")
    column: str
    table: tuple[str, ...] | None  # None: the trigger's own table
    via: str  # trigger | insert


def ident(part: str) -> str:
    """PostgreSQL folding: unquoted names are lower-cased, quoted ones kept."""
    part = part.strip()
    if len(part) > 1 and part[0] == part[-1] == '"':
        return part[1:-1].replace('""', '"')
    return part.lower()


def dotted(name: str) -> tuple[str, ...]:
    parts, buf, quoted = [], "", False
    for ch in name.strip():
        if ch == '"':
            quoted = not quoted
            buf += ch
        elif ch == "." and not quoted:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return tuple(ident(p) for p in parts if p.strip())


def _split_top_level(text: str, start: int, *, stop_at_close: bool) -> list[str]:
    """Comma-separated expressions from `start` until the closing parenthesis (VALUES)
    or the end of the select list (SELECT)."""
    items, depth, buf, i, n = [], 0, [], start, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and (j + 1 >= n or text[j + 1] != "'")):
                j += 2 if text[j] == "'" else 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
            i += 1
            continue
        elif (depth == 0 and not stop_at_close and (i == start or not (text[i - 1].isalnum() or text[i - 1] in "_$"))
              and _SELECT_END.match(text, i)):
            break
        buf.append(ch)
        i += 1
    items.append("".join(buf))
    return items


def find_uses(source: str) -> list[SequenceUse]:
    code = mask(source).with_strings  # comments blanked, strings kept
    uses: list[SequenceUse] = []
    for m in _TRIGGER_ASSIGN.finditer(code):
        uses.append(SequenceUse(dotted(m.group(2)), ident(m.group(1)), None, "trigger"))
    for m in _TRIGGER_SELECT_INTO.finditer(code):
        uses.append(SequenceUse(dotted(m.group(1)), ident(m.group(2)), None, "trigger"))
    for m in _INSERT.finditer(code):
        columns = [ident(c) for c in m.group(2).split(",") if c.strip()]
        values = _split_top_level(code, m.end(), stop_at_close=m.group(3).upper().startswith("VALUES"))
        for column, value in zip(columns, values, strict=False):
            seq = _NEXTVAL_IN.search(value)
            if seq:
                uses.append(SequenceUse(dotted(seq.group(1)), column, dotted(m.group(1)), "insert"))
    return list(dict.fromkeys(uses))


# --- Oracle ------------------------------------------------------------------------------------
# Triggers fill keys with :NEW.ID := SEQ.NEXTVAL (or NVL(:NEW.ID, SEQ.NEXTVAL)) or
# SELECT SEQ.NEXTVAL INTO :NEW.ID FROM DUAL; code with INSERT ... VALUES (SEQ.NEXTVAL, ...).

_ORA_NAME = r'(?:"[^"]+"|[A-Za-z][\w$#]*)'
_ORA_SEQ = rf"({_ORA_NAME}(?:\s*\.\s*{_ORA_NAME})?)\s*\.\s*NEXTVAL\b"
_ORA_NEW_COL = rf":NEW\s*\.\s*({_ORA_NAME})"
_ORA_ASSIGN = re.compile(
    _ORA_NEW_COL + rf"\s*:=\s*(?:(?:NVL|COALESCE)\s*\(\s*:NEW\s*\.\s*{_ORA_NAME}\s*,\s*)?" + _ORA_SEQ, re.I)
_ORA_SELECT_INTO = re.compile(r"SELECT\s+" + _ORA_SEQ + r"\s+INTO\s+" + _ORA_NEW_COL, re.I)
_ORA_INSERT = re.compile(
    rf"INSERT\s+INTO\s+({_ORA_NAME}(?:\s*\.\s*{_ORA_NAME})?)\s*(?:{_ORA_NAME}\s*)?\(([^()]*)\)\s*(VALUES\s*\(|SELECT\b)", re.I)
_ORA_SEQ_IN = re.compile(_ORA_SEQ, re.I)


def ora_ident(part: str) -> str:
    """Oracle folding: unquoted names are upper-cased, quoted ones kept."""
    part = part.strip()
    if len(part) > 1 and part[0] == part[-1] == '"':
        return part[1:-1]
    return part.upper()


def _ora_dotted(name: str) -> tuple[str, ...]:
    return tuple(ora_ident(p) for p in re.split(r"\s*\.\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", name.strip()) if p.strip())


def find_oracle_uses(source: str) -> list[SequenceUse]:
    """Where PL/SQL code puts a sequence's NEXTVAL into a column."""
    code = mask(source).with_strings
    uses: list[SequenceUse] = []
    for m in _ORA_ASSIGN.finditer(code):
        uses.append(SequenceUse(_ora_dotted(m.group(2)), ora_ident(m.group(1)), None, "trigger"))
    for m in _ORA_SELECT_INTO.finditer(code):
        uses.append(SequenceUse(_ora_dotted(m.group(1)), ora_ident(m.group(2)), None, "trigger"))
    for m in _ORA_INSERT.finditer(code):
        columns = [ora_ident(c) for c in m.group(2).split(",") if c.strip()]
        values = _split_top_level(code, m.end(), stop_at_close=m.group(3).upper().startswith("VALUES"))
        for column, value in zip(columns, values, strict=False):
            seq = _ORA_SEQ_IN.search(value)
            if seq:
                uses.append(SequenceUse(_ora_dotted(seq.group(1)), column, _ora_dotted(m.group(1)), "insert"))
    return list(dict.fromkeys(uses))
