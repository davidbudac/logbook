"""Who gets one week's review, in what form, and over which channel.

Every decision is made in `decide` before a sink touches anything. `decide` is
pure over the published review and the resolved policy, so what a run will
send can be printed, diffed and tested without a relay. A sink is only a
transport: it gets a rendered subject and body and either returns or raises.

The audit is the source of truth about what was sent, not a sink. `dispatch`
skips every key `Audit.delivered_keys` already holds, so a retry never sends a
second copy, and a dead relay is a counted `failed` row rather than a failed
run. `dbwiki review --deliver-only` is the retry.

`Audit` is a Protocol rather than a call into `review.py` because the
dependency runs the other way. `review.py` owns the audit vocabulary, the log
path and the published file, and it passes an implementation in. This module
never imports it and does not know where a row lands.

The yaml is one top-level `delivery:` block. `Policy.resolve` is the only
place it becomes typed, and it refuses there rather than degrading:

    delivery:
      enabled: true
      external_enabled: false
      smtp_host: relay.example.com
      smtp_port: 25
      starttls: false
      from_address: dbwiki@example.com
      subject_prefix: "[dbwiki] "
      timeout_s: 10.0
      portal_base: https://wiki.example.com/portal/
      recipients:
        - id: ops
          address: ops@example.com
          channels: [inbox, email]
          content_class: internal_full
          role: viewer

A recipient must name its `id`, its `channels` and, on the email channel, its
`address`. `content_class` defaults to `internal_full` and `role` to `viewer`.
"""

import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote

from . import health


class Channel(StrEnum):
    INBOX = "inbox"
    EMAIL = "email"


#: The two outcomes an attempt can have. The inbox page pins its glyph table
#: against this the way it pins movements against `review.MOVEMENTS`.
STATUSES = ("delivered", "failed")

DELIVERED, FAILED = STATUSES

DEFAULT_CONTENT_CLASS = "internal_full"

#: The category a relay failure gets, and what counts as one. The arm lives
#: here rather than in `health.categorize` because an `OSError` arm in the
#: shared categorizer would claim every other stage's file errors; inside
#: `dispatch`'s send the only code is a renderer and the SMTP client, so here
#: these two *are* the transport.
SMTP_FAILED = "smtp_failed"

_TRANSPORT_ERRORS = (smtplib.SMTPException, OSError)


@dataclass(frozen=True)
class Rendered:
    """One review as one recipient sees it."""

    subject: str
    body: str


@dataclass(frozen=True)
class ContentClass:
    """One row of the rendering authority: how much of a review a recipient in
    this class is shown, and the function that writes it."""

    id: str
    label: str
    description: str
    render: Callable[[Mapping, "Policy"], Rendered]


@dataclass(frozen=True)
class Recipient:
    """One configured destination. `channels` names every channel this
    recipient wants, and nothing else decides that."""

    id: str
    address: str
    channels: tuple[str, ...]
    content_class: str
    #: a `portal.identity.Role` value, validated at resolve
    role: str


@dataclass(frozen=True)
class Policy:
    """The one boundary where `delivery:` yaml becomes typed decisions."""

    enabled: bool = True
    external_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 25
    starttls: bool = False
    from_address: str = ""
    subject_prefix: str = "[dbwiki] "
    timeout_s: float = 10.0
    portal_base: str = ""
    recipients: tuple[Recipient, ...] = ()

    @staticmethod
    def resolve(cfg) -> "Policy":
        """`cfg.delivery` as a typed policy, refusing at resolve and naming the
        key: an unknown content class, channel or role, a recipient without an
        id or with one another recipient already has, an email recipient
        without an address, and external delivery with nothing to send
        through. A refusal fails the stage's start, so a misconfiguration is
        never a review that quietly reaches nobody."""
        from .portal.identity import Role

        raw = cfg.delivery
        base = Policy()
        recipients: list[Recipient] = []
        seen: set[str] = set()
        for index, row in enumerate(raw.get("recipients") or ()):
            row = dict(row or {})
            rid = str(row.get("id") or "")
            if not rid:
                raise ValueError(f"delivery.recipients[{index}].id: a "
                                 f"recipient needs an id")
            if rid in seen:
                raise ValueError(f"delivery.recipients[{rid}].id: {rid!r} is "
                                 f"already a recipient")
            if rid == Channel.INBOX:
                # `delivery_key(review, "inbox")` is the inbox row's key,
                # which always delivers first: this recipient's email would
                # count as sent and never go, nor ever be retried
                raise ValueError(f"delivery.recipients[{rid}].id: {rid!r} is "
                                 f"reserved for the inbox row; pick another "
                                 f"id")
            seen.add(rid)
            channels = tuple(str(c) for c in (row.get("channels") or ()))
            if not channels:
                raise ValueError(f"delivery.recipients[{rid}].channels: a "
                                 f"recipient needs at least one channel")
            for channel in channels:
                if channel not in tuple(Channel):
                    raise ValueError(
                        f"delivery.recipients[{rid}].channels: {channel!r} is "
                        f"not a channel; the channels are "
                        f"{_names(tuple(Channel))}")
            address = str(row.get("address") or "")
            if Channel.EMAIL in channels and not address:
                raise ValueError(f"delivery.recipients[{rid}].address: a "
                                 f"recipient on the email channel needs an "
                                 f"address")
            content_class = str(row.get("content_class")
                                or DEFAULT_CONTENT_CLASS)
            _class_row(content_class,
                       f"delivery.recipients[{rid}].content_class")
            role = str(row.get("role") or Role.VIEWER)
            if role not in tuple(Role):
                raise ValueError(f"delivery.recipients[{rid}].role: {role!r} "
                                 f"is not a role; the roles are "
                                 f"{_names(tuple(Role))}")
            recipients.append(Recipient(id=rid, address=address,
                                        channels=channels,
                                        content_class=content_class,
                                        role=role))
        external = bool(raw.get("external_enabled", base.external_enabled))
        smtp_host = str(raw.get("smtp_host", base.smtp_host))
        from_address = str(raw.get("from_address", base.from_address))
        if external and not smtp_host:
            raise ValueError("delivery.smtp_host: external_enabled is true "
                             "and no host is set")
        if external and not from_address:
            raise ValueError("delivery.from_address: external_enabled is true "
                             "and no address is set")
        if external and not any(Channel.EMAIL in r.channels
                                for r in recipients):
            raise ValueError("delivery.recipients: external_enabled is true "
                             "and no recipient names the email channel")
        return Policy(
            enabled=bool(raw.get("enabled", base.enabled)),
            external_enabled=external, smtp_host=smtp_host,
            smtp_port=int(raw.get("smtp_port", base.smtp_port)),
            starttls=bool(raw.get("starttls", base.starttls)),
            from_address=from_address,
            subject_prefix=str(raw.get("subject_prefix", base.subject_prefix)),
            timeout_s=float(raw.get("timeout_s", base.timeout_s)),
            portal_base=str(raw.get("portal_base", base.portal_base)),
            recipients=tuple(recipients))


