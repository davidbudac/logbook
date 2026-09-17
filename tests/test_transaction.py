"""The publish transaction: one `Proposal` lands whole or not at all.

Every test runs against a throwaway wiki repo, because the value under test is
the interaction between a proposal, git's index and HEAD, and the lint verdict
computed in a detached candidate worktree — none of which a fake reproduces.
"""

import os
import subprocess

import pytest

from dbwiki import gitutil, transaction
from dbwiki.lock import Held
from dbwiki.transaction import (Actor, BaseMoved, Committed, LintBlocked,
                                LockNotHeld, NothingToDo, Proposal,
                                ProposalError, TreeDirty)

INDEX = "---\ntype: index\n---\n\n# index\n\n- [[log]]\n- [[cdb1]]\n"
LOG = "---\ntype: log\n---\n\n# log\n"
DIGEST = "# cdb1 2026-08-05\n"
ACTOR = Actor("agent@test", "Agent", "flag")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def page(name, body=""):
    return f"---\ntype: database\n---\n\n# {name}\n\n{body}\n"


@pytest.fixture
def wiki(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "index.md").write_text(INDEX)
    (repo / "log.md").write_text(LOG)
    (repo / "digests" / "cdb1").mkdir(parents=True)
    (repo / "digests" / "cdb1" / "2026-08-05.md").write_text(DIGEST)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def held(tmp_path):
    return Held(tmp_path / "state", "test", 0.0)


def proposal(wiki, files, message="publish", trailers=()):
    return Proposal(base=transaction.head(wiki), actor=ACTOR, message=message,
                    files=files, trailers=trailers)


def status(wiki):
    return git(wiki, "status", "--porcelain", "-uall").stdout


def test_tree_reads_the_revision_not_the_working_tree(wiki):
    base = transaction.head(wiki)
    (wiki / "log.md").write_text("scribbled over\n")
    tree = transaction.Tree.at(wiki, base)
    assert tree.read("log.md") == LOG
    assert tree.read("cdb1.md") is None


def test_tree_of_reads_from_the_mapping(wiki):
    tree = transaction.Tree.of({"cdb1.md": "content"}, revision="deadbeef")
    assert tree.revision == "deadbeef"
    assert tree.read("cdb1.md") == "content"
    assert tree.read("log.md") is None


def test_proposal_rejects_what_it_may_not_write(wiki):
    for rel in ("/etc/passwd", "../outside.md", "digests/cdb1/2026-08-05.md",
                ".agent-result.json"):
        with pytest.raises(ProposalError):
            proposal(wiki, {rel: "x"})
    with pytest.raises(ProposalError):
        proposal(wiki, {"cdb1.md": "x"}, message="   ")


def test_proposal_paths_are_sorted(wiki):
    p = proposal(wiki, {"log.md": LOG, "cdb1.md": "x", "a/b.md": "y"})
    assert p.paths == ("a/b.md", "cdb1.md", "log.md")


def test_commit_message_carries_the_actor_then_the_trailers(wiki):
    p = proposal(wiki, {"cdb1.md": "x"}, message="publish cdb1",
                 trailers=(("Run-ID", "3f2a"), ("Kind", "report")))
    assert p.commit_message() == ("publish cdb1\n\n"
                                  "Actor: agent@test\n"
                                  "Run-ID: 3f2a\n"
                                  "Kind: report\n")


def test_actor_rejects_an_email_git_cannot_carry():
    for email in ("nobody", "a<b@test", "a>b@test", "a@test\nActor: b@test"):
        with pytest.raises(ValueError, match="not a usable actor email"):
            Actor(email)


def test_actor_renders_a_git_author():
    assert Actor("a@test", "A Name").git_author() == "A Name <a@test>"
    assert Actor("a@test").git_author() == "a <a@test>"


def test_resolve_actor_prefers_override_then_configured_then_git(wiki):
    both = transaction.resolve_actor(wiki, configured="cfg@test",
                                     override="flag@test")
    assert (both.email, both.source, both.name) == ("flag@test", "flag", "test")
    cfg = transaction.resolve_actor(wiki, configured="cfg@test")
    assert (cfg.email, cfg.source) == ("cfg@test", "config")
    fell_back = transaction.resolve_actor(wiki)
    assert (fell_back.email, fell_back.source) == ("test@test", "git")


