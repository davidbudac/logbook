#!/usr/bin/env python3
"""Render every markdown page under the given directories through
`dbwiki.markdown.render`, each under a wall-clock limit.

The portal renders wiki pages on request, so a page the renderer cannot
finish is a stuck server thread per click (issue 01: a line starting with `|`
looped forever). CI runs this over `wiki-template/`; point it at a wiki
checkout to prove a real snapshot renders too:

    uv run python tools/render_corpus.py                      # wiki-template/
    uv run python tools/render_corpus.py ../wiki --timeout 5

Exit 0 when every page rendered in time, 1 when any hung or raised (each is
named on stderr), 2 on bad arguments. Unix only: the limit is a SIGALRM
timer, which interrupts a pure-Python loop in the main thread.
"""
from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = (ROOT / "wiki-template",)
DEFAULT_TIMEOUT_S = 10.0


class RenderTimeout(Exception):
    """A page took longer than the limit."""


def _alarm(signum, frame):
    raise RenderTimeout


def pages(dirs: Iterable[Path]) -> list[Path]:
    """Every `*.md` under `dirs`, sorted, `.git` excluded."""
    found: set[Path] = set()
    for root in dirs:
        found.update(p for p in Path(root).rglob("*.md")
                     if ".git" not in p.parts and p.is_file())
    return sorted(found)


def within(seconds: float, fn: Callable[[], object]) -> object:
    """`fn()`, or `RenderTimeout` once `seconds` have passed."""
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def check(paths: Iterable[Path], timeout_s: float = DEFAULT_TIMEOUT_S,
          render: Callable[[str], object] | None = None) -> list[str]:
    """One problem line per page that hung or raised; `[]` when all
    rendered. `render` defaults to `dbwiki.markdown.render`."""
    if render is None:
        from dbwiki import markdown
        render = markdown.render
    problems = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            within(timeout_s, lambda: render(text))
        except RenderTimeout:
            problems.append(f"{path}: did not render within {timeout_s}s")
        except Exception as e:  # noqa: BLE001 — every failure is reported
            problems.append(f"{path}: {type(e).__name__}: {e}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="*", type=Path,
                    help="directories to walk (default: wiki-template/)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"seconds per page (default {DEFAULT_TIMEOUT_S})")
    args = ap.parse_args(argv)
    dirs = args.dirs or list(DEFAULT_DIRS)
    missing = [d for d in dirs if not d.is_dir()]
    if missing or args.timeout <= 0:
        print(f"render_corpus: no such directory: "
              f"{', '.join(map(str, missing))}" if missing
              else "render_corpus: --timeout must be positive", file=sys.stderr)
        return 2
    found = pages(dirs)
    problems = check(found, args.timeout)
    for line in problems:
        print(line, file=sys.stderr)
    print(f"rendered {len(found) - len(problems)}/{len(found)} pages")
    return 1 if problems or not found else 0


if __name__ == "__main__":
    sys.exit(main())