@dataclass(frozen=True)
class Delivery:
    """One review to one recipient over one channel. `key` is what makes a
    resend idempotent, so it is spelled once, in `delivery_key`."""

    key: str
    review_id: str
    channel: str
    recipient: str
    address: str
    content_class: str


@dataclass(frozen=True)
class Attempt:
    """What happened when one `Delivery` was tried. `error` is a
    `health.categorize` category and `detail` the transport's own words,
    capped: an audit row is evidence, not a log sink."""

    delivery: Delivery
    status: str
    at: str
    error: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.delivery.key, "channel": self.delivery.channel,
                "recipient": self.delivery.recipient,
                "content_class": self.delivery.content_class,
                "status": self.status, "at": self.at, "error": self.error,
                "detail": self.detail}


class Audit(Protocol):
    """What `dispatch` needs from the trail: which keys are already delivered,
    and somewhere to put each attempt."""

    def delivered_keys(self, review_id: str) -> frozenset[str]: ...

    def record(self, attempt: Attempt) -> None: ...


MAX_DETAIL = 200


def _names(values) -> str:
    return ", ".join(str(v) for v in values)


def delivery_key(review_id: str, recipient: str) -> str:
    """`review_id|recipient` — the identity a resend is checked against."""
    return f"{review_id}|{recipient}"


def _review_url(policy: Policy, review_id: str) -> str:
    """The portal's page for one review, or "" when no base is configured.
    The only place a URL is built, so a review reaches an inbox with no link
    at all rather than a link to nowhere."""
    if not policy.portal_base or not review_id:
        return ""
    return (policy.portal_base.rstrip("/")
            + "/#/review/" + quote(review_id, safe=""))


def _finding_line(finding: Mapping) -> str:
    return (f"{str(finding.get('severity') or ''):6} "
            f"{finding.get('kind')}/{finding.get('code')}  "
            f"{finding.get('db')}  {finding.get('slug')} — "
            f"{finding.get('explanation')}")


def _render_internal_full(review: Mapping, policy: Policy) -> Rendered:
    review_id = str(review.get("review_id") or "")
    findings = list(review.get("findings") or ())
    lines = [str(review.get("explanation") or "")]
    synthesis = review.get("synthesis") or None
    if synthesis:
        lines += ["", str(synthesis.get("summary") or "")]
    elif review.get("synthesis_error"):
        lines += ["", f"No covering note this week, the synthesis failed "
                      f"({review['synthesis_error']})."]
    lines.append("")
    lines += [_finding_line(f) for f in findings] or ["nothing selected"]
    url = _review_url(policy, review_id)
    if url:
        lines += ["", url]
    return Rendered(
        subject=(f"{policy.subject_prefix}Weekly review {review_id}: "
                 f"{len(findings)} to look at"),
        body="\n".join(lines) + "\n")


#: content class id -> how much of a review it shows. One row today. A class
#: that redacts for an external audience is a second row here and a branch
#: nowhere else.
CONTENT_CLASSES: Mapping[str, ContentClass] = {
    "internal_full": ContentClass(
        id="internal_full", label="the whole review",
        description="everything the published file carries, for an operator "
                    "who may already read the wiki",
        render=_render_internal_full),
}


