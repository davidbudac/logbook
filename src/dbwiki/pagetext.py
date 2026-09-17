"""Surgical, idempotent edits to one curated Markdown page.

A page is a YAML frontmatter block followed by a title and a sequence of
`## ` sections. Every function here is a pure `str -> str` transform that
preserves the bytes it did not target. No Path, no git, no domain vocabulary.

Two invariants hold for every function and callers depend on both:

1. Idempotent by content. Applying the same edit twice yields byte-identical
   text, so a replayed transaction touches nothing.
2. Nothing outside the target moves. A heading regex matches only its own
   writer's heading shape. `## Update <day>` and `## <day> — ` belong to the
   ingest writer; `## Action <ISO timestamp>` belongs to the incident
   lifecycle; neither can match the other.
"""

import re
from collections.abc import Mapping

import yaml

FM_RE = re.compile(r"\A---\n(.*?)\n---\s*?\n", re.S)
PLACEHOLDER_RE = re.compile(r"\A\(.*\)\Z")


def nl(text: str) -> str:
    """Exactly one trailing newline. Every writer ends here so two writers
    never disagree about the file's last byte."""
    return text.rstrip("\n") + "\n"


def oneline(text: str) -> str:
    """Collapse whitespace and escape `|` so the result is safe as one table
    cell, one bullet, or one `log.md` line."""
    return " ".join(text.split()).replace("|", r"\|")


def frontmatter(text: str) -> dict:
    """Leading YAML frontmatter as a dict; `{}` when absent or unparseable.
    Silent by design: `lint`'s `frontmatter-malformed` rule is the alarm."""
    m = FM_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def set_frontmatter(text: str, updates: Mapping[str, str | None]) -> str:
    """Set, replace, or delete frontmatter keys in one pass.

    A `str` value sets `key: value`; `None` deletes the key's line. Existing
    keys keep their position, new keys are appended in `updates` order, and
    every byte outside the touched lines survives unchanged. Values must be
    one physical line; a mapping value is rendered by `fm_flow_map` first.
    A key the block repeats ends up written once, at its first position.
    Returns `text` unchanged when there is no frontmatter block.

    Twice: identical bytes."""
    m = FM_RE.match(text)
    if not m:
        return text
    written: set[str] = set()
    body: list[str] = []
    for line in m.group(1).split("\n"):
        key, sep, _ = line.partition(":")
        if not sep or key not in updates:
            body.append(line)
            continue
        if key in written:
            continue
        written.add(key)
        if updates[key] is not None:
            body.append(f"{key}: {updates[key]}")
    for key, value in updates.items():
        if key not in written and value is not None:
            body.append(f"{key}: {value}")
    return "---\n" + "\n".join(body) + "\n---\n" + text[m.end():]


def fm_flow_map(fields: Mapping[str, str]) -> str:
    """Render `{k: 'v', ...}` as a single-line YAML flow mapping, every scalar
    single-quoted (an unquoted ISO timestamp resolves to a `datetime`; an
    unquoted `,` or `}` ends the mapping early). Insertion order, so the bytes
    are deterministic. Escapes `'` as `''`."""
    escaped = {k: str(v).replace("'", "''") for k, v in fields.items()}
    return "{" + ", ".join(f"{k}: '{v}'" for k, v in escaped.items()) + "}"


def sections(text: str) -> list[tuple[str, str]]:
    """`(heading line, body)` for every `## ` section in page order. Body runs
    to the next `## ` line or EOF. Text before the first section is not
    returned; this is a reader, not a splitter."""
    lines = text.splitlines()
    heads = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    return [(lines[i], "\n".join(lines[i + 1:end]))
            for i, end in zip(heads, heads[1:] + [len(lines)])]


def section_append(text: str, section: str, line: str, header: str = "") -> str:
    """Append `line` to the `## <section>` block, creating the section (with
    `header`) at the end of the page when missing. Placeholder lines like
    `(none yet)` are replaced by the first real entry.

    Twice: identical bytes, because an exact `line` already present anywhere
    in the page short-circuits. That whole-page check is why
    `section_move_line` removes before it appends."""
    if line in text:
        return text
    lines = text.rstrip("\n").splitlines()
    head = re.compile(rf"(?i)\A##\s+{re.escape(section)}\s*\Z")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        block = f"\n\n## {section}\n\n" + (f"{header}\n" if header else "")
        return nl("\n".join(lines) + block + line)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    body = [ln for ln in lines[start + 1:end]
            if not PLACEHOLDER_RE.match(ln.strip())]
    while body and not body[-1].strip():
        body.pop()
    body = body or [""]
    return nl("\n".join(lines[:start + 1] + body + [line, ""] + lines[end:]))