def test_resolve_actor_raises_when_no_identity_resolves(wiki):
    git(wiki, "config", "--unset", "user.email")
    # an empty local value shadows the developer's global identity, which
    # `--unset` alone leaves visible to `git config user.email`
    git(wiki, "config", "--local", "user.email", "")
    with pytest.raises(RuntimeError):
        transaction.resolve_actor(wiki)


def test_preview_leaves_the_live_tree_alone_and_names_strays(wiki):
    p = proposal(wiki, {"log.md": LOG + "\nappended\n"})
    pv = transaction.preview(wiki, p)
    assert pv.base == p.base
    assert pv.paths == ("log.md",)
    assert "+appended" in pv.diff
    assert pv.blocked() == ()
    assert pv.strays == ()
    assert transaction.stray_paths(wiki) == ()
    assert (wiki / "log.md").read_text() == LOG

    (wiki / "index.md").write_text("hand-edited\n")
    assert transaction.preview(wiki, p).strays == ("index.md",)


def test_uncommitted_machine_output_satisfies_a_digest_citation(wiki, held):
    (wiki / "digests" / "cdb1" / "2026-08-06.md").write_text("# fresher\n")
    p = proposal(wiki, {"cdb1.md": page("cdb1",
                                        "built from digests/cdb1/2026-08-06.md")})
    assert transaction.preview(wiki, p).findings == ()
    assert isinstance(transaction.commit(wiki, p, lock=held, push=False),
                      Committed)


def test_commit_without_an_active_lock_raises(wiki, held):
    p = proposal(wiki, {"cdb1.md": page("cdb1")})
    held.active = False
    with pytest.raises(LockNotHeld):
        transaction.commit(wiki, p, lock=held, push=False)
    with pytest.raises(LockNotHeld):
        transaction.commit(wiki, p, lock=None, push=False)


def test_commit_refuses_an_unrelated_dirty_file_and_keeps_its_bytes(wiki, held):
    p = proposal(wiki, {"cdb1.md": page("cdb1")})
    scribble = "hand-edited, not ours\n"
    (wiki / "index.md").write_text(scribble)
    assert transaction.commit(wiki, p, lock=held, push=False) == \
        TreeDirty(("index.md",))
    assert (wiki / "index.md").read_text() == scribble
    assert not (wiki / "cdb1.md").exists()
    assert transaction.head(wiki) == p.base


def test_commit_is_nothing_to_do_when_the_bytes_already_match_base(wiki, held):
    p = proposal(wiki, {"log.md": LOG, "index.md": INDEX})
    assert transaction.commit(wiki, p, lock=held, push=False) == \
        NothingToDo(p.base)
    assert transaction.head(wiki) == p.base


def test_commit_refuses_after_an_external_commit_landed(wiki, held):
    p = proposal(wiki, {"cdb1.md": page("cdb1")})
    (wiki / "log.md").write_text(LOG + "\nsomeone else\n")
    git(wiki, "commit", "-am", "external")
    assert transaction.commit(wiki, p, lock=held, push=False) == \
        BaseMoved(p.base, transaction.head(wiki))
    assert not (wiki / "cdb1.md").exists()
    assert transaction.stray_paths(wiki) == ()


def test_commit_refuses_a_page_with_a_broken_wikilink(wiki, held):
    p = proposal(wiki, {"cdb1.md": page("cdb1", "see [[nowhere]]")})
    before = status(wiki)
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert isinstance(out, LintBlocked)
    assert [(f.file, f.rule) for f in out.findings] == [("cdb1.md",
                                                         "wikilink-broken")]
    assert status(wiki) == before
    assert not (wiki / "cdb1.md").exists()
    assert transaction.head(wiki) == p.base


def test_commit_refuses_a_page_whose_bytes_are_not_utf_8(wiki, held):
    """A lone surrogate is how `_read`/`_place` carry a byte that is not
    UTF-8; the candidate lint must refuse it rather than raise through."""
    p = proposal(wiki, {"cdb1.md": page("cdb1", "\udcff")})
    before = status(wiki)
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert isinstance(out, LintBlocked)
    assert [(f.file, f.rule) for f in out.findings] == [("cdb1.md",
                                                         "encoding-invalid")]
    assert status(wiki) == before
    assert not (wiki / "cdb1.md").exists()
    assert transaction.head(wiki) == p.base


def test_commit_adopts_the_debris_of_its_own_interrupted_run(wiki, held):
    content = page("cdb1")
    p = proposal(wiki, {"cdb1.md": content})
    (wiki / "cdb1.md").write_text(content)
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert isinstance(out, Committed)
    assert out.paths == ("cdb1.md",)
    assert transaction.stray_paths(wiki) == ()
    assert git(wiki, "show", "HEAD:cdb1.md").stdout == content