def _class_row(content_class: str, key: str) -> ContentClass:
    row = CONTENT_CLASSES.get(content_class)
    if row is None:
        raise ValueError(f"{key}: {content_class!r} is not a content class; "
                         f"the classes are {_names(CONTENT_CLASSES)}")
    return row


def render(review: Mapping, policy: Policy, content_class: str) -> Rendered:
    """One review as its content class writes it."""
    return _class_row(content_class, "delivery content_class").render(review,
                                                                     policy)


def decide(review: Mapping, policy: Policy, *,
           now: str) -> tuple[Delivery, ...]:
    """Everything this review should reach, inbox first. Pure: no I/O, no
    clock, no transport. The inbox row is unconditional because the review is
    already published and the inbox screen reads it; email rows exist only
    when `external_enabled` is set and a recipient names the channel."""
    review_id = str((review or {}).get("review_id") or "")
    if not policy.enabled or not review_id:
        return ()
    plan = [Delivery(key=delivery_key(review_id, str(Channel.INBOX)),
                     review_id=review_id, channel=str(Channel.INBOX),
                     recipient=str(Channel.INBOX), address="",
                     content_class=DEFAULT_CONTENT_CLASS)]
    if policy.external_enabled:
        for r in policy.recipients:
            if Channel.EMAIL not in r.channels:
                continue
            _class_row(r.content_class,
                       f"delivery.recipients[{r.id}].content_class")
            plan.append(Delivery(key=delivery_key(review_id, r.id),
                                 review_id=review_id,
                                 channel=str(Channel.EMAIL), recipient=r.id,
                                 address=r.address,
                                 content_class=r.content_class))
    return tuple(plan)


Smtp = Callable[[Policy], AbstractContextManager]


def _open_smtp(policy: Policy) -> AbstractContextManager:
    server = smtplib.SMTP(policy.smtp_host, policy.smtp_port,
                          timeout=policy.timeout_s)
    if policy.starttls:
        # verify the relay's certificate and name: a bare `starttls()`
        # encrypts to whoever answers
        server.starttls(context=ssl.create_default_context())
    return server


def _send_inbox(delivery: Delivery, rendered: Rendered, policy: Policy,
                smtp: Smtp) -> None:
    """A no-op that records `delivered`. The review is already in
    `.state/review/reviews/` and the inbox screen reads it from there, so the
    row states a fact rather than doing work, and the inbox counts the same
    way every other channel does."""


def _send_email(delivery: Delivery, rendered: Rendered, policy: Policy,
                smtp: Smtp) -> None:
    """One plain-text message through the injected factory. No retries, the
    stance `alerts.WebhookSink` takes: `dispatch` already counts the failure
    and `--deliver-only` resends only what is missing."""
    message = EmailMessage()
    message["From"] = policy.from_address
    message["To"] = delivery.address
    message["Subject"] = rendered.subject
    message.set_content(rendered.body)
    with smtp(policy) as server:
        server.send_message(message)


#: channel -> the sink that carries it. A third channel is a row here.
SINKS: Mapping[str, Callable[[Delivery, Rendered, Policy, Smtp], None]] = {
    str(Channel.INBOX): _send_inbox,
    str(Channel.EMAIL): _send_email,
}


def dispatch(review: Mapping, policy: Policy, audit: Audit, *, now: str,
             smtp: Smtp | None = None) -> tuple[Attempt, ...]:
    """Send what `decide` planned and is not already delivered, one audit row
    per attempt.

    Never raises. A render or transport failure becomes a `failed` attempt
    carrying `SMTP_FAILED` for a transport error and its `health.categorize`
    category otherwise, because a relay nobody can reach must not cost the
    review that was already published."""
    plan = decide(review, policy, now=now)
    if not plan:
        return ()
    already = audit.delivered_keys(plan[0].review_id)
    attempts: list[Attempt] = []
    for delivery in plan:
        if delivery.key in already:
            continue
        try:
            rendered = render(review, policy, delivery.content_class)
            SINKS[delivery.channel](delivery, rendered, policy,
                                    smtp or _open_smtp)
            attempt = Attempt(delivery, DELIVERED, now)
        except Exception as exc:  # noqa: BLE001 — an unreachable relay is a failed attempt
            category = (SMTP_FAILED if isinstance(exc, _TRANSPORT_ERRORS)
                        else health.categorize(exc))
            attempt = Attempt(delivery, FAILED, now, category,
                              str(exc)[:MAX_DETAIL])
        audit.record(attempt)
        attempts.append(attempt)
    return tuple(attempts)


def merge(rows: Sequence[Mapping], attempts: Sequence[Attempt]) -> list[dict]:
    """The review file's `deliveries` list with these attempts folded in, last
    attempt per key winning and sorted by key. A retried key's `failed` row is
    replaced rather than appended, so the list stays one row per key and the
    published bytes stay stable."""
    by_key = {str(row.get("key") or ""): dict(row) for row in rows or ()}
    for attempt in attempts:
        by_key[attempt.delivery.key] = attempt.to_dict()
    return [by_key[key] for key in sorted(by_key)]
