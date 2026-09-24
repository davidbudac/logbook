"""Stale research-exchange claims come back on their own (ADR-0002).

A researcher killed mid-request left its claim in `requests/claimed/` until
a human moved the file back; `dbwiki health` only named it. This is the
analyst queue's reclaim (issue 22) for the exchange, run by the researcher's
claim and by the on-prem research round alike, so it has to be idempotent
and safe when both sides run it. Real git repos in tmp, as in
test_exchange, whose fixtures these are."""

import datetime as dt
import json
import subprocess

from test_exchange import cfg, git, init_repo, req, wiki, xroot  # noqa: F401 — fixtures

from dbwiki import exchange as ex
from dbwiki.lock import Held
from dbwiki.orchestrate import Orchestrator

OLD = "2000-01-01T00:00:00Z"


def age_claim(root, name, claimed_at=OLD, **extra):
    path = root / "requests" / "claimed" / name
    rec = {**json.loads(path.read_text()), "claimed_at": claimed_at, **extra}
    path.write_text(json.dumps(rec))
    git(root, "add", "-A")
    git(root, "commit", "-m", "age the claim")


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").stdout


def test_a_stale_claim_is_reclaimed_by_the_next_claim(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    dead = ex.claim(xroot, "dead-researcher", push=False)
    age_claim(xroot, dead["_file"])
    again = ex.claim(xroot, "live-researcher", push=False)
    assert again["run_id"] == "r1" and again["claimed_by"] == "live-researcher"
    assert again["attempts"] == 1          # the dead run counted as an attempt
    assert [c["_file"] for c in ex.claimed_requests(xroot)] == [dead["_file"]]


def test_a_fresh_claim_is_left_alone(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    ex.claim(xroot, "busy", push=False)
    assert ex.reclaim_stale(xroot, push=False) == []
    assert ex.claim(xroot, "other", push=False) is None
    assert ex.claimed_requests(xroot)[0]["claimed_by"] == "busy"


def test_the_stale_budget_is_a_parameter(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    age_claim(xroot, got["_file"], "2026-08-16T10:00:00Z")
    now = dt.datetime(2026, 8, 16, 13, tzinfo=dt.timezone.utc)
    assert ex.reclaim_stale(xroot, push=False, now=now) == []
    assert ex.reclaim_stale(xroot, push=False, now=now,
                            stale_hours=2) == [got["_file"]]


def test_a_request_that_keeps_going_stale_ends_in_failed(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    age_claim(xroot, got["_file"], attempts=ex.MAX_REQUEST_ATTEMPTS - 1)
    assert ex.reclaim_stale(xroot, push=False) == [got["_file"]]
    assert [f["error_category"] for f in ex.failed_requests(xroot)] == ["stale_claim"]
    assert ex.claimed_requests(xroot) == [] and ex.pending_requests(xroot) == []
    assert "exchange: reclaim 1 stale claim" in git(xroot, "log", "-1",
                                                    "--format=%s").stdout


def test_a_claim_with_an_unreadable_stamp_is_left_for_a_human(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    age_claim(xroot, got["_file"], "last tuesday")
    assert ex.reclaim_stale(xroot, push=False) == []


def test_reclaiming_twice_is_a_no_op(xroot):  # noqa: F811
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    age_claim(xroot, got["_file"])
    assert ex.reclaim_stale(xroot, push=False) == [got["_file"]]
    before = head(xroot)
    assert ex.reclaim_stale(xroot, push=False) == []
    assert head(xroot) == before
    assert ex.pending_requests(xroot)[0]["attempts"] == 1


def test_both_sides_reclaiming_the_same_claim_count_it_once(tmp_path):
    """The on-prem node and the researcher each see the same stale claim.
    The first reclaim to reach the remote wins; the other side's push is
    rejected and its reclaim dropped rather than rebased on top, which would
    count the dead run twice or conflict with the fresh claim."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True,
                   capture_output=True)
    onprem = init_repo(tmp_path / "onprem", {"README.md": "x\n"})
    git(onprem, "remote", "add", "origin", str(origin))
    git(onprem, "push", "-u", "origin", "main")
    ex.enqueue(onprem, req(), push=True)
    researcher = tmp_path / "researcher"
    subprocess.run(["git", "clone", "-q", str(origin), str(researcher)], check=True)
    git(researcher, "config", "user.email", "t@t")
    git(researcher, "config", "user.name", "t")
    got = ex.claim(researcher, "dead", push=True)
    age_claim(researcher, got["_file"])
    git(researcher, "push")
    ex.pull(onprem)

    # the researcher reclaims and claims it again, pushed...
    again = ex.claim(researcher, "alive", push=True)
    assert again["attempts"] == 1
    # ...while the on-prem side still holds the old, stale view
    before = head(onprem)
    assert ex.reclaim_stale(onprem, push=True) == []
    assert head(onprem) == before
    assert git(onprem, "status", "--porcelain").stdout == ""

    ex.pull(onprem)
    [claimed] = ex.claimed_requests(onprem)
    assert claimed["claimed_by"] == "alive" and claimed["attempts"] == 1
    assert ex.pending_requests(onprem) == [] and ex.failed_requests(onprem) == []


def test_the_on_prem_round_reclaims_with_its_own_budget(cfg, xroot):  # noqa: F811
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    orch.research(["errors/ORA-12154.md"], [], run_id="run-1")
    got = ex.claim(xroot, "researcher", push=False)
    age_claim(xroot, got["_file"], "2026-01-01T00:00:00Z")
    cfg.research["exchange"]["stale_hours"] = 10 ** 6   # not stale yet
    orch.research([], [], run_id="run-2")
    assert [c["_file"] for c in ex.claimed_requests(xroot)] == [got["_file"]]
    del cfg.research["exchange"]["stale_hours"]         # the default
    orch.research([], [], run_id="run-3")
    assert ex.claimed_requests(xroot) == []
    assert ex.pending_requests(xroot)[0]["attempts"] == 1


def test_health_reads_the_exchange_budget(cfg, xroot):  # noqa: F811
    from dbwiki.health import _exchange_state, exchange_stale_hours
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    age_claim(xroot, got["_file"], "2026-08-15T00:00:00Z")
    now = "2026-08-16T12:00:00Z"                        # 36h later
    assert exchange_stale_hours(cfg) == ex.STALE_CLAIM_HOURS
    assert _exchange_state(cfg, now, exchange_stale_hours(cfg))["stale_claimed"]
    cfg.research["exchange"]["stale_hours"] = 48
    assert not _exchange_state(cfg, now, exchange_stale_hours(cfg))["stale_claimed"]


def test_the_researcher_reads_its_budget_from_its_config(tmp_path, xroot):  # noqa: F811
    from dbwiki_researcher.cli import load_config
    path = tmp_path / "researcher.yaml"
    path.write_text(f"exchange:\n  path: {xroot}\n")
    assert load_config(path)["stale_hours"] == ex.STALE_CLAIM_HOURS
    path.write_text(f"exchange:\n  path: {xroot}\nstale_hours: 6\n")
    assert load_config(path)["stale_hours"] == 6
