"""The portal's one piece of mutable server state.

Everything else in the process keeps the Stage 1 rule: a frozen `Workbench`,
every request reading HEAD afresh and holding nothing after it returns. This
module holds a single reference to an immutable value pinned to one revision
and swaps it when HEAD moves, which is what buys a whole-wiki view for about
one git call per request instead of one per page.

The value is any the server can rebuild from a revision and which carries the
revision it was built at: `readmodel.Snapshot`, and the `heat.Heat` window
built beside it. Nothing else about it is ever read here, so one policy serves
both rather than each growing its own copy of the trylock argument.

Read paths never take `lock.single_flight`. That flock self-conflicts
in-process, so a read arriving behind the tick would answer 423 instead of the
page it was asked for. The exclusion here is a plain `threading.Lock` taken
non-blocking, which no request waits on once a value exists.

`get` takes the builder per call rather than holding one from construction, so
`Workbench` can carry a `RevisionCache` on a frozen dataclass through
`field(default_factory=...)` with no `object.__setattr__` in a `__post_init__`.
The cache still owns the lock and the swap and calls `build(head)` itself, so
"the value is pinned at head" stays this module's invariant and not its
caller's.
"""

import threading
from collections.abc import Callable
from typing import Protocol


class Revisioned(Protocol):
    """All the cache asks of what it holds: the revision it was built at.

    A protocol rather than a base class, because `Snapshot` and `Heat` are
    frozen dataclasses belonging to the domain and neither should have to know
    that a portal cache exists in order to be cacheable."""

    revision: str


class RevisionCache[T: Revisioned]:
    """The value for the revision most recently built, and nothing else.

    `get(head, build)` answers the held value when it already carries `head`.
    Otherwise the rebuild is attempted under a non-blocking trylock: the
    winner calls `build(head)` and swaps the reference, while a loser returns
    the value already held rather than queueing behind the build. The
    response's `revision != head` is the stale badge, so a race degrades to
    honesty rather than to latency, and the next request after the winner
    finishes is current again.

    The first call has nothing to serve, so it blocks on the lock and returns
    what the winner swapped in. That is the one place a request waits, and it
    happens once per process.

    A torn value is impossible: every builder reaching this cache reads its
    blobs at the revision it was handed, so a value labelled A holds only A's
    bytes even if HEAD moves while it is being built.

    There is no eviction and no second entry. A rebuild is the same from any
    prior state, which is what lets the whole invalidation question be one
    string comparison. One cache holds one kind of value, and a server wanting
    both the snapshot and the heat window keeps two of these rather than one
    keyed map: two one-slot caches cannot evict each other, and each answers
    its own staleness question.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: T | None = None

    def get(self, head: str, build: Callable[[str], T]) -> T:
        held = self._value
        if held is not None and held.revision == head:
            return held
        if not self._lock.acquire(blocking=held is None):
            return held
        try:
            current = self._value
            if current is None or current.revision != head:
                self._value = build(head)
            return self._value
        finally:
            self._lock.release()