def test_commit_without_push_reports_pushed_false(wiki, held):
    p = proposal(wiki, {"cdb1.md": page("cdb1")})
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert isinstance(out, Committed)
    assert out.pushed is False
    assert "Actor: agent@test" in git(wiki, "log", "-1", "--format=%B").stdout


def test_commit_rolls_back_to_base_when_git_commit_fails(wiki, held,
                                                         monkeypatch):
    real = gitutil.git

    def fail_on_commit(repo, *args, **kwargs):
        if args[:1] == ("commit",):
            raise RuntimeError("injected commit failure")
        return real(repo, *args, **kwargs)

    p = proposal(wiki, {"log.md": LOG + "\nappended\n",
                        "cdb1.md": page("cdb1")})
    monkeypatch.setattr(gitutil, "git", fail_on_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        transaction.commit(wiki, p, lock=held, push=False)
    monkeypatch.undo()
    assert transaction.stray_paths(wiki) == ()
    assert status(wiki) == ""
    assert (wiki / "log.md").read_text() == LOG
    assert not (wiki / "cdb1.md").exists()
    assert transaction.head(wiki) == p.base


def test_commit_leaves_uncommitted_machine_output_where_it_found_it(wiki, held):
    fresh = wiki / "digests" / "cdb1" / "2026-08-06.md"
    fresh.write_text("# fresher\n")
    p = proposal(wiki, {"cdb1.md": page("cdb1")})
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert out.paths == ("cdb1.md",)
    assert status(wiki) == "?? digests/cdb1/2026-08-06.md\n"
    assert git(wiki, "show", "--name-only", "--format=", "HEAD").stdout == \
        "cdb1.md\n"


def test_restore_reverts_a_modified_file_and_deletes_an_added_one(wiki):
    base = transaction.head(wiki)
    (wiki / "log.md").write_text("scribbled over\n")
    (wiki / "notes").mkdir()
    (wiki / "notes" / "cdb1.md").write_text(page("cdb1"))
    transaction.restore(wiki, ["log.md", "notes/cdb1.md"], base)
    assert (wiki / "log.md").read_text() == LOG
    assert not (wiki / "notes").exists()
    assert transaction.stray_paths(wiki) == ()


def test_commit_restores_the_tree_when_the_commit_is_interrupted(wiki, held,
                                                                 monkeypatch):
    """Ctrl-C is not an `Exception`. Under a narrower arm it escaped past the
    restore, leaving the files staged and every later commit refusing on the
    debris of this one."""
    real = gitutil.git

    def interrupt_on_commit(repo, *args, **kwargs):
        if args[:1] == ("commit",):
            raise KeyboardInterrupt
        return real(repo, *args, **kwargs)

    p = proposal(wiki, {"log.md": LOG + "\nappended\n",
                        "cdb1.md": page("cdb1")})
    monkeypatch.setattr(gitutil, "git", interrupt_on_commit)
    with pytest.raises(KeyboardInterrupt):
        transaction.commit(wiki, p, lock=held, push=False)
    monkeypatch.undo()
    assert status(wiki) == ""
    assert (wiki / "log.md").read_text() == LOG
    assert not (wiki / "cdb1.md").exists()
    assert transaction.head(wiki) == p.base


ACCENTED = "errors/ORA-00600 café.md"
ERROR_PAGE = "---\ntype: error-class\n---\n\n# ORA-00600\n"


def test_a_path_git_has_to_quote_survives_capture_and_commit(wiki, held):
    """`status --porcelain` octal-escapes such a path. Handing the escaped
    spelling on made `capture` read None, so the commit "succeeded" without
    the page."""
    (wiki / "errors").mkdir()
    (wiki / ACCENTED).write_text(ERROR_PAGE)
    assert transaction.stray_paths(wiki) == (ACCENTED,)
    p = transaction.capture(wiki, transaction.head(wiki), ACTOR, "publish")
    assert p.files == {ACCENTED: ERROR_PAGE}
    assert isinstance(transaction.commit(wiki, p, lock=held, push=False),
                      Committed)
    assert git(wiki, "show", f"HEAD:{ACCENTED}").stdout == ERROR_PAGE
    assert transaction.stray_paths(wiki) == ()


def test_a_rename_captures_as_the_deletion_and_the_addition(wiki, held):
    """A rename entry names two paths. Reporting only the new one left the
    staged deletion of the old one behind, dirty forever."""
    (wiki / "notes.md").write_text(page("notes"))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "add notes")
    git(wiki, "mv", "notes.md", "cdb1.md")
    assert set(gitutil.changed_paths(wiki)) == {"notes.md", "cdb1.md"}
    p = transaction.capture(wiki, transaction.head(wiki), ACTOR, "rename")
    assert p.files == {"notes.md": None, "cdb1.md": page("notes")}
    assert isinstance(transaction.commit(wiki, p, lock=held, push=False),
                      Committed)
    assert status(wiki) == ""
    assert not (wiki / "notes.md").exists()
    assert git(wiki, "show", "HEAD:cdb1.md").stdout == page("notes")


