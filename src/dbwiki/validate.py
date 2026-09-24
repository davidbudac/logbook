"""Boundary checks for values that arrive from outside: portal requests,
agent results, the exchange repo, the command line.

Each function takes the raw value and returns it unchanged (or, for
`confined`, the resolved path) when it is acceptable, and raises
`ValueError` naming the offending value when it is not. Call them where
the value enters, before it becomes a file name, a frontmatter scalar or a
section heading; everything past that point may trust it.

Pure: no config, no domain vocabulary. `confined` is the only function that
touches the filesystem, and only to resolve symlinks."""

import datetime as dt
import re
from pathlib import Path

SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SAFE_ID_MAX = 200
REVIEW_ID_RE = re.compile(r"\A[0-9]{4}-W([0-9]{2})\Z")
INSTANT_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
INSTANT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def safe_id(s: str, what: str = "id") -> str:
    """An id, slug, code or run id that will become (part of) a file name.
    ASCII letters, digits, `.`, `_` and `-`, starting with a letter or
    digit, at most SAFE_ID_MAX characters, and no `..` anywhere — so it can
    be neither a path, a parent reference, nor a hidden file."""
    if not isinstance(s, str) or not SAFE_ID_RE.match(s):
        raise ValueError(f"{what}: {s!r} is not a safe id "
                         "([A-Za-z0-9][A-Za-z0-9._-]*)")
    if len(s) > SAFE_ID_MAX:
        raise ValueError(f"{what}: {s[:40]!r}… is longer than "
                         f"{SAFE_ID_MAX} characters")
    if ".." in s:
        raise ValueError(f"{what}: {s!r} contains '..'")
    return s


def is_safe_id(s: str) -> bool:
    try:
        safe_id(s)
    except ValueError:
        return False
    return True


def review_id(s: str) -> str:
    """A weekly review id, ISO year and week: `2026-W38`."""
    m = REVIEW_ID_RE.match(s) if isinstance(s, str) else None
    if not m or not 1 <= int(m.group(1)) <= 53:
        raise ValueError(f"review id: {s!r} is not YYYY-Www")
    return s


def confined(root: Path, rel: str | Path) -> Path:
    """`root / rel`, resolved, provided it lands strictly below `root`
    (also resolved). Rejects absolute `rel`, `..` escapes and symlinks
    pointing out of the tree; `root` itself is not a valid target either."""
    if Path(rel).is_absolute():
        raise ValueError(f"path: {str(rel)!r} is absolute")
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"path: {str(rel)!r} escapes {str(base)!r}")
    return target


def instant(s: str) -> str:
    """A UTC instant exactly as the incident pages write it,
    `YYYY-MM-DDTHH:MM:SSZ`, that also exists on the calendar:
    `2026-02-30T10:00:00Z` has the shape but no such day, and YAML would
    choke on it when the page is read back."""
    if not isinstance(s, str) or not INSTANT_RE.match(s):
        raise ValueError(f"instant: {s!r} is not YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt.datetime.strptime(s, INSTANT_FMT)
    except ValueError as exc:
        raise ValueError(f"instant: {s!r} is not a real instant") from exc
    return s


def is_instant(s: str) -> bool:
    try:
        instant(s)
    except ValueError:
        return False
    return True


def one_line(s: str, what: str = "value") -> str:
    """A value that must stay one physical line. Rejects every character
    `str.splitlines` breaks on (`\\n`, `\\r`, `\\v`, `\\f`, `\\x1c`-`\\x1e`,
    `\\x85`, U+2028, U+2029), trailing ones included: YAML and the page
    parsers split on all of them, so any would change the value on the way
    back in."""
    if not isinstance(s, str) or s.splitlines() != ([s] if s else []):
        raise ValueError(f"{what}: must be one line, got {s!r}")
    return s
