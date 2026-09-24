# ADR-0004 — Deliver the weekly review, by email only when an operator asks for it

Date: 2026-09-01
Status: accepted

## Context

Phase 3.3 publishes one review document per ISO week under
`.state/review/reviews/<id>.json`. Nothing carries it anywhere.
`review._deliver` returns an empty tuple, the published document carries
`"deliveries": []`, and `review.AUDIT_EVENTS` already spells `delivered` and
`delivery_failed` for rows nothing writes. The vocabulary is in place and the
work behind it is not.

The gap that leaves is real. The review is an unattended cron product. Its
one reader today is the inbox screen, which is served by a portal bound to
`127.0.0.1`. An operator who does not open that browser in a given week
never learns the review happened, and the finding it selected ages another
seven days.

`alerts.py` already met this question and answered it no. That answer is not
an omission. It is four sentences of written decision, and this ADR has to
say which of them it reverses and which it keeps.

| Claim | Where it is written | Verdict here |
|---|---|---|
| "Email/webhook/desktop transport is out of scope by design" | `src/dbwiki/alerts.py`, lines 331-332 (`Sink`) | reversed, narrowly |
| "The receiving end decides what an alert means; this only delivers the same dict every other sink gets" | `src/dbwiki/alerts.py`, lines 364-365 (`WebhookSink`) | upheld |
| "No retries by design" | `src/dbwiki/alerts.py`, line 361 (`WebhookSink`) | upheld for sinks |
| "a transport that is nobody's source of truth" | `src/dbwiki/alerts.py`, lines 363-364 (`WebhookSink`) | narrowed |

**The reversal is narrow.** It covers advisory review content and nothing
else. Transport is stdlib `smtplib` plus `email.message.EmailMessage`, no new
dependency, and it lives behind `delivery.py` where the review is the only
caller. External delivery is off by default. Pipeline alerts still never
email: nothing in this phase registers an `EmailSink` in `alerts._sink`, and
`alerts.py` is not in the phase's diff. A review is a weekly, bounded,
already-published document an operator asked to be told about. An alert is a
per-tick health signal whose transport failures would ride the same cron run
that reported the failure. They are different things, and only the first one
gets a relay.

**Transport stays dumb.** The recipient, the content class and the channel
are all decided in `delivery.decide` before any sink is constructed. A sink
receives a rendered message and an address, and it has no opinion about
either. The `Sink` protocol's one method
(`src/dbwiki/alerts.py`, line 334) is the shape this follows.

**Sinks still never retry.** A `send` that fails raises once and is done.
Redelivery moved one layer up, where it is idempotent instead of a loop:
`dispatch` skips every key the audit already holds a `delivered` row for, and
`dbwiki review --deliver-only` is the retry an operator runs. A cron run is
never delayed by a relay that is down.

**A sink is still nobody's source of truth, and the audit is.** The narrowing
is on the second half of the sentence, not the first. `.state/review/log.jsonl`
is the record of what was sent where, and it is authoritative about that.
It is not authoritative about the review, which is the published document, and
it is not authoritative about the estate, which is the wiki.

## Decision

A new module, `src/dbwiki/delivery.py`, owns policy, rendering, transport and
the audit for the weekly review. Nothing else calls it.

### Channels and content classes

```python
class Channel(StrEnum):
    INBOX = "inbox"
    EMAIL = "email"
```

`CONTENT_CLASSES` is a registry of `ContentClass` rows and ships exactly one,
`internal_full`, which renders the whole selection for a reader already
trusted with the wiki. A second class is a later row in that table rather than
a branch anywhere: an external summary that redacts to ADR-0002's rules is the
obvious next one, and it costs a row and its renderer.

`Recipient(id, address, channels, content_class, role)` is the configured
unit. `role` is a `portal.identity.Role` and has no teeth until Stage 4. It is
declared now so the seam exists where the recipient does, and fixtures
exercise it.

### `decide` is pure

`decide(review, policy, *, now)` takes a published review and a resolved
policy and returns the deliveries. No I/O, no clock, no sink.

- The inbox delivery is always present and always first.
- Then one email delivery per recipient naming the email channel, in
  configured order, and only when `external_enabled` is true.
- `enabled: false` yields no deliveries at all, inbox included.

The inbox delivery does no work. The review is already on disk and the inbox
screen reads it from there. The row states that fact rather than performing
it, which is the point: one audit vocabulary then covers both channels, and
"was this review delivered, and where" is one question with one answer instead
of two half-answers in different files.