def test_a_file_that_is_not_utf_8_round_trips_byte_identical(wiki, held):
    """Decoding strictly read such a file as None, which captures as a
    deletion of a file that is really there, so nothing is written and it
    stays a stray for every run after."""
    raw = "ORA-00600 caf\xe9\n".encode("latin-1")
    (wiki / "errors").mkdir()
    (wiki / "errors" / "ORA-00600.trace").write_bytes(raw)
    p = transaction.capture(wiki, transaction.head(wiki), ACTOR, "publish")
    assert isinstance(transaction.commit(wiki, p, lock=held, push=False),
                      Committed)
    assert (wiki / "errors" / "ORA-00600.trace").read_bytes() == raw
    assert transaction.stray_paths(wiki) == ()


def test_capture_refuses_a_stray_that_is_not_a_regular_file(wiki):
    """A symlink read as None, captured as a deletion nothing performs, and
    stayed a stray for every run after."""
    (wiki / "cdb1.md").symlink_to(wiki / "nowhere.md")
    with pytest.raises(ProposalError, match="cdb1.md"):
        transaction.capture(wiki, transaction.head(wiki), ACTOR, "publish")


def test_an_actor_with_no_name_still_commits(wiki, held, monkeypatch):
    """git refuses an empty ident name, so `<email>` alone failed every commit
    from a wiki whose `user.name` is unset."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_COMMITTER_NAME", "committer")
    git(wiki, "config", "--unset", "user.name")
    actor = transaction.resolve_actor(wiki)
    assert actor.name == ""
    p = Proposal(base=transaction.head(wiki), actor=actor, message="publish",
                 files={"cdb1.md": page("cdb1")})
    assert isinstance(transaction.commit(wiki, p, lock=held, push=False),
                      Committed)
    assert git(wiki, "log", "-1", "--format=%an <%ae>").stdout.strip() == \
        "test <test@test>"


LATIN_PAGE = b"---\ntype: error-class\n---\n\n# caf\xe9\n"
SPACED = "errors/ORA-00600 timeout.md"


def test_tree_reads_a_blob_that_is_not_utf_8_the_way_the_working_tree_is_read(wiki):
    (wiki / "errors" / "latin.md").parent.mkdir(exist_ok=True)
    (wiki / "errors" / "latin.md").write_bytes(LATIN_PAGE)
    git(wiki, "add", "errors/latin.md")
    git(wiki, "commit", "-q", "-m", "latin")
    tree = transaction.Tree.at(wiki, transaction.head(wiki))
    assert tree.read("errors/latin.md") == transaction._read(wiki / "errors" / "latin.md")
    assert tree.read("errors/absent.md") is None


def test_every_blob_in_the_tree_reads_the_same_batched_as_singly(wiki):
    """The parity that lets `batch_at` stand in for a read per path, asserted
    over the whole inventory rather than a path somebody remembered."""
    base = transaction.head(wiki)
    paths = gitutil.ls_tree(wiki, base)
    tree = transaction.Tree.at(wiki, base)
    assert paths
    assert gitutil.cat_blobs(wiki, base, paths) == {
        path: tree.read(path).encode(errors="surrogateescape")
        for path in paths}


def test_a_path_absent_at_the_revision_is_absent_from_the_batch(wiki):
    base = transaction.head(wiki)
    wanted = ("log.md", "nowhere.md")
    assert set(gitutil.cat_blobs(wiki, base, wanted)) == {"log.md"}
    batched = transaction.Tree.batch_at(wiki, base, wanted)
    assert batched.read("nowhere.md") is None
    assert transaction.Tree.at(wiki, base).read("nowhere.md") is None


def test_a_batched_blob_that_is_not_utf_8_survives_the_round_trip(wiki):
    (wiki / "errors").mkdir()
    (wiki / "errors" / "latin.md").write_bytes(LATIN_PAGE)
    git(wiki, "add", "errors/latin.md")
    git(wiki, "commit", "-q", "-m", "latin")
    base = transaction.head(wiki)
    batched = transaction.Tree.batch_at(wiki, base, ["errors/latin.md"])
    assert batched.read("errors/latin.md") == \
        transaction.Tree.at(wiki, base).read("errors/latin.md")
    assert batched.read("errors/latin.md").encode(
        errors="surrogateescape") == LATIN_PAGE


def test_ls_tree_names_a_path_holding_a_space_unquoted(wiki):
    (wiki / "errors").mkdir()
    (wiki / SPACED).write_text(ERROR_PAGE)
    git(wiki, "add", "-A")
    git(wiki, "commit", "-q", "-m", "spaced")
    assert set(gitutil.ls_tree(wiki, transaction.head(wiki))) == {
        "index.md", "log.md", "digests/cdb1/2026-08-05.md", SPACED}


def publish(wiki, held, message, body=""):
    p = proposal(wiki, {"cdb1.md": page("cdb1", body or message)},
                 message=message)
    out = transaction.commit(wiki, p, lock=held, push=False)
    assert isinstance(out, Committed)
    return out


def test_a_published_transaction_shows_in_the_history_with_its_actor(
        wiki, held, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-08-05T01:30:00-05:00")
    publish(wiki, held, "publish cdb1")
    only, = transaction.page_history(wiki, "cdb1.md")
    assert only.sha == transaction.head(wiki)
    assert only.short == git(wiki, "rev-parse", "--short",
                             "HEAD").stdout.strip()
    assert only.subject == "publish cdb1"
    assert only.author == ACTOR.email
    assert only.actor == ACTOR.email
    assert only.at == "2026-08-05T06:30:00Z"


def test_a_hand_edit_shows_in_the_history_with_no_actor(wiki, monkeypatch):
    """The empty actor is the whole point: it is what distinguishes an
    out-of-band hand edit from a transaction. The date is normalised to UTC,
    so two wikis in different zones render one commit the same way."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-08-05T12:34:56+02:00")
    (wiki / "cdb1.md").write_text(page("cdb1"))
    git(wiki, "add", "cdb1.md")
    git(wiki, "commit", "-m", "hand edit")
    only, = transaction.page_history(wiki, "cdb1.md")
    assert only.actor == ""
    assert only.subject == "hand edit"
    assert only.author == "test@test"
    assert only.at == "2026-08-05T10:34:56Z"


