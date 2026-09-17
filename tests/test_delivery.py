"""Delivery: what a review reaches, in what form, and what a resend costs.

Nothing here opens a socket. `no_real_smtp` is autouse and replaces
`smtplib.SMTP` for the whole module, so a test that forgot to inject a factory
fails loudly instead of dialling a relay; every send goes through the injected
`smtp` factory or the fake it wraps.

`decide` and `merge` are pure, so they are tested as functions over values.
Only `dispatch` gets a fake audit, because idempotency is a claim about the
trail rather than about the plan.
"""

import smtplib
from types import SimpleNamespace

import pytest

from dbwiki import delivery
from dbwiki.delivery import (Attempt, Channel, CONTENT_CLASSES, Delivery,
                             Policy, Recipient, SMTP_FAILED, STATUSES, decide,
                             delivery_key, dispatch, merge, render)

NOW = "2026-09-07T00:00:00Z"
REVIEW_ID = "2026-W37"
PORTAL = "https://wiki.example.com/portal/"


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(delivery, "_open_smtp", refuse)


def review_doc(findings: int = 2, *, synthesis: str = "two databases drift",
               synthesis_error: str = "") -> dict:
    return {
        "review_id": REVIEW_ID,
        "explanation": f"{REVIEW_ID}: {findings} selected",
        "findings": [{"severity": "high", "kind": "stale_open",
                      "code": "stale_open", "db": "cdb1", "slug": f"i-{n}",
                      "explanation": f"i-{n} on cdb1: open and untouched."}
                     for n in range(findings)],
        "synthesis": {"summary": synthesis} if synthesis else None,
        "synthesis_error": synthesis_error,
        "deliveries": [],
    }


def cfg_with(block: dict):
    return SimpleNamespace(delivery=block)


def external(**over) -> Policy:
    block = {"external_enabled": True, "smtp_host": "relay.example.com",
             "from_address": "dbwiki@example.com",
             "recipients": [{"id": "ops", "address": "ops@example.com",
                             "channels": ["inbox", "email"]},
                            {"id": "dba", "address": "dba@example.com",
                             "channels": ["email"], "role": "operator"}],
             **over}
    return Policy.resolve(cfg_with(block))


class FakeSmtp:
    """The injected factory and the connection it hands back, in one object."""

    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.sent: list = []
        self.opens = 0

    def __call__(self, policy):
        self.opens += 1
        return self

    def __enter__(self):
        if self.raises is not None:
            raise self.raises
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, message):
        self.sent.append(message)

    @property
    def recipients(self) -> list[str]:
        return [m["To"] for m in self.sent]


def raiser(exc: Exception):
    def raise_it(*args, **kwargs):
        raise exc

    return raise_it


class FakeAudit:
    """The `Audit` seam, as `review._ReviewAudit` implements it against
    `review/log.jsonl`."""

    def __init__(self):
        self.rows: list[Attempt] = []

    def delivered_keys(self, review_id: str) -> frozenset[str]:
        return frozenset(a.delivery.key for a in self.rows
                         if a.status == "delivered"
                         and a.delivery.review_id == review_id)

    def record(self, attempt: Attempt) -> None:
        self.rows.append(attempt)


def test_the_statuses_are_exactly_the_two():
    assert STATUSES == ("delivered", "failed")


def test_only_one_content_class_ships():
    assert set(CONTENT_CLASSES) == {"internal_full"}


def test_decide_is_config_in_plan_out(tmp_path, monkeypatch):
    """Pure: no clock, no transport, and nothing on disk. The autouse fixture
    already refuses a socket; this pins the filesystem too."""
    monkeypatch.setattr("dbwiki.state.atomic_write_text",
                        lambda *a, **kw: pytest.fail("decide wrote something"))
    plan = decide(review_doc(), external(), now=NOW)
    assert [d.recipient for d in plan] == ["inbox", "ops", "dba"]
    assert plan[0].channel == str(Channel.INBOX)
    assert plan[0].key == delivery_key(REVIEW_ID, "inbox")
    assert [d.address for d in plan[1:]] == ["ops@example.com",
                                             "dba@example.com"]
    assert not list(tmp_path.iterdir())


def test_the_inbox_is_always_first_and_alone_without_external():
    plan = decide(review_doc(), Policy(), now=NOW)
    assert len(plan) == 1
    assert plan[0].channel == str(Channel.INBOX)
    assert plan[0].content_class == "internal_full"


