#!/usr/bin/env python3
"""Idempotently install/remove the crontab block for `config/schedule.json`.

docs/scheduling.md is the human-readable copy of the installed crontab;
`n8n/scripts/watchdog.py --fix`'s `crontab` check reads the same
config/schedule.json to warn when the two drift. Until now the crontab
itself was hand-edited (`crontab -e`) to match — this script generates that
block from schedule.json and replaces it in place, so installing is a
re-run rather than a hand copy-paste.

    python3 scripts/manage_cron.py install [--node onprem|analyst] [--dry-run]
    python3 scripts/manage_cron.py remove  [--node onprem|analyst] [--dry-run]
    python3 scripts/manage_cron.py status  [--node onprem|analyst]

Everything else in the user's crontab (PATH, LANGFUSE_* secrets, unrelated
jobs) is preserved byte-for-byte. `install` replaces an existing managed
block or appends a new one; `remove` deletes it. Both also recognize and
strip stray unmarked lines that already match a schedule.json entry for
--node (e.g. the original hand-written block this script's first run
adopts), so a crontab never ends up with two copies of the same job and
`remove` works even before anything has been marked.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "config" / "schedule.json"

BEGIN = "# >>> dbwiki cron (managed by scripts/manage_cron.py — see config/schedule.json) >>>"
END = "# <<< dbwiki cron <<<"

NODE_REPO_DIR = {
    "onprem": str(ROOT),
    "analyst": "/opt/logbook",
}


def _log_path(command: str) -> str:
    if command.startswith("python3 elk/"):
        return ".state/elk/emitter.log"
    if command.startswith("python3 n8n/scripts/watchdog"):
        return ".state/watchdog.log"
    return ".state/cron.log"


def _cmd_line(command: str) -> str:
    return command.replace("dbwiki ", "uv run dbwiki ", 1) if command.startswith("dbwiki ") else command


def _entries(node: str) -> list:
    entries = json.loads(SCHEDULE.read_text())["entries"]
    selected = [e for e in entries if e.get("node", "onprem") == node]
    if not selected:
        raise SystemExit(f"no config/schedule.json entries for node={node!r}")
    return selected


def render_block(node: str) -> str:
    repo_dir = NODE_REPO_DIR.get(node, str(ROOT))
    lines = [BEGIN]
    for e in _entries(node):
        cmd = _cmd_line(e["command"])
        log = _log_path(e["command"])
        lines.append(f"{e['cron']}  cd {repo_dir} && {cmd} >> {log} 2>&1  # {e['entry']}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _line_matches_entry(line: str, entry: dict) -> bool:
    norm = _norm(line)
    if not norm or norm.startswith("#"):
        return False
    if not norm.startswith(_norm(entry["cron"])):
        return False
    return entry["command"] in norm or _cmd_line(entry["command"]) in norm


def _strip_stray_entries(lines: list, entries: list) -> tuple:
    """Drop lines outside the managed block that already match a
    schedule.json entry (e.g. the original hand-written jobs), so install
    never duplicates them and remove works even pre-adoption."""
    kept, dropped = [], 0
    for line in lines:
        if any(_line_matches_entry(line, e) for e in entries):
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def read_crontab() -> str:
    cp = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if cp.returncode != 0:
        if "no crontab" in (cp.stderr or "").lower():
            return ""
        raise RuntimeError(f"crontab -l failed: {cp.stderr.strip()}")
    return cp.stdout


def write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def _find_block(lines: list):
    try:
        start = lines.index(BEGIN)
        end = lines.index(END, start)
    except ValueError:
        return None
    return start, end


def install(node: str, dry_run: bool) -> None:
    entries = _entries(node)
    block = render_block(node)
    current = read_crontab()
    lines = current.splitlines()
    span = _find_block(lines)
    if span:
        start, end = span
        rest = lines[:start] + lines[end + 1:]
    else:
        rest = lines
    rest, dropped = _strip_stray_entries(rest, entries)
    new_lines = rest + ([""] if rest and rest[-1].strip() else []) + block.splitlines()
    new_text = "\n".join(new_lines) + "\n"
    if new_text == current:
        print("already up to date")
        return
    print(block, end="")
    if dropped:
        print(f"(adopted {dropped} pre-existing unmarked line(s))")
    if dry_run:
        print("(dry run — not applied)")
        return
    write_crontab(new_text)
    print("installed")


def remove(node: str, dry_run: bool) -> None:
    entries = _entries(node)
    current = read_crontab()
    lines = current.splitlines()
    span = _find_block(lines)
    if span:
        start, end = span
        lines = lines[:start] + lines[end + 1:]
        block_removed = end - start + 1
    else:
        block_removed = 0
    lines, dropped = _strip_stray_entries(lines, entries)
    if not block_removed and not dropped:
        print("not installed")
        return
    new_text = ("\n".join(lines).rstrip("\n") + "\n") if lines else ""
    if dry_run:
        print(f"would remove {block_removed} block line(s) + {dropped} stray line(s)")
        return
    write_crontab(new_text)
    print(f"removed ({block_removed} block line(s), {dropped} stray line(s))")


def status(node: str) -> None:
    lines = read_crontab().splitlines()
    span = _find_block(lines)
    if not span:
        entries = _entries(node)
        _, dropped = _strip_stray_entries(lines, entries)
        if dropped:
            print(f"not installed as a managed block, but {dropped} unmarked line(s) "
                  f"match schedule.json ({node}) — run `install` to adopt them")
        else:
            print("not installed")
        return
    start, end = span
    installed_block = "\n".join(lines[start:end + 1]) + "\n"
    expected_block = render_block(node)
    if installed_block == expected_block:
        print(f"installed and up to date ({node})")
    else:
        print(f"installed but out of date with config/schedule.json ({node}) — run `install` to sync")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("install", help="add/update the managed crontab block")
    i.add_argument("--node", default="onprem", choices=sorted(NODE_REPO_DIR))
    i.add_argument("--dry-run", action="store_true")

    r = sub.add_parser("remove", help="delete the managed block and any stray unmarked lines")
    r.add_argument("--node", default="onprem", choices=sorted(NODE_REPO_DIR))
    r.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("status", help="check whether the block matches schedule.json")
    s.add_argument("--node", default="onprem", choices=sorted(NODE_REPO_DIR))

    args = p.parse_args()
    if args.cmd == "install":
        install(args.node, args.dry_run)
    elif args.cmd == "remove":
        remove(args.node, args.dry_run)
    elif args.cmd == "status":
        status(args.node)
    return 0


if __name__ == "__main__":
    sys.exit(main())
