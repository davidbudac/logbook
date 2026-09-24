"""The analyst fold-in at the start of `dbwiki run` (ADR-0001, issue 15):
it runs inside the run recorder, so a failure there is a run-health note and
not a tick that died before it could record anything. Same fakes as
test_cli_run: no ES, no agent, no git."""

from test_cli_run import DAY, event, tick  # noqa: F401 — `tick` is a fixture

from dbwiki import cli, queue


def test_a_failed_analyst_fold_in_is_recorded_and_the_tick_runs_on(
        tick, monkeypatch, capsys):  # noqa: F811
    def broken(*a, **k):
        raise RuntimeError("git commit: index.lock exists")

    monkeypatch.setattr(queue, "fold_results", broken)
    assert cli.main(["run"]) == 0
    ev = event(tick)
    assert "index.lock" in ev["facts"]["fold_error"]
    assert ("report", ["db1", "db2"]) in tick.calls
    assert ("render_html", DAY) in tick.calls
    assert "fold-in failed" in capsys.readouterr().err


def test_folded_results_are_counted_in_the_run_event(tick, monkeypatch):  # noqa: F811
    monkeypatch.setattr(queue, "fold_results", lambda *a, **k: 2)
    assert cli.main(["run"]) == 0
    assert event(tick)["facts"]["analyst_results_folded"] == 2