def test_recipients_that_name_no_email_channel_get_no_external_delivery():
    policy = Policy(external_enabled=True, smtp_host="relay",
                    from_address="a@b",
                    recipients=(Recipient(id="ops", address="ops@example.com",
                                          channels=("inbox",),
                                          content_class="internal_full",
                                          role="viewer"),))
    assert [d.channel for d in decide(review_doc(), policy, now=NOW)] \
        == [str(Channel.INBOX)]


def test_delivery_disabled_plans_nothing():
    assert decide(review_doc(), external(enabled=False), now=NOW) == ()


def test_a_review_without_an_id_plans_nothing():
    assert decide({"findings": []}, Policy(), now=NOW) == ()


def test_an_unknown_content_class_names_the_key():
    policy = Policy(external_enabled=True,
                    recipients=(Recipient(id="ops", address="ops@example.com",
                                          channels=("email",),
                                          content_class="external_summary",
                                          role="viewer"),))
    with pytest.raises(ValueError,
                       match=r"delivery\.recipients\[ops\]\.content_class"):
        decide(review_doc(), policy, now=NOW)


def test_the_policy_defaults_when_the_block_is_absent():
    assert Policy.resolve(SimpleNamespace()) == Policy()


def one(**over) -> dict:
    return {"id": "ops", "address": "ops@example.com",
            "channels": ["email"], **over}


REFUSALS = [
    ({"recipients": [one(content_class="external_summary")]},
     r"delivery\.recipients\[ops\]\.content_class"),
    ({"recipients": [one(channels=["pigeon"])]},
     r"delivery\.recipients\[ops\]\.channels"),
    ({"recipients": [one(channels=[])]},
     r"delivery\.recipients\[ops\]\.channels"),
    ({"recipients": [one(role="admin")]},
     r"delivery\.recipients\[ops\]\.role"),
    ({"recipients": [one(id="")]}, r"delivery\.recipients\[0\]\.id"),
    ({"recipients": [one(), one()]}, r"delivery\.recipients\[ops\]\.id"),
    ({"recipients": [one(address="")]},
     r"delivery\.recipients\[ops\]\.address"),
    ({"external_enabled": True, "from_address": "a@b",
      "recipients": [one()]}, r"delivery\.smtp_host"),
    ({"external_enabled": True, "smtp_host": "relay",
      "recipients": [one()]}, r"delivery\.from_address"),
    ({"external_enabled": True, "smtp_host": "relay", "from_address": "a@b",
      "recipients": [one(channels=["inbox"], address="")]},
     r"delivery\.recipients:"),
]


@pytest.mark.parametrize("block,key", REFUSALS,
                         ids=[k for _, k in REFUSALS])
def test_a_refusal_names_the_key(block, key):
    with pytest.raises(ValueError, match=key):
        Policy.resolve(cfg_with(block))


def test_the_content_class_refusal_lists_the_classes():
    with pytest.raises(ValueError) as caught:  # noqa: PT011 — the message is the assertion
        Policy.resolve(cfg_with({"recipients": [one(content_class="x")]}))
    assert str(caught.value) == (
        "delivery.recipients[ops].content_class: 'x' is not a content class; "
        "the classes are internal_full")


def test_the_policy_reads_the_block():
    policy = external(subject_prefix="[wiki] ", smtp_port=2525,
                      starttls=True, timeout_s=2.5, portal_base=PORTAL)
    assert policy.smtp_port == 2525 and policy.starttls is True
    assert policy.timeout_s == 2.5 and policy.portal_base == PORTAL
    assert [r.id for r in policy.recipients] == ["ops", "dba"]
    assert policy.recipients[0].content_class == "internal_full"
    assert policy.recipients[0].role == "viewer"
    assert policy.recipients[1].role == "operator"


def test_a_body_without_a_portal_base_carries_no_url():
    rendered = render(review_doc(), Policy(), "internal_full")
    assert "http" not in rendered.body and "#/" not in rendered.body
    assert rendered.subject == "[dbwiki] Weekly review 2026-W37: 2 to look at"
    assert "two databases drift" in rendered.body
    assert "i-0 on cdb1" in rendered.body and "i-1 on cdb1" in rendered.body


def test_a_body_with_a_portal_base_carries_the_link_once():
    rendered = render(review_doc(), Policy(portal_base=PORTAL),
                      "internal_full")
    url = "https://wiki.example.com/portal/#/review/2026-W37"
    assert rendered.body.count(url) == 1


def test_a_failed_synthesis_says_so_in_the_body():
    rendered = render(review_doc(synthesis="",
                                 synthesis_error="harness_error"),
                      Policy(), "internal_full")
    assert "harness_error" in rendered.body


