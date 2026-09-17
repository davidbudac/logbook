"""The operator's link board: `config/links.yaml` read into values the portal
can hand to the page.

The board is configuration, not wiki state. Nothing in the pipeline writes it
and nothing derives it from the logs; an operator edits the file and the links
change. That is why it sits beside `config/dbwiki.yaml` and is read the same
way, with `yaml.safe_load` and no schema library.

This module is the boundary. Every check the board needs happens in
`load_links`, so a `LinkBoard` in hand is already known to have a name and an
http(s) url on every link and one of three tags on each. Code above this line
places values into the page without asking whether they are there.

A missing file is an empty board rather than an error. The portal has to start
on a fresh checkout, and an operator who has not written a link board yet has
not made a mistake; the screen says so itself. A file that *is* there and is
wrong is a different matter: it is an edit that did not do what its author
meant, so it raises, and the message names the offending link so the operator
can find the line. A url is rejected unless its scheme is http or https,
because the page renders it as an anchor the operator clicks: `javascript:` and
`data:` are not links, they are code that arrives through a config file.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import yaml

#: The heading an empty board carries, so the screen has a title before the
#: operator has written one.
DEFAULT_TITLE = "Links"

_SCHEMES = ("http", "https")


class Tag(StrEnum):
    """Where a link goes, as far as the operator needs to know before clicking.
    TAILSCALE means "only reachable on the tailnet", which is the one fact that
    decides whether a link will work from where the operator is sitting.
    GITHUB and ARTIFACT are both public, and are kept apart because a repository
    and a written report are read in different moods."""

    TAILSCALE = "tailscale"
    GITHUB = "github"
    ARTIFACT = "artifact"


@dataclass(frozen=True)
class Link:
    """One destination. `what` is empty rather than None when the operator gave
    no explanation: the page prints it or does not, and a single string type
    keeps that decision at the one place that renders it."""

    name: str
    what: str
    url: str
    tag: Tag


@dataclass(frozen=True)
class Section:
    """A group of links under a heading, with an optional `note` for the thing
    the operator needs to know about the whole group rather than any one link."""

    title: str
    note: str
    links: tuple[Link, ...]


@dataclass(frozen=True)
class LinkBoard:
    """The whole file. `sections` is empty for a board that does not exist,
    which is the only shape the screen treats specially."""

    title: str
    intro: str
    sections: tuple[Section, ...]


def empty_board() -> LinkBoard:
    """The board a checkout with no `config/links.yaml` has. A function rather
    than a module constant so it can be a `default_factory` and so no caller
    can hold a shared object it might come to expect to mutate."""
    return LinkBoard(title=DEFAULT_TITLE, intro="", sections=())


def load_links(root: Path | str) -> LinkBoard:
    """Read `root/config/links.yaml`.

    Absent file -> `empty_board()`. Present and malformed -> `ValueError`
    naming the item, never a partial board: half a link board silently missing
    the row the operator just added is worse than a refusal at startup, because
    the operator would conclude the link is unreachable rather than mistyped."""
    path = Path(root) / "config" / "links.yaml"
    if not path.exists():
        return empty_board()
    raw = yaml.safe_load(path.read_text()) or {}
    sections = tuple(
        _section(s or {}, i) for i, s in enumerate(raw.get("sections") or [], start=1)
    )
    return LinkBoard(
        title=str(raw.get("title") or DEFAULT_TITLE),
        intro=str(raw.get("intro") or ""),
        sections=sections,
    )


def _section(raw: dict, position: int) -> Section:
    title = str(raw.get("title") or f"section {position}")
    links = tuple(
        _link(link or {}, title, i)
        for i, link in enumerate(raw.get("links") or [], start=1)
    )
    return Section(title=title, note=str(raw.get("note") or ""), links=links)


def _link(raw: dict, section: str, position: int) -> Link:
    name = str(raw.get("name") or "")
    where = f"{name!r}" if name else f"link {position} of section {section!r}"
    if not name:
        raise ValueError(f"links.yaml: {where} has no name")
    url = str(raw.get("url") or "")
    if not url:
        raise ValueError(f"links.yaml: {where} has no url")
    if urlsplit(url).scheme not in _SCHEMES:
        raise ValueError(
            f"links.yaml: {where} has url {url!r}, which is not http or https")
    tag = str(raw.get("tag") or "")
    try:
        parsed = Tag(tag)
    except ValueError:
        allowed = ", ".join(t.value for t in Tag)
        raise ValueError(
            f"links.yaml: {where} has tag {tag!r}; allowed: {allowed}") from None
    return Link(name=name, what=str(raw.get("what") or ""), url=url, tag=parsed)
