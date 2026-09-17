"""The link board a portal serves, read from `config/links.yaml`.

What matters is the boundary. Above `load_links` nothing checks anything, so
every way a hand-edited file can be wrong has to stop here, and it has to stop
with a message that names the row rather than the exception's own line number.
The happy path runs over the repository's real board, so a link added to it in
future is parsed by this test too.
"""

from pathlib import Path

import pytest
import yaml

from dbwiki.links import DEFAULT_TITLE, LinkBoard, Tag, empty_board, load_links

ROOT = Path(__file__).resolve().parents[1]


def board(tmp_path: Path, raw: dict) -> LinkBoard:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "links.yaml").write_text(yaml.safe_dump(raw))
    return load_links(tmp_path)


def one(**overrides) -> dict:
    link = {"name": "Kibana", "url": "http://box:5601/", "tag": "tailscale"}
    return {"sections": [{"title": "Dashboards", "links": [link | overrides]}]}


@pytest.mark.parametrize("name", ["links.yaml", "links.yaml.example"])
def test_the_repository_board_parses(name, tmp_path):
    src = ROOT / "config" / name
    if not src.exists():                 # a fresh clone has only the example
        pytest.skip(f"no config/{name} in this checkout")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "links.yaml").write_text(src.read_text())
    got = load_links(tmp_path)
    assert got.title, "the shipped board names itself"
    assert got.sections, "the shipped board has sections"
    links = [link for section in got.sections for link in section.links]
    assert all(isinstance(link.tag, Tag) for link in links), \
        "every tag on the shipped board is one of the three"
    assert all(link.url.startswith("http") for link in links), \
        "every url on the shipped board is http or https"
    assert any(link.what for link in links), \
        "the shipped board explains at least some of its links"


def test_a_missing_file_is_an_empty_board(tmp_path):
    got = load_links(tmp_path)
    assert got == empty_board(), "a checkout with no board gets the empty one"
    assert got.title == DEFAULT_TITLE, "the empty board still has a heading"
    assert got.sections == (), "the empty board has no sections"


def test_absent_what_and_note_are_empty_strings(tmp_path):
    got = board(tmp_path, one())
    assert got.sections[0].note == "", "a section with no note carries an empty one"
    assert got.sections[0].links[0].what == "", \
        "a link with no what carries an empty one"


def test_a_link_with_no_name_is_refused(tmp_path):
    raw = {"sections": [{"title": "Dashboards",
                         "links": [{"url": "http://box/", "tag": "github"}]}]}
    with pytest.raises(ValueError, match="links.yaml") as err:
        board(tmp_path, raw)
    assert "link 1 of section 'Dashboards'" in str(err.value), \
        "a nameless link is placed by its position and its section"


def test_a_link_with_no_url_is_refused(tmp_path):
    with pytest.raises(ValueError, match="links.yaml") as err:
        board(tmp_path, one(url=""))
    assert "'Kibana'" in str(err.value), "the refusal names the link"
    assert "no url" in str(err.value), "the refusal says what is missing"


def test_an_unknown_tag_is_refused(tmp_path):
    with pytest.raises(ValueError, match="links.yaml") as err:
        board(tmp_path, one(tag="grafana"))
    assert "'Kibana'" in str(err.value), "the refusal names the link"
    assert "tailscale" in str(err.value), "the refusal lists the tags that are allowed"


def test_a_url_that_is_not_http_is_refused(tmp_path):
    with pytest.raises(ValueError, match="links.yaml") as err:
        board(tmp_path, one(url="javascript:alert(1)"))
    assert "'Kibana'" in str(err.value), "the refusal names the link"
    assert "not http or https" in str(err.value), "the refusal says why"
