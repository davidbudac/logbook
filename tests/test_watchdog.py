"""n8n/scripts/watchdog.py --fix: the reload it decides on, the POST it sends,
and the fresh read it proves the reload with. Imported by path like
test_emit_derived.py — n8n/scripts is a standalone stdlib-only script tree
(bare python3 from cron, no venv), not part of `dbwiki`.

Every test fakes the urlopen seam: conftest's guard fails any test that opens
a socket to the studio's port, and the studio is the developer's own machine.
"""

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "n8n" / "scripts" / "watchdog.py"
spec = importlib.util.spec_from_file_location("watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

MODEL = "unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF"
OTHER = "bottlecapai/ThinkingCap-Qwen3.6-27B-GGUF"
BASE = "http://127.0.0.1:8888"

def config_yaml(min_context):
    return f"""\
agents:
  pi:
    provider: unsloth
    cheap: {MODEL}
    strong: {MODEL}
health:
  min_context: {min_context}
"""

PI_MODELS = json.dumps({"providers": {"unsloth": {
    "baseUrl": f"{BASE}/v1", "apiKey": "sk-test"}}})


def listing(context_length, loaded=True, quant="UD-Q4_K_XL"):
    """The studio's /v1/models payload, in its own dialect: served context in
    `context_length`, load state in the boolean `loaded`."""
    return {"object": "list", "data": [
        {"id": MODEL, "object": "model", "owned_by": "unsloth-studio",
         "quant": quant, "context_length": context_length,
         "max_context_length": 8192, "native_context_length": 1048576,
         "loaded": loaded},
        {"id": OTHER, "object": "model", "owned_by": "unsloth-studio",
         "quant": "Q4_K_M", "loaded": False},
    ]}


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeStudio:
    """unsloth studio at the urlopen seam. `listings` is consumed one per
    models read and the last one repeats, so a test says what the server looks
    like before and after the reload; `phases` does the same for
    load-progress."""

    def __init__(self, listings, phases=("ready",), post_error=None):
        self.listings = list(listings)
        self.phases = list(phases)
        self.post_error = post_error
        self.calls = []

    def _next(self, queue):
        return queue[0] if len(queue) == 1 else queue.pop(0)

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def urlopen(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((method, url, body))
        if method == "POST":
            if self.post_error is not None:
                raise self.post_error
            return _Response({"load_request_id": body["load_request_id"]})
        if url.endswith("/api/v0/models"):        # LM Studio's API; unsloth has none
            raise urllib.error.HTTPError(url, 404, "API endpoint not found", {}, None)
        if url.endswith("/v1/models"):
            return _Response(self._next(self.listings))
        if url.endswith("/api/inference/load-progress"):
            phase = self._next(self.phases)
            return _Response({"phase": phase, "fraction": 1.0 if phase == "ready" else 0.4})
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture
def studio(monkeypatch, tmp_path):
    """A fake studio plus fixture copies of the two files the watchdog reads
    from the developer's machine. The other three checks answer clean so the
    exit code tracks the model server alone."""
    cfg = tmp_path / "dbwiki.yaml"
    (tmp_path / "models.json").write_text(PI_MODELS)
    monkeypatch.setattr(watchdog, "CONFIG", cfg)
    monkeypatch.setattr(watchdog, "PI_MODELS_JSON", tmp_path / "models.json")
    for check in ("check_tick", "check_failures", "check_crontab"):
        monkeypatch.setattr(watchdog, check, lambda *a, **k: {"problems": []})
    monkeypatch.setattr(watchdog.time, "sleep", lambda *_: None)

    def make(listings, phases=("ready",), post_error=None, min_context=32768):
        cfg.write_text(config_yaml(min_context))
        fake = FakeStudio(listings, phases, post_error)
        monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
        return fake

    return make


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["watchdog.py", *argv])
    rc = watchdog.main()
    return rc, json.loads(capsys.readouterr().out)


def test_a_model_already_serving_a_full_context_is_never_reloaded(
        studio, monkeypatch, capsys):
    fake = studio([listing(98176)])
    rc, report = run(monkeypatch, capsys, "--fix")
    assert fake.posts == []
    assert report["fix"] == {"model": MODEL, "verdict": "healthy",
                             "reloaded": False, "context_before": 98176}
    assert rc == 0


def test_a_short_context_model_is_reloaded_with_the_variant_the_server_reports(
        studio, monkeypatch, capsys):
    fake = studio([listing(8192), listing(98176)], phases=("loading", "ready"))
    rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    _, url, body = fake.posts[0]
    assert url == f"{BASE}/api/inference/load"
    assert body["model_path"] == MODEL
    assert body["gguf_variant"] == "UD-Q4_K_XL"
    assert body["max_seq_length"] == 98176
    assert body["force_reload"] is True
    assert body["load_request_id"]
    assert fake.phases == ["ready"]           # the poll loop waited out `loading`
    fix = report["fix"]
    assert (fix["verdict"], fix["context_before"], fix["context_after"]) == (
        "context_too_small", 8192, 98176)
    assert fix["reloaded"] is True
    assert report["problems"] == []
    assert rc == 0


def test_a_model_the_studio_has_unloaded_is_reloaded(studio, monkeypatch, capsys):
    fake = studio([listing(98176, loaded=False), listing(98176)])
    rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    assert report["fix"]["verdict"] == "not_loaded"
    assert report["fix"]["reloaded"] is True
    assert report["fix"]["context_after"] == 98176
    assert rc == 0


def test_a_load_post_that_raises_is_a_problem_and_not_a_reload(
        studio, monkeypatch, capsys):
    fake = studio([listing(8192)], post_error=urllib.error.URLError("refused"))
    rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    assert report["fix"]["reloaded"] is False
    assert "URLError" in report["fix"]["error"]
    assert any("--fix failed to reload" in p for p in report["problems"])
    assert rc == 1


def test_a_reload_that_leaves_the_context_short_stays_a_failure(
        studio, monkeypatch, capsys):
    fake = studio([listing(8192)])
    rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    assert report["fix"]["reloaded"] is False
    assert report["fix"]["context_after"] == 8192
    assert "context_too_small" in report["fix"]["error"]
    assert rc == 1


def test_a_second_fix_run_against_the_reloaded_server_posts_nothing(
        studio, monkeypatch, capsys):
    fake = studio([listing(8192), listing(98176)])
    first_rc, _ = run(monkeypatch, capsys, "--fix")
    second_rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    assert report["fix"]["verdict"] == "healthy"
    assert (first_rc, second_rc) == (0, 0)


def test_without_fix_a_short_context_server_is_reported_and_never_posted(
        studio, monkeypatch, capsys):
    fake = studio([listing(8192)])
    rc, report = run(monkeypatch, capsys)
    assert fake.posts == []
    assert "fix" not in report
    assert any("context 8192 < 32768" in p for p in report["problems"])
    assert rc == 1


def test_an_unloaded_model_is_not_counted_as_loaded(studio, monkeypatch, capsys):
    """Reading LM Studio's `state` alone put every unsloth model in the loaded
    list, which is what kept the not-loaded branch dead for a week."""
    studio([listing(98176)])
    _, report = run(monkeypatch, capsys)
    check = report["checks"]["model_server"]
    assert check["loaded"] == [{"id": MODEL, "loaded": True, "context_length": 98176}]
    assert check["note"] == "/api/v0 unavailable; load state from the listing"


def test_a_server_that_reports_no_load_state_is_never_reloaded_on_a_guess(
        studio, monkeypatch, capsys):
    """Plain /v1/models says neither `loaded` nor `state`. That is `unknown`,
    and the fix acts on facts."""
    plain = {"data": [{"id": MODEL, "object": "model", "context_length": 98176}]}
    fake = studio([plain])
    rc, report = run(monkeypatch, capsys, "--fix")
    assert fake.posts == []
    assert report["fix"]["verdict"] == "unknown"
    assert (report["checks"]["model_server"]["note"]
            == "/api/v0 unavailable; loaded state unknown")
    assert rc == 0


def test_the_minimum_context_comes_from_the_config_not_the_hardcoded_fallback(
        studio, monkeypatch, capsys):
    """32768 is also the fallback, so only a differing configured value tells
    a working scrape from an ignored one."""
    fake = studio([listing(40960)], min_context=65536)
    rc, report = run(monkeypatch, capsys)
    assert fake.posts == []
    assert any("context 40960 < 65536" in p for p in report["problems"])
    assert rc == 1


def test_a_context_short_of_the_configured_minimum_is_reloaded(
        studio, monkeypatch, capsys):
    fake = studio([listing(40960), listing(98176)], min_context=65536)
    rc, report = run(monkeypatch, capsys, "--fix")
    assert len(fake.posts) == 1
    fix = report["fix"]
    assert (fix["verdict"], fix["context_before"], fix["reloaded"]) == (
        "context_too_small", 40960, True)
    assert rc == 0


def test_the_pi_thinking_suffix_is_not_part_of_the_served_model_id(
        studio, monkeypatch, tmp_path):
    cfg = tmp_path / "dbwiki.yaml"
    cfg.write_text(config_yaml(32768).replace(f"cheap: {MODEL}",
                                              f"cheap: {MODEL}:off"))
    assert watchdog._configured_models() == [MODEL]
