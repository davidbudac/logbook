"""The prompt instruction text, read from `config/prompts/`.

Every instruction or rule block a model is sent — the ingest template, the
report contracts and rules, the research and caveats instructions, the review
synthesis rules, the advisory tool preambles — lives as one file under
`config/prompts/<name>.md`, so "the prompt" can be read in one place. The
modules keep the assembly: which evidence goes in, the caps, the ordering, the
allow-lists. This module only reads text; it has no template engine.

File conventions, which `load` relies on:

- A file holds the block verbatim. Exactly one trailing newline is dropped
  (the one every editor adds), so a block that must end in a newline gets it
  from the Python that assembles it, never from the file.
- A file named in a `str.format` call (`report`, `research`,
  `research-caveats`, `advisory/*`) uses `{field}` placeholders and may not
  hold a literal brace. `ingest` uses mustache `{{db}}` fields filled with
  `str.replace` (it is registered with Langfuse as a prompt with variables).
  `researcher/*` (the ADR-0002 researcher's prompt) also uses `{{field}}`,
  filled in one pass by `dbwiki_researcher.cli._fill`, because its result
  shapes are literal JSON. Every other file is sent as is, braces and all —
  the JSON contracts.
- `shared/*` blocks are also rendered into the wiki's `AGENTS.md` between
  `<!-- BEGIN generated: <name> ... -->` / `<!-- END generated -->` markers,
  so the structured and agentic modes say the same rule in the same words.
  `drift` reports a generated section that no longer matches its file;
  `uv run python -m dbwiki.prompts sync <AGENTS.md>` rewrites them in
  place (`check` reports drift, `render` prints a section to paste).

Files are read once, at the importing module's import, and a missing one
fails that import: a prompt with a hole in it is worse than no run.

Lookup order: `dbwiki/_prompts/` inside the installed package (a wheel build
force-includes `config/prompts` there, see pyproject.toml), then
`config/prompts/` of the source checkout this module sits in (`uv sync`'s
editable install). Stdlib only: the researcher's import footprint is pinned.
"""

import functools
import re
import sys
from pathlib import Path

_PACKAGED = Path(__file__).parent / "_prompts"
_CHECKOUT = Path(__file__).resolve().parents[2] / "config" / "prompts"

#: the blocks both modes share: the structured builders include them, and the
#: wiki's AGENTS.md carries them as a generated section
SHARED = ("shared/evidence-rules",)

_BEGIN = ("<!-- BEGIN generated: {name} — from config/prompts/{name}.md; "
          "do not edit here, run `uv run python -m dbwiki.prompts sync "
          "<this file>` -->")
_END = "<!-- END generated -->"
_SECTION_RE = re.compile(
    r"^<!-- BEGIN generated: (?P<name>\S+)[^\n]*-->\n(?P<body>.*?)^<!-- END generated -->$",
    re.M | re.S)
_BEGIN_RE = re.compile(r"^<!-- BEGIN generated: (\S+)", re.M)


def root() -> Path:
    """The directory the prompt files are read from."""
    for cand in (_PACKAGED, _CHECKOUT):
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        f"prompt directory not found: neither {_PACKAGED} (installed wheel) "
        f"nor {_CHECKOUT} (source checkout) exists")


@functools.cache
def load(name: str) -> str:
    """The block `config/prompts/<name>.md`, minus one trailing newline.
    Raises FileNotFoundError naming the file when it is missing."""
    path = root() / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"prompt file missing: {path} ({exc})") from exc
    return text[:-1] if text.endswith("\n") else text


def section(name: str) -> str:
    """The generated AGENTS.md section for shared block `name`, markers
    included."""
    return f"{_BEGIN.format(name=name)}\n{load(name)}\n{_END}"


def drift(agents_md: str) -> list[str]:
    """Findings for every generated section in `agents_md` that does not match
    its prompt file. A file with no markers has no findings: a wiki adopts the
    generated section when its operator pastes the markers in."""
    findings: list[str] = []
    closed = 0
    for m in _SECTION_RE.finditer(agents_md):
        closed += 1
        name = m.group("name")
        if name not in SHARED:
            findings.append(f"generated section {name!r}: not a shared prompt "
                            f"block (known: {', '.join(SHARED)})")
            continue
        if m.group("body") != load(name) + "\n":
            findings.append(f"generated section {name!r} differs from "
                            f"config/prompts/{name}.md: run `uv run python -m "
                            f"dbwiki.prompts sync <AGENTS.md>`")
    if len(_BEGIN_RE.findall(agents_md)) != closed:
        findings.append("a `<!-- BEGIN generated: ... -->` marker has no "
                        "matching `<!-- END generated -->`")
    return findings


def sync(agents_md: str) -> str:
    """`agents_md` with every generated section re-rendered from its file.
    Only rewrites between existing markers; adds none."""
    def fill(m: re.Match) -> str:
        name = m.group("name")
        return section(name) if name in SHARED else m.group(0)
    return _SECTION_RE.sub(fill, agents_md)


def main(argv: list[str] | None = None) -> int:
    """`python -m dbwiki.prompts render [NAME]` prints a section to paste;
    `check PATH` exits 1 on drift; `sync PATH` rewrites PATH's sections."""
    args = sys.argv[1:] if argv is None else argv
    usage = ("usage: python -m dbwiki.prompts render [NAME] | check PATH | "
             "sync PATH")
    if not args or args[0] not in ("render", "check", "sync"):
        print(usage, file=sys.stderr)
        return 2
    if args[0] == "render":
        names = args[1:] or list(SHARED)
        print("\n\n".join(section(n) for n in names))
        return 0
    if len(args) != 2:
        print(usage, file=sys.stderr)
        return 2
    path = Path(args[1])
    text = path.read_text(encoding="utf-8")
    if not _BEGIN_RE.search(text):
        print(f"{path}: no generated section (markers absent) — paste the "
              f"output of `python -m dbwiki.prompts render` to adopt one",
              file=sys.stderr)
        return 0 if args[0] == "check" else 2
    if args[0] == "check":
        findings = drift(text)
        for f in findings:
            print(f"{path}: {f}")
        return 1 if findings else 0
    new = sync(text)
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"{path}: generated sections rewritten")
    else:
        print(f"{path}: up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