def section_line_replace(text: str, section: str, prefix: str, line: str,
                         header: str = "") -> str:
    """Within `## <section>`, replace every line starting with `prefix` by
    `line` at the first match's position; later matches are deleted. Falls
    back to `section_append` when the section or a match is missing.

    Caller invariant: `prefix` is a prefix of `line`, so a second application
    is a no-op."""
    lines = text.rstrip("\n").splitlines()
    head = re.compile(rf"(?i)\A##\s+{re.escape(section)}\s*\Z")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return section_append(text, section, line, header=header)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    hits = [i for i in range(start + 1, end) if lines[i].startswith(prefix)]
    if not hits:
        return section_append(text, section, line, header=header)
    out = [line if i == hits[0] else ln
           for i, ln in enumerate(lines) if i == hits[0] or i not in hits]
    return nl("\n".join(out))


def section_remove_line(text: str, section: str, prefix: str) -> tuple[str, str | None]:
    """Delete every line in `## <section>` starting with `prefix`. Returns the
    new text and the first removed line (its exact bytes), or None when there
    was nothing to remove. The section itself stays, even when it ends up
    empty: `## Open incidents` must survive the last incident resolving."""
    lines = text.rstrip("\n").splitlines()
    head = re.compile(rf"(?i)\A##\s+{re.escape(section)}\s*\Z")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return text, None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    hits = [i for i in range(start + 1, end) if lines[i].startswith(prefix)]
    if not hits:
        return text, None
    body = [ln for i, ln in enumerate(lines[start + 1:end], start + 1)
            if i not in hits]
    while body and not body[-1].strip():
        body.pop()
    out = lines[:start + 1] + body + [""] + lines[end:]
    return nl("\n".join(out)), lines[hits[0]]


def section_drop_if_empty(text: str, section: str) -> str:
    """Take `## <section>` off the page, heading and all, when its body holds
    nothing but blank lines. The counterpart to `section_append`'s
    create-on-demand: a section a writer conjures the first time it is needed
    should not outlive its last entry as a bare heading. Returns `text`
    unchanged when the section is absent or holds anything at all, a
    `(none yet)` placeholder line included; only whitespace reads as empty.

    Which sections are on demand is the caller's policy. `section_remove_line`
    deliberately keeps an emptied section, so a caller that wants the section
    gone asks for it here.

    Twice: identical bytes, the section being gone after the first call."""
    lines = text.rstrip("\n").splitlines()
    head = re.compile(rf"(?i)\A##\s+{re.escape(section)}\s*\Z")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    if any(ln.strip() for ln in lines[start + 1:end]):
        return text
    return nl("\n".join(lines[:start] + lines[end:]))


def section_move_line(text: str, *, frm: str, to: str, prefix: str,
                      fallback: str, header: str = "") -> str:
    """Move the entry starting with `prefix` from `## <frm>` to `## <to>`,
    creating `to` at the end of the page when missing.

    The moved line keeps its exact existing bytes (the label text a human or
    an earlier writer gave it); `fallback` is appended instead when `frm`
    never listed it, which is the pre-migration case. Remove first, then
    append, because `section_append` treats a line already present anywhere
    in the page as done.

    Twice: identical bytes."""
    after, removed = section_remove_line(text, frm, prefix)
    if removed is not None:
        return section_append(after, to, removed, header=header)
    head = re.compile(rf"(?i)\A##\s+{re.escape(to)}\s*\Z")
    listed = any(ln.startswith(prefix)
                 for heading, body in sections(after) if head.match(heading)
                 for ln in body.splitlines())
    return after if listed else section_append(after, to, fallback,
                                               header=header)


def day_block_replace(text: str, head_re: re.Pattern, block: str) -> str:
    """Replace every `## ` section whose heading matches `head_re` with
    `block`, spliced where the first match stood; append `block` when nothing
    matches.

    `head_re` must match only its own writer's heading shape. On the match
    path this collapses runs of 3+ newlines to 2 across the whole page, so no
    writer may emit a blank-line pair inside a fenced block;
    `incidents.ActionRecord` enforces that on `notes`."""
    lines = text.rstrip("\n").splitlines()
    heads = [i for i, ln in enumerate(lines)
             if ln.startswith("## ") and head_re.match(ln)]
    if not heads:
        return nl(text) + block
    spans = []
    for i in heads:
        end = next((j for j in range(i + 1, len(lines))
                    if lines[j].startswith("## ")), len(lines))
        spans.append((i, end))
    dropped = set()
    for s, e in spans:
        dropped.update(range(s, e))
    out: list[str] = []
    for i, ln in enumerate(lines):
        if i == spans[0][0]:
            out += [""] + block.strip("\n").splitlines() + [""]
        if i in dropped:
            continue
        out.append(ln)
    return nl(re.sub(r"\n{3,}", "\n\n", "\n".join(out)))


def log_append(log: str | None, line: str) -> str:
    """The append-only `log.md` contract line, added once (exact-line match
    over the whole page). Creates the page when `log` is None."""
    log = log or "---\ntype: log\n---\n\n# Log\n"
    if line in log:
        return log
    tail = log.rstrip("\n")
    sep = "\n\n" if tail.rsplit("\n", 1)[-1].startswith("#") else "\n"
    return tail + sep + line + "\n"
