"""`dbwiki langfuse sync` at the CLI edge, and the repo's own `langfuse.models:`
rows. The command is the only place that talks to the Langfuse admin API, so
what is under test here is the wiring and the failure exit, not the HTTP."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dbwiki import cli, promptreg

# the live config when this is a deployment checkout, the shipped example
# otherwise (a fresh clone has no config/dbwiki.yaml)
_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_CONFIG_PATH = next(p for p in (_CONFIG_DIR / "dbwiki.yaml",
                                _CONFIG_DIR / "dbwiki.yaml.example") if p.exists())
CONFIG = yaml.safe_load(_CONFIG_PATH.read_text())


@pytest.fixture
def stub_config(monkeypatch):
    monkeypatch.setattr(cli, "load_config",
                        lambda *a, **kw: SimpleNamespace(langfuse={"host": "h"}))


def test_sync_prints_prompts_then_models(stub_config, monkeypatch, capsys):
    monkeypatch.setattr(promptreg, "sync_prompts",
                        lambda cfg: ["prompt dbwiki/ingest-structured: v3"])
    monkeypatch.setattr(promptreg, "sync_models",
                        lambda lf: ["model gpt-5.6-luna: created"])
    assert cli.main(["langfuse", "sync"]) == 0
    assert capsys.readouterr().out == ("prompt dbwiki/ingest-structured: v3\n"
                                       "model gpt-5.6-luna: created\n")


def test_an_unreachable_server_is_an_exit_code_not_a_traceback(
        stub_config, monkeypatch, capsys):
    def boom(cfg):
        raise RuntimeError("GET /api/public/models: HTTP 401 unauthorized")

    monkeypatch.setattr(promptreg, "sync_prompts", boom)
    assert cli.main(["langfuse", "sync"]) == 2
    assert "HTTP 401" in capsys.readouterr().err


def test_configured_model_rows_are_well_formed():
    rows = (CONFIG.get("langfuse") or {}).get("models") or []
    if _CONFIG_PATH.name.endswith(".example"):
        pytest.skip("the example config ships its price table commented out")
    assert rows, "config/dbwiki.yaml should price the models this pipeline runs"
    for row in rows:
        payload = promptreg._row(row)
        assert payload["unit"] == "TOKENS"
        assert payload["inputPrice"] == row["input_per_million"] / 1e6
    names = [r["name"] for r in rows]
    assert "unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF" in names
