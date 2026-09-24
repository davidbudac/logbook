import pytest

from dbwiki import validate

# ---- safe_id ---------------------------------------------------------------

GOOD_IDS = ["cdb1", "ORA-00600", "2026-09-23_run.1", "a", "x" * 200,
            "run-20260923T101500Z", "v1.2"]
BAD_IDS = ["", ".", "..", "../../x", "../../outside/pwn", "/etc/passwd",
           "a/../b", "a/b", "a\\b", "..hidden", ".hidden", "-flag", "_x",
           "a..b", "x..", "a b", "a\nb", "a\n", "a\x00b", "é", "٣",
           "x" * 201, "C:foo", "~root"]


@pytest.mark.parametrize("s", GOOD_IDS)
def test_safe_id_accepts(s):
    assert validate.safe_id(s) == s
    assert validate.is_safe_id(s)


@pytest.mark.parametrize("s", BAD_IDS)
def test_safe_id_rejects(s):
    with pytest.raises(ValueError, match="slug"):
        validate.safe_id(s, "slug")
    assert not validate.is_safe_id(s)


def test_safe_id_rejects_non_strings():
    with pytest.raises(ValueError, match="not a safe id"):
        validate.safe_id(None)  # type: ignore[arg-type]


# ---- review_id -------------------------------------------------------------

@pytest.mark.parametrize("s", ["2026-W38", "2026-W01", "2020-W53"])
def test_review_id_accepts(s):
    assert validate.review_id(s) == s


@pytest.mark.parametrize("s", ["", "2026-W00", "2026-W54", "2026-W1",
                               "2026-w38", "2026-W38\n", "/etc/passwd",
                               "../2026-W38", "2026-W38.json", "٢٠٢٦-W38"])
def test_review_id_rejects(s):
    with pytest.raises(ValueError, match="review id"):
        validate.review_id(s)


# ---- confined --------------------------------------------------------------

@pytest.mark.parametrize("rel", ["a.md", "sources/x.md", "a/../b.md",
                                 "./c.md"])
def test_confined_accepts_paths_under_root(tmp_path, rel):
    got = validate.confined(tmp_path, rel)
    assert got == (tmp_path / rel).resolve()
    assert got.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("rel", ["../x", "../../project/README.md",
                                 "sources/../../x", "/etc/passwd", "", ".",
                                 "a/.."])
def test_confined_rejects_escapes(tmp_path, rel):
    with pytest.raises(ValueError, match="path"):
        validate.confined(tmp_path / "wiki", rel)


def test_confined_rejects_a_symlink_out_of_the_tree(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "out").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        validate.confined(root, "out/secret.json")


# ---- instant ---------------------------------------------------------------

@pytest.mark.parametrize("s", ["2026-09-23T10:00:00Z", "2024-02-29T23:59:59Z",
                               "2026-01-01T00:00:00Z"])
def test_instant_accepts(s):
    assert validate.instant(s) == s
    assert validate.is_instant(s)


@pytest.mark.parametrize("s", [
    "2026-02-30T10:00:00Z",       # no such day
    "2025-02-29T10:00:00Z",       # not a leap year
    "2026-13-01T10:00:00Z", "2026-09-23T24:00:00Z", "2026-09-23T10:60:00Z",
    "2026-09-23T10:00:60Z",       # strptime's %S takes 60 and 61
    "2026-09-23T10:00:00", "2026-09-23T10:00:00+00:00",
    "2026-09-23T10:00:00.5Z", "2026-09-23 10:00:00Z", "2026-09-23",
    "2026-09-23T10:00:00Z\n", " 2026-09-23T10:00:00Z", "",
    "٢٠٢٦-09-23T10:00:00Z",       # non-ASCII digits
])
def test_instant_rejects(s):
    with pytest.raises(ValueError, match="instant"):
        validate.instant(s)
    assert not validate.is_instant(s)


# ---- one_line --------------------------------------------------------------

@pytest.mark.parametrize("s", ["", "plain", "tabs\tare fine", "  spaced  ",
                               "pipes | and — dashes"])
def test_one_line_accepts(s):
    assert validate.one_line(s) == s


@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", "\v", "\f", "\x1c",
                                 "\x1d", "\x1e", "\x85", " ", " "])
@pytest.mark.parametrize("where", ["mid", "trailing", "leading", "alone"])
def test_one_line_rejects_every_splitlines_break(brk, where):
    s = {"mid": f"a{brk}b", "trailing": f"a{brk}", "leading": f"{brk}a",
         "alone": brk}[where]
    with pytest.raises(ValueError, match="summary: must be one line"):
        validate.one_line(s, "summary")


def test_one_line_rejects_a_frontmatter_break_out():
    with pytest.raises(ValueError, match="one line"):
        validate.one_line("fine\n---\nstatus: resolved")
