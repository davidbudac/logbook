"""The Langfuse prompt registry (promptreg.py): which static block a stage
runs under, and registration that creates a version only when the text moved.
The langfuse client is stubbed — these tests verify what we hand it."""

from types import SimpleNamespace

import pytest

from dbwiki import promptreg
from dbwiki.research_structured import _RESEARCH_CONTRACT
from dbwiki.structured import INGEST_TEMPLATE, _REPORT_CONTRACT


@pytest.fixture(autouse=True)
def reset_cache():
    promptreg._cache = {}
    yield
    promptreg._cache = {}


class StubPrompt:
    def __init__(self, name, prompt, version):
        self.name, self.prompt, self.version = name, prompt, version


class StubClient:
    """A tiny Langfuse prompt store: latest production version per name."""

    def __init__(self, stored: dict[str, str] | None = None):
        self.stored = dict(stored or {})
        self.gets: list[str] = []
        self.creates: list[tuple[str, str]] = []
        self.flushed = 0

    def flush(self):
        self.flushed += 1

    def get_prompt(self, name, *, label=None, fallback=None):
        self.gets.append(name)
        if name not in self.stored:
            raise ValueError(f"prompt {name} not found")
        return StubPrompt(name, self.stored[name], 1)

    def create_prompt(self, *, name, prompt, type, labels):
        assert type == "text" and labels == ["production"]
        self.creates.append((name, prompt))
        self.stored[name] = prompt
        return StubPrompt(name, prompt, 2)


def cfg(wiki_repo=None):
    return SimpleNamespace(wiki_repo=wiki_repo)


def test_structured_stages_map_to_their_contracts():
    assert promptreg.lookup(cfg(), "ingest", "structured") == (
        "dbwiki/ingest-structured", INGEST_TEMPLATE)
    assert promptreg.lookup(cfg(), "report", "structured") == (
        "dbwiki/report-structured", _REPORT_CONTRACT)
    assert promptreg.lookup(cfg(), "research", "structured") == (
        "dbwiki/research-structured", _RESEARCH_CONTRACT)


def test_ingest_template_carries_the_mustache_variables():
    for var in ("{{db}}", "{{day}}", "{{codes}}"):
        assert var in INGEST_TEMPLATE


def test_strong_tier_report_is_the_escalated_prompt():
    routine = promptreg.lookup(cfg(), "report", "structured", "cheap")
    escalated = promptreg.lookup(cfg(), "report", "structured", "strong")
    assert routine[0] == "dbwiki/report-structured"
    assert escalated[0] == "dbwiki/report-escalated"
    assert escalated[1] != routine[1]
    assert "notable_analysis" in escalated[1]
    assert "Escalated-window rules" in escalated[1]
    # an agentic report is the agent file whatever the tier
    assert promptreg.lookup(cfg(), "report", "agentic", "strong") is None


def test_agentic_stages_share_the_wiki_agents_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# how to behave\n")
    for task in promptreg.AGENTIC_TASKS:
        assert promptreg.lookup(cfg(tmp_path), task, "agentic") == (
            "dbwiki/agents-md", "# how to behave\n")


def test_no_prompt_for_stages_without_a_static_block(tmp_path):
    assert promptreg.lookup(cfg(tmp_path), "research", "offload") is None
    assert promptreg.lookup(cfg(), "ingest", None) is None
    assert promptreg.lookup(cfg(), None, None) is None
    # agentic with no AGENTS.md on disk: nothing to version, not a failure
    assert promptreg.lookup(cfg(tmp_path), "ingest", "agentic") is None


def test_identical_text_reuses_the_stored_version():
    client = StubClient({"dbwiki/ingest-structured": INGEST_TEMPLATE})
    p = promptreg.register(client, "dbwiki/ingest-structured", INGEST_TEMPLATE)
    assert p.version == 1 and client.creates == []


def test_changed_text_creates_a_new_version():
    client = StubClient({"dbwiki/ingest-structured": "older wording"})
    p = promptreg.register(client, "dbwiki/ingest-structured", INGEST_TEMPLATE)
    assert p.version == 2
    assert client.creates == [("dbwiki/ingest-structured", INGEST_TEMPLATE)]


def test_unknown_prompt_is_created():
    client = StubClient()
    promptreg.register(client, "dbwiki/agents-md", "text")
    assert client.creates == [("dbwiki/agents-md", "text")]


def test_registration_is_cached_per_process():
    client = StubClient()
    first = promptreg.register(client, "dbwiki/agents-md", "text")
    second = promptreg.register(client, "dbwiki/agents-md", "text")
    assert first is second
    assert len(client.gets) == 1 and len(client.creates) == 1
    # a text change inside the same process still registers
    promptreg.register(client, "dbwiki/agents-md", "other")
    assert len(client.creates) == 2


def test_never_raises():
    class Broken:
        def get_prompt(self, *a, **kw):
            raise RuntimeError("unreachable")

        def create_prompt(self, **kw):
            raise RuntimeError("unreachable")

    assert promptreg.register(Broken(), "dbwiki/agents-md", "text") is None