def test_the_history_is_newest_first(wiki, held):
    for message in ("first", "second", "third"):
        publish(wiki, held, message)
    assert [c.subject for c in transaction.page_history(wiki, "cdb1.md")] == \
        ["third", "second", "first"]


def test_the_history_stops_at_the_limit(wiki, held):
    for message in ("first", "second", "third"):
        publish(wiki, held, message)
    assert len(transaction.page_history(wiki, "cdb1.md")) == 3
    capped = transaction.page_history(wiki, "cdb1.md", limit=2)
    assert [c.subject for c in capped] == ["third", "second"]


def test_a_page_with_nothing_committed_has_an_empty_history(wiki, tmp_path):
    """A page the agent created in the working tree, a page that never
    existed, and a repository with no commit at all: the read model answers
    for all three rather than raising."""
    (wiki / "cdb1.md").write_text(page("cdb1"))
    assert transaction.page_history(wiki, "cdb1.md") == ()
    assert transaction.page_history(wiki, "nowhere.md") == ()
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-b", "main")
    assert transaction.page_history(unborn, "index.md") == ()


def test_an_actor_line_that_is_not_a_trailer_leaves_the_actor_empty(wiki):
    subject = "Actor: not-a-trailer@example.com is in the subject"
    (wiki / "cdb1.md").write_text(page("cdb1"))
    git(wiki, "add", "cdb1.md")
    git(wiki, "commit", "-m", f"{subject}\n\nActor: sneaky@example.com\n\n"
        "a closing paragraph, so neither line is a trailer\n")
    only, = transaction.page_history(wiki, "cdb1.md")
    assert only.subject == subject
    assert only.actor == ""
    assert only.author == "test@test"


