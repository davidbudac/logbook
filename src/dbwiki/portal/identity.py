"""Who is at the browser, and what they may do.

Stage 1 has one implementation, `TrustedOperator`: the single operator the
localhost bind implies, resolved once through `transaction.resolve_actor`
(the CLI's seam, so the portal and the CLI attribute to the same person by
the same precedence) and granted every role. Stage 4 adds a second `Provider`
that reads a reverse-proxy header and maps roles from config; `api.py` does
not change, because it only ever sees a `Principal`.

Roles are named per command in `ROLE_OF` and nowhere else. The gate runs in
`api.Workbench._built`, for preview and commit alike, so a form the operator
may not submit is also a form they cannot rehearse: a preview is a
`git worktree add` plus a whole-wiki lint, and an ungated one is a compute
amplifier behind whatever Stage 4's weakest identity turns out to be.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .. import lifecycle, transaction
from ..transaction import Actor


class Role(StrEnum):
    VIEWER = "viewer"        # read the queue and incidents, see the page
    OPERATOR = "operator"    # record actions, monitor, reopen, run a tool
    CLOSER = "closer"        # resolve, merge


@dataclass(frozen=True)
class Principal:
    """An identified caller. `actor` is what reaches `incident_action.decode`,
    the `Actor:` trailer and the run-health audit; `roles` is what `require`
    checks. Frozen, so a request cannot escalate itself; and the actor never
    rides on the wire, so a body asserting one is refused."""

    actor: Actor
    roles: frozenset[Role]


@dataclass(frozen=True)
class RequestContext:
    """What a provider may look at to decide who is calling. Header keys are
    lower-cased. Stage 1's provider ignores all of it; Stage 4's reads a
    header. Carrying it from day one is what makes the swap one file."""

    remote: str                     # peer address, "127.0.0.1"
    headers: Mapping[str, str]
    request_id: str


class Provider(Protocol):
    def identify(self, ctx: RequestContext) -> Principal:
        """The caller, or raise `Unidentified`. Called once per request by the
        server before routing. Must be thread-safe: the server calls it from
        every handler thread."""


class Unidentified(PermissionError):
    """No principal for this request -> 401. Never raised by
    `TrustedOperator`.

    Carries `error` in `api.ApiError`'s shape, so the server renders every
    refusal it catches through one line that reads `error`, the message and
    `extra` off the exception, instead of a branch per exception type."""

    error = "unidentified"


class Forbidden(PermissionError):
    """The principal lacks `role` -> 403.

    Shaped like `api.ApiError` for the same reason `Unidentified` is, and
    `extra` is what puts the missing role in the body: the page says which
    role this act needs and which ones the caller holds, rather than showing
    a bare 403 the operator can do nothing with."""

    error = "forbidden"

    def __init__(self, role: Role, principal: Principal):
        self.role = role
        self.principal = principal
        super().__init__(f"{principal.actor.email} is not a {role}")

    @property
    def extra(self) -> dict:
        return {"required": str(self.role),
                "held": sorted(str(role) for role in self.principal.roles)}


class TrustedOperator:
    """Stage 1's provider: one actor, every role, for every request.

    Resolved at construction rather than per request. The identity of a
    trusted single operator does not change between requests, and resolving
    it at startup is what lets `dbwiki portal serve` refuse to start when no
    actor can be attributed, instead of serving a read-only page that fails
    on the first commit."""

    def __init__(self, principal: Principal):
        self._principal = principal

    @classmethod
    def from_config(cls, cfg) -> "TrustedOperator":
        """`transaction.resolve_actor(cfg.wiki_repo,
        configured=cfg.portal["operator_email"])` with all three roles.
        Raises with `resolve_actor`'s message when nothing resolves.

        The `RuntimeError` propagates unwrapped: `server.serve` prints it and
        exits 2, and a second sentence of our own would only bury the one
        that names the three ways to set an actor."""
        actor = transaction.resolve_actor(
            cfg.wiki_repo, configured=cfg.portal["operator_email"])
        return cls(Principal(actor, frozenset(Role)))

    def identify(self, ctx: RequestContext) -> Principal:
        return self._principal


#: command type -> the role that may preview and commit it. Every command
#: `lifecycle.KINDS` names has a row, asserted by a test: an unlisted one
#: would `KeyError` out of an incident GET rather than fail closed.
#:
#: The `closer` rows are the commands that reach `resolved`. `Merge` is one
#: of them because it is a second path there, and an operator who could
#: merge but not resolve could close any incident by folding it into
#: another.
ROLE_OF: dict[type, Role] = {
    lifecycle.RecordAction: Role.OPERATOR,
    lifecycle.StartMonitoring: Role.OPERATOR,
    lifecycle.ExtendMonitoring: Role.OPERATOR,
    lifecycle.Reopen: Role.OPERATOR,
    lifecycle.Resolve: Role.CLOSER,
    lifecycle.Merge: Role.CLOSER,
}

READ_ROLE = Role.VIEWER
DRAFT_ROLE = Role.OPERATOR


def require(principal: Principal, role: Role) -> None:
    """Raise `Forbidden` unless `role in principal.roles`. Roles are flat,
    not ordered: a closer who is not also an operator cannot record an
    action. `TrustedOperator` grants all three, so the question does not
    arise until Stage 4 maps them from config."""
    if role not in principal.roles:
        raise Forbidden(role, principal)


def permits(principal: Principal, command_type: type) -> bool:
    """Whether this principal may run this command. The incident read model
    reports it per verb as `permitted`, so a verb the caller cannot submit is
    still listed with the role it would need."""
    return ROLE_OF[command_type] in principal.roles