def test_sync_prompts_registers_every_version(monkeypatch, tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n")
    client = StubClient()
    from dbwiki import observability
    monkeypatch.setattr(observability, "_get_client", lambda lf: client)
    lines = promptreg.sync_prompts(
        SimpleNamespace(wiki_repo=tmp_path, langfuse={"host": "h"}))
    assert len(lines) == 5 and all(line.endswith("v2") for line in lines)
    assert client.flushed == 1


def test_sync_prompts_without_a_client_is_an_error(monkeypatch):
    from dbwiki import observability
    monkeypatch.setattr(observability, "_get_client", lambda lf: None)
    with pytest.raises(RuntimeError, match="uv sync"):
        promptreg.sync_prompts(SimpleNamespace(wiki_repo=None, langfuse={}))


class FakeApi:
    """The Langfuse model table as the sync sees it: pages of rows in, POST
    and DELETE recorded."""

    def __init__(self, pages: list[list[dict]] | None = None):
        self.pages = pages or [[]]
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            return None
        page = int(path.split("page=")[1])
        return {"data": self.pages[page - 1],
                "meta": {"totalPages": len(self.pages)}}


def stored_row(name, match, in_price, out_price, managed=False, id="m1"):
    return {"id": id, "modelName": name, "matchPattern": match,
            "unit": "TOKENS", "inputPrice": in_price, "outputPrice": out_price,
            "isLangfuseManaged": managed}


NEMOTRON = {"name": "unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF",
            "match": "(?i)^unsloth/", "input_per_million": 0,
            "output_per_million": 0}
LUNA = {"name": "gpt-5.6-luna", "match": r"(?i)^gpt-5\.6-luna$",
        "input_per_million": 1.25, "output_per_million": 10}


@pytest.fixture
def fake_api(monkeypatch):
    def install(pages=None):
        client = FakeApi(pages)
        monkeypatch.setattr(promptreg, "api", lambda lf_cfg: client)
        return client

    return install


def test_missing_model_is_created(fake_api):
    client = fake_api()
    (line,) = promptreg.sync_models({"models": [LUNA]})
    assert "created" in line and "gpt-5.6-luna" in line
    method, path, payload = client.calls[-1]
    assert (method, path) == ("POST", "/api/public/models")
    assert payload == {"modelName": "gpt-5.6-luna",
                       "matchPattern": r"(?i)^gpt-5\.6-luna$",
                       "unit": "TOKENS",
                       "inputPrice": 1.25e-6, "outputPrice": 1e-5}


def test_identical_row_is_left_alone(fake_api):
    client = fake_api([[stored_row("gpt-5.6-luna", r"(?i)^gpt-5\.6-luna$",
                                   1.25e-6, 1e-5)]])
    (line,) = promptreg.sync_models({"models": [LUNA]})
    assert "unchanged" in line
    assert [c[0] for c in client.calls] == ["GET"]


def test_changed_price_is_delete_then_create(fake_api):
    client = fake_api([[stored_row("gpt-5.6-luna", r"(?i)^gpt-5\.6-luna$",
                                   9e-6, 9e-5, id="old")]])
    (line,) = promptreg.sync_models({"models": [LUNA]})
    assert "replaced" in line
    assert [(c[0], c[1]) for c in client.calls[1:]] == [
        ("DELETE", "/api/public/models/old"),
        ("POST", "/api/public/models")]


def test_builtin_row_of_the_same_name_is_never_deleted(fake_api):
    client = fake_api([[stored_row("gpt-5.6-luna", "(?i)^(gpt-5.6-luna)$",
                                   9e-6, 9e-5, managed=True, id="builtin")]])
    (line,) = promptreg.sync_models({"models": [LUNA]})
    assert "created" in line
    assert [c[0] for c in client.calls] == ["GET", "POST"]


def test_every_page_of_the_model_table_is_read(fake_api):
    client = fake_api([[stored_row("other", "x", 0.0, 0.0)],
                       [stored_row("gpt-5.6-luna", r"(?i)^gpt-5\.6-luna$",
                                   1.25e-6, 1e-5)]])
    assert "unchanged" in promptreg.sync_models({"models": [LUNA]})[0]
    assert [c[1] for c in client.calls] == [
        "/api/public/models?limit=100&page=1",
        "/api/public/models?limit=100&page=2"]


def test_zero_priced_local_model_is_a_price_not_a_gap(fake_api):
    fake_api()
    (line,) = promptreg.sync_models({"models": [NEMOTRON]})
    assert "created" in line and "0/0 USD per 1M tokens" in line


def test_a_row_missing_a_price_is_refused(fake_api):
    fake_api()
    bad = {"name": "m", "match": "^m$", "input_per_million": 1}
    with pytest.raises(RuntimeError, match="output_per_million"):
        promptreg.sync_models({"models": [bad]})


def test_no_models_configured_touches_nothing(monkeypatch):
    monkeypatch.setattr(promptreg, "api", lambda lf_cfg: pytest.fail(
        "sync_models must not open a connection with nothing to sync"))
    assert promptreg.sync_models({}) == [
        "models: none configured (langfuse.models is empty)"]


def test_api_needs_a_host_and_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="langfuse.host"):
        promptreg.api({})
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        promptreg.api({"host": "http://lf:3000"})
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert promptreg.api({"host": "http://lf:3000/"}).host == "http://lf:3000"


def test_all_prompts_covers_the_table_once(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n")
    names = [n for n, _ in promptreg.all_prompts(cfg(tmp_path))]
    assert sorted(names) == ["dbwiki/agents-md", "dbwiki/ingest-structured",
                             "dbwiki/report-escalated",
                             "dbwiki/report-structured",
                             "dbwiki/research-structured"]