def test_a_message_with_a_newline_cannot_desynchronise_the_history(wiki, held):
    """`%s` folds the subject onto one line, so a message carrying its own
    newline and a decoy `Actor:` line still parses as one record holding the
    real trailer."""
    publish(wiki, held, "publish cdb1\nActor: decoy@example.com")
    publish(wiki, held, "later")
    history = transaction.page_history(wiki, "cdb1.md")
    assert [c.subject for c in history] == \
        ["later", "publish cdb1 Actor: decoy@example.com"]
    assert [c.actor for c in history] == [ACTOR.email, ACTOR.email]


def test_the_newest_commit_touching_a_path_wins_over_the_older_one(wiki, held):
    publish(wiki, held, "first")
    publish(wiki, held, "second")
    latest = transaction.last_commits(wiki)
    assert latest["cdb1.md"].subject == "second"
    assert latest["cdb1.md"].sha == transaction.head(wiki)
    assert latest["index.md"].subject == "init"


def test_last_commits_carries_the_actor_of_a_transaction_but_not_a_hand_edit(
        wiki, held):
    publish(wiki, held, "publish cdb1")
    (wiki / "log.md").write_text(LOG + "\nby hand\n")
    git(wiki, "add", "log.md")
    git(wiki, "commit", "-m", "hand edit")
    latest = transaction.last_commits(wiki)
    assert latest["cdb1.md"].actor == ACTOR.email
    assert latest["log.md"].actor == ""


def test_a_repository_with_no_commits_has_no_last_commits(tmp_path):
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-b", "main")
    assert transaction.last_commits(unborn) == {}


PENDING_DIGEST = "digests/cdb1/2026-08-30.json"
CITING = page("cdb1", f"The evidence is {PENDING_DIGEST}.")


def test_a_half_written_digest_in_the_overlay_does_not_block_a_preview(wiki):
    """The Stage 1 architecture record's miss 2, reproduced.

    `_candidate` copies the working tree's uncommitted machine output into
    the candidate worktree by reading each file where it lies, so a tick
    writing a digest during a preview really can hand lint a truncated one.
    Lint never reads a digest's bytes, though: `lint_wiki` skips everything
    under `digests/` when it builds its page texts, and `digest-missing` asks
    only whether the path exists. The truncation is invisible and the preview
    is clean, so the false `LintBlocked` the record carried as a risk cannot
    happen through content.
    """
    (wiki / PENDING_DIGEST).write_text('{"day": "2026-08-30", "gro')
    pv = transaction.preview(wiki, proposal(wiki, {"cdb1.md": CITING}))
    assert pv.findings == ()
    assert pv.blocked() == ()
    assert pv.diff


def test_a_digest_the_overlay_snapshot_misses_blocks_that_preview_alone(
        wiki, monkeypatch):
    """What survives of that risk, and why nothing retries for it.

    The overlay is built from one `changed_paths` listing, so a digest the
    compactor creates after the listing and before the read is absent from
    the candidate worktree, and the citation of it reads as `digest-missing`
    against the citing page rather than against a machine path. It is
    transient: the same proposal previews clean once the listing sees the
    file, and `commit` runs its own lint in its own candidate, so a stale
    block never becomes a wrong write.
    """
    (wiki / PENDING_DIGEST).write_text('{"day": "2026-08-30"}')
    pending = proposal(wiki, {"cdb1.md": CITING})
    listing = gitutil.changed_paths
    monkeypatch.setattr(gitutil, "changed_paths",
                        lambda repo: tuple(p for p in listing(repo)
                                           if not p.startswith("digests/")))

    blocked = transaction.preview(wiki, pending)
    assert [f.rule for f in blocked.blocked()] == ["digest-missing"]
    assert PENDING_DIGEST in blocked.findings[0].message
    assert blocked.findings[0].file == "cdb1.md"

    monkeypatch.undo()
    assert transaction.preview(wiki, pending).blocked() == ()


def test_a_commit_relints_rather_than_trusting_the_preview_that_blocked(
        wiki, held, monkeypatch):
    listing = gitutil.changed_paths
    monkeypatch.setattr(gitutil, "changed_paths",
                        lambda repo: tuple(p for p in listing(repo)
                                           if not p.startswith("digests/")))
    (wiki / PENDING_DIGEST).write_text('{"day": "2026-08-30"}')
    pending = proposal(wiki, {"cdb1.md": CITING})
    assert transaction.preview(wiki, pending).blocked()

    monkeypatch.undo()
    assert isinstance(transaction.commit(wiki, pending, lock=held, push=False),
                      Committed)