### `dispatch` is idempotent and never raises

The idempotency key is `(review_id, recipient)`, spelled
`f"{review_id}|{recipient_id}"`. `dispatch` reads the audit, skips every key
that already holds a `delivered` row, renders per the content class row, sends
through the channel's sink, and appends exactly one audit row per attempt:
`delivered` or `delivery_failed`. A failure is categorized through
`health.categorize`, the same vocabulary every other stage's failures use.

`dispatch` never raises. A dead relay is a counted `failed` attempt on a
review that was published anyway, and `dbwiki review --deliver-only` resends
exactly the missing keys later. Losing the review because a mail server was
down would be the same wrong trade `review.run` already refuses for a failed
synthesis.

### The `delivery:` config block

A top-level block, read by `Policy.resolve(cfg)`. Every key has a default and
the whole block may be absent.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` delivers nothing at all |
| `external_enabled` | `false` | the switch that lets anything leave the box |
| `smtp_host` | `""` | the relay |
| `smtp_port` | `25` | |
| `starttls` | `false` | |
| `from_address` | `""` | the envelope sender |
| `subject_prefix` | `"[dbwiki] "` | |
| `timeout_s` | `10.0` | per send |
| `portal_base` | `""` | the base URL a body links back to |
| `recipients` | `[]` | the configured `Recipient` rows |

`resolve` refuses at read time and names the exact key, which is
`alerts.make_sink`'s stance that silently degrading delivery leaves an
operator who configured it with none and never told. It refuses an unknown
`content_class`, an unknown channel, an unknown role, a missing or duplicate
recipient `id`, an empty `address` on a recipient naming the email channel,
and `external_enabled: true` with an empty `smtp_host`, with an empty
`from_address`, or with no recipient naming the email channel at all.

`external_enabled` defaults false because the review carries incident titles,
database names, error codes and page paths. That is estate-identifying data
by ADR-0002's definition, and the default for sending it off the box is no.
Turning it on is an operator's deliberate act with an address they chose.

`portal_base` defaults empty because the portal binds loopback. A link into
`127.0.0.1` inside an email is a false promise: it resolves to the reader's
own machine, not to the box that published the review. A body rendered with
no `portal_base` carries no URL at all rather than a broken one.

### Transport

Stdlib `smtplib` and `email.message.EmailMessage`, injected as a factory. No
test opens a socket, and no dependency is added. The audit rows land in
`.state/review/log.jsonl` through `review._audit`, whose `AUDIT_EVENTS` set
already holds both names.

## Consequences

**`alerts.py` is untouched, and a test says so.** The pipeline's alert
transports keep the behavior and the reasoning they have. The phase's diff
must not contain `src/dbwiki/alerts.py`, which a test asserts, so the module
cannot drift into this feature by accident. That is also why the pointer to
this ADR is this text rather than a new docstring line in `alerts.py`: an
edit that only adds a cross-reference would still be an edit, and the guard
does not read intent.

**The review gains a second reason to be trusted, and a second thing to get
wrong.** An operator can now be told about a review they did not go looking
for. In exchange, the estate has one more place data leaves from, gated by
one boolean and a resolve-time refusal. The gate is the whole safety story
and it is deliberately small enough to read.

**Redelivery has a name.** `dbwiki review --deliver-only` is a supported
operation rather than a rerun with `--force`, which would republish the
document. It re-reads the existing review, dispatches the keys the audit is
missing, and writes nothing else.

**The audit answers a question the wiki cannot.** "Who was told about
2026-W36" is now readable from `.state/review/log.jsonl`. It is not in git
and it is not durable in the wiki's sense, which is consistent with the
review itself (Q18): an operator who needs a delivery on the record records
an action citing the `review_id`.

## Rollout

1. **Phase 3.4:** this ADR becomes accepted. `delivery.py` with `Channel`,
   `CONTENT_CLASSES`, `Recipient`, `Policy.resolve`, `decide` and `dispatch`;
   the `delivery:` config block; `review.run` calling `dispatch` in place of
   the stub; `dbwiki review --deliver-only`; the delivery rows on the review
   envelope and the review screen.
2. **Deferred:** a second content class, an external summary redacted to
   ADR-0002's rules, which is one row in `CONTENT_CLASSES` and its renderer.
3. **Deferred to Stage 4:** `Recipient.role` gaining teeth, so what a
   recipient may be sent follows from who they are.