def test_dispatch_sends_the_plan_and_records_one_row_each():
    audit, smtp = FakeAudit(), FakeSmtp()
    attempts = dispatch(review_doc(), external(), audit, now=NOW, smtp=smtp)
    assert [a.status for a in attempts] == ["delivered"] * 3
    assert len(audit.rows) == 3
    assert smtp.recipients == ["ops@example.com", "dba@example.com"]
    assert all(a.error == "" and a.detail == "" for a in attempts)


def test_a_delivered_key_is_never_sent_twice():
    audit, policy = FakeAudit(), external()
    first = FakeSmtp()
    dispatch(review_doc(), policy, audit, now=NOW, smtp=first)
    second = FakeSmtp()
    again = dispatch(review_doc(), policy, audit, now=NOW, smtp=second)
    assert again == () and second.sent == [] and second.opens == 0
    assert len(audit.rows) == 3


def test_a_dead_relay_is_counted_and_the_retry_sends_only_what_is_missing():
    audit, policy = FakeAudit(), external()
    dead = FakeSmtp(raises=ConnectionRefusedError("connection refused"))
    attempts = dispatch(review_doc(), policy, audit, now=NOW, smtp=dead)
    assert [(a.delivery.recipient, a.status) for a in attempts] == [
        ("inbox", "delivered"), ("ops", "failed"), ("dba", "failed")]
    assert all(a.error == SMTP_FAILED and "connection refused" in a.detail
               for a in attempts if a.status == "failed")

    live = FakeSmtp()
    retry = dispatch(review_doc(), policy, audit, now=NOW, smtp=live)
    assert [a.delivery.recipient for a in retry] == ["ops", "dba"]
    assert [a.status for a in retry] == ["delivered", "delivered"]
    assert live.recipients == ["ops@example.com", "dba@example.com"]


def test_an_smtp_protocol_error_is_a_transport_failure_too():
    audit = FakeAudit()
    refused = FakeSmtp(raises=smtplib.SMTPRecipientsRefused(
        {"ops@example.com": (550, b"no such user")}))
    attempts = dispatch(review_doc(), external(), audit, now=NOW,
                        smtp=refused)
    assert [a.error for a in attempts if a.status == "failed"] == [
        SMTP_FAILED, SMTP_FAILED]


def test_a_non_transport_failure_keeps_the_shared_category(monkeypatch):
    audit = FakeAudit()
    monkeypatch.setattr(delivery, "render", raiser(
        RuntimeError("uncommitted changes")))
    attempts = dispatch(review_doc(), external(), audit, now=NOW,
                        smtp=FakeSmtp())
    assert [a.status for a in attempts] == ["failed"] * 3
    assert {a.error for a in attempts} == {"dirty_tree"}


def test_a_transport_detail_is_capped():
    audit = FakeAudit()
    dead = FakeSmtp(raises=OSError("x" * 900))
    attempts = dispatch(review_doc(), external(), audit, now=NOW, smtp=dead)
    assert all(len(a.detail) <= delivery.MAX_DETAIL for a in attempts)


def test_dispatch_over_a_disabled_policy_touches_nothing():
    audit = FakeAudit()
    assert dispatch(review_doc(), Policy(enabled=False), audit, now=NOW) == ()
    assert audit.rows == []


def sent(recipient: str, status: str) -> Attempt:
    key = delivery_key(REVIEW_ID, recipient)
    return Attempt(Delivery(key=key, review_id=REVIEW_ID, channel="email",
                            recipient=recipient, address=f"{recipient}@x",
                            content_class="internal_full"),
                   status, NOW)


def test_merge_replaces_a_failed_row_with_its_delivered_row():
    rows = merge([], [sent("ops", "failed"), sent("dba", "delivered")])
    assert [r["key"] for r in rows] == [delivery_key(REVIEW_ID, "dba"),
                                        delivery_key(REVIEW_ID, "ops")]
    again = merge(rows, [sent("ops", "delivered")])
    assert len(again) == 2
    assert {r["recipient"]: r["status"] for r in again} == {
        "ops": "delivered", "dba": "delivered"}


def test_merge_sorts_by_key_whatever_order_the_attempts_arrive_in():
    forward = merge([], [sent("a", "delivered"), sent("z", "delivered")])
    backward = merge([], [sent("z", "delivered"), sent("a", "delivered")])
    assert forward == backward
    assert [r["key"] for r in forward] == sorted(r["key"] for r in forward)


def test_an_attempt_row_carries_the_published_shape():
    row = sent("ops", "failed").to_dict()
    assert set(row) == {"key", "channel", "recipient", "content_class",
                        "status", "at", "error", "detail"}
