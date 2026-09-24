"""The stdlib cron next-run calculator in elk/scripts/emit_derived.py (loop
observability: the `schedule` stream). Imported via importlib since the elk/
scripts tree is a standalone stdlib-only script, not part of the `dbwiki`
package — see the module docstring there for why (runs under bare python3
from cron, no venv)."""

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "elk" / "scripts" / "emit_derived.py"
spec = importlib.util.spec_from_file_location("emit_derived", SCRIPT)
emit_derived = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emit_derived)

next_run = emit_derived.next_run

TZ = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 8, 12, 14, 15, 7, tzinfo=TZ)  # a Wednesday


def test_every_two_hours_at_15():
    assert next_run("15 */2 * * *", NOW) == dt.datetime(2026, 8, 12, 16, 15, tzinfo=TZ)


def test_daily_2330():
    assert next_run("30 23 * * *", NOW) == dt.datetime(2026, 8, 12, 23, 30, tzinfo=TZ)


def test_weekly_monday_8am():
    got = next_run("0 8 * * 1", NOW)
    assert got == dt.datetime(2026, 8, 17, 8, 0, tzinfo=TZ)
    assert got.weekday() == 0  # Monday


def test_monthly_first_9am():
    assert next_run("0 9 1 * *", NOW) == dt.datetime(2026, 9, 1, 9, 0, tzinfo=TZ)


def test_every_15_minutes():
    assert next_run("*/15 * * * *", NOW) == dt.datetime(2026, 8, 12, 14, 30, tzinfo=TZ)


def test_range_with_step():
    # minute field 10-40/10 -> 10, 20, 30, 40; next after 14:15 is 14:20
    assert next_run("10-40/10 * * * *", NOW) == dt.datetime(2026, 8, 12, 14, 20, tzinfo=TZ)


def test_dom_dow_or_semantics():
    # both dom (1,15) and dow (Monday) restricted -> OR; the 15th (Saturday)
    # fires before the next Monday (the 17th), so it wins
    got = next_run("0 0 1,15 * 1", NOW)
    assert got == dt.datetime(2026, 8, 15, 0, 0, tzinfo=TZ)


def test_interval_is_the_gap_between_consecutive_fires():
    first = next_run("15 */2 * * *", NOW)
    second = next_run("15 */2 * * *", first)
    assert (second - first).total_seconds() == 7200


def test_unparseable_cron_raises():
    with pytest.raises(ValueError, match="must have 5 fields"):
        next_run("not a cron", NOW)
