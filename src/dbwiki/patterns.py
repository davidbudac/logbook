"""Pattern library: ordered rules loaded from YAML, first match wins.

Rule schema:
  name: str
  class: noise | routine | lifecycle | dataguard | warning | error | unmatched
  field: normalized-event field the regex tests (default "message")
  regex: python re, tested with .search
  where / where_not: {field: value} equality guards on normalized fields
  counter: routine counter template; "{field}" placeholders filled from the event
"""

import re
import string
from pathlib import Path

import yaml

NOTABLE_CLASSES = {"lifecycle", "dataguard", "warning", "error", "unmatched"}

_FMT = string.Formatter()


class Rule:
    def __init__(self, d: dict):
        self.name: str = d["name"]
        self.klass: str = d["class"]
        self.field: str = d.get("field", "message")
        self.regex = re.compile(d["regex"]) if d.get("regex") else None
        self.where: dict = d.get("where", {})
        self.where_not: dict = d.get("where_not", {})
        self.counter: str | None = d.get("counter")

    def matches(self, ev: dict) -> bool:
        for k, v in self.where.items():
            if ev.get(k) != v:
                return False
        for k, v in self.where_not.items():
            if ev.get(k) == v:
                return False
        if self.regex is not None:
            val = ev.get(self.field)
            if val is None or not self.regex.search(str(val)):
                return False
        return True

    def matched_line(self, ev: dict) -> str:
        """The single line of the tested field that this rule's regex hit.

        One ES document carries many alert-log lines, and the line that names
        the change is rarely the first of them: a `db_mount_open` document
        opens with "Stopping background process MMON" and only its second
        line says "alter pluggable database all close immediate". `""` when
        there is no regex, no value, or no match."""
        if self.regex is None:
            return ""
        val = ev.get(self.field)
        if val is None:
            return ""
        text = str(val)
        m = self.regex.search(text)
        if m is None:
            return ""
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.start())
        return text[start:end if end != -1 else len(text)].strip()

    def counter_key(self, ev: dict) -> str:
        if not self.counter:
            return self.name
        try:
            fields = [f for _, f, _, _ in _FMT.parse(self.counter) if f]
            return self.counter.format(**{f: ev.get(f) or "?" for f in fields})
        except (KeyError, ValueError):
            return self.counter

    @property
    def notable(self) -> bool:
        return self.klass in NOTABLE_CLASSES


UNMATCHED = Rule({"name": "unmatched", "class": "unmatched"})


class PatternLibrary:
    def __init__(self, rules: list[Rule], version: int):
        self.rules = rules
        self.version = version

    @classmethod
    def load(cls, path: Path) -> "PatternLibrary":
        raw = yaml.safe_load(path.read_text())
        return cls([Rule(d) for d in raw["rules"]], raw.get("version", 0))

    def classify(self, ev: dict) -> Rule:
        for rule in self.rules:
            if rule.matches(ev):
                return rule
        return UNMATCHED


# Replace digit runs with '#', but keep error codes (ORA-/TNS-/etc.) intact:
# a digit run immediately preceded by "<letter>-" is part of a code.
_NUM_RE = re.compile(r"(?<![A-Za-z]-)(?<!\d)\d+")
_WS_RE = re.compile(r"\s+")

ORA_CODE_RE = re.compile(r"\b(ORA|TNS|LGWR|PGA|RMAN|DGM)-\d{3,6}\b")


def message_template(msg: str, width: int = 160) -> str:
    t = _NUM_RE.sub("#", msg)
    t = _WS_RE.sub(" ", t).strip()
    return t[:width]


# Trace files named by an alert-log line: "Errors in file …:", "Incident …
# created, dump file: …", "See trace file …". Incident-dir paths match too;
# filebeat does not ship those, so they simply find no documents. `+` is a
# path character here because ASM writes under `diag/asm/+asm/+ASM1/`.
TRACE_PATH_RE = re.compile(r"(?<![\w./+-])(/[\w+][\w./+-]*\.trc)\b")


def extract_trace_paths(msg: str) -> list[str]:
    """Absolute `.trc` paths named in one message, in order, deduped.

    Must be applied per event, never to a group's exemplar: `message_template`
    collapses digit runs, so events naming different trace files share one
    group and only the first exemplar's path would survive."""
    seen: dict[str, None] = {}
    for m in TRACE_PATH_RE.finditer(msg):
        seen[m.group(1)] = None
    return list(seen)


def extract_codes(msg: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in ORA_CODE_RE.finditer(msg):
        code = m.group(0)
        # normalize zero-padding: ORA-00600 -> ORA-600
        prefix, num = code.split("-", 1)
        seen[f"{prefix}-{int(num)}"] = None
    return list(seen)
