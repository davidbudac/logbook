"""`RevisionCache`, the portal's one kind of mutable object.

Its own file rather than an appendix to `test_readmodel.py`: what is under
test here is a concurrency policy, not the read model or the heat window that
the same class holds. Every test drives the cache with a build callable that
counts its calls or blocks on an event, and none of them touches git — a real
`readmodel.build` would make the rebuild race depend on how long a subprocess
takes, which is the one thing a test of this policy must not depend on.
"""

import threading

import pytest

from dbwiki.portal.cache import RevisionCache


class FakeSnapshot:
    """Enough of a `Snapshot` for the cache, which reads `revision` and
    nothing else. Compared by identity on purpose: the tests ask which object
    came back, not whether two are equal."""

    def __init__(self, revision: str):
        self.revision = revision


@pytest.fixture
def builds():
    """A build callable and the list of revisions it was asked for."""
    asked = []

    def build(revision):
        asked.append(revision)
        return FakeSnapshot(revision)

    return build, asked


def test_the_first_build_happens_inline(builds):
    build, asked = builds
    cache = RevisionCache()
    snap = cache.get("a" * 40, build)
    assert snap.revision == "a" * 40
    assert asked == ["a" * 40]


def test_the_same_head_returns_the_same_object(builds):
    build, asked = builds
    cache = RevisionCache()
    first = cache.get("a" * 40, build)
    assert cache.get("a" * 40, build) is first
    assert asked == ["a" * 40]


def test_a_moved_head_builds_again(builds):
    build, asked = builds
    cache = RevisionCache()
    first = cache.get("a" * 40, build)
    second = cache.get("b" * 40, build)
    assert second is not first
    assert second.revision == "b" * 40
    assert asked == ["a" * 40, "b" * 40]


def test_the_race_loser_serves_the_old_snapshot_without_waiting():
    """The winner's build blocks until the test releases it. The loser must
    come back with the previous snapshot while that is still true, which is
    what makes `revision != head` the stale badge rather than a wait."""
    started, release = threading.Event(), threading.Event()

    def build(revision):
        if revision == "b" * 40:
            started.set()
            release.wait(timeout=10)
        return FakeSnapshot(revision)

    cache = RevisionCache()
    old = cache.get("a" * 40, build)

    winner = threading.Thread(target=cache.get, args=("b" * 40, build))
    winner.start()
    assert started.wait(timeout=10)

    assert cache.get("b" * 40, build) is old

    release.set()
    winner.join(timeout=10)
    assert not winner.is_alive()
    assert cache.get("b" * 40, build).revision == "b" * 40


def test_a_first_caller_with_nothing_to_serve_waits_for_the_winner():
    """The one place a request blocks. With no snapshot held there is no
    honest stale answer, so the loser waits and returns what the winner
    swapped in rather than None."""
    started, release = threading.Event(), threading.Event()
    built = []

    def build(revision):
        built.append(revision)
        started.set()
        release.wait(timeout=10)
        return FakeSnapshot(revision)

    cache = RevisionCache()
    answers = []

    def ask():
        answers.append(cache.get("a" * 40, build))

    winner = threading.Thread(target=ask)
    winner.start()
    assert started.wait(timeout=10)
    loser = threading.Thread(target=ask)
    loser.start()

    release.set()
    for thread in (winner, loser):
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert built == ["a" * 40]
    assert len(answers) == 2
    assert answers[0] is answers[1]
