"""Suite-wide guard: no test may open a socket to a live dependency.

`dbwiki health` probes the local model server and Elasticsearch, and a test
that reaches either passes or fails by what happens to be running on the
developer's machine — `test_missing_wiki_is_a_reported_category_not_a_crash`
was connecting to localhost:1234 on every run until 2026-08. Loopback in
general stays allowed: the webhook-sink tests serve a real HTTP server on
127.0.0.1.

The same reasoning covers pi's provider table: the probe reads `base_url` and
the API key out of the developer's own `~/.pi/agent/models.json`, so every
test gets a path that does not exist instead.
"""

import socket

import pytest

from dbwiki import health

#: The model servers (LM Studio 1234, unsloth studio 8888), Elasticsearch,
#: and the incident workbench (8765): a test that forgets get_json= or binds a
#: fixed port must never reach a developer's live server.
#: Only these — the guard is a tripwire for an un-faked dependency probe, not
#: a general network ban.
FORBIDDEN_PORTS = {1234, 8888, 9200, 8765}


@pytest.fixture(autouse=True)
def no_live_dependency_sockets(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if port in FORBIDDEN_PORTS:
            raise AssertionError(
                f"a test tried to connect to {address}: the model-server "
                f"probe and Elasticsearch must be faked (pass get_json=/es=), "
                f"never reached")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def no_developer_pi_provider_table(monkeypatch, tmp_path):
    """The health probe resolves base URL and API key from pi's provider
    table. Point it at nothing so no test inherits the developer's."""
    monkeypatch.setattr(health, "PI_MODELS_JSON", tmp_path / "no-pi-models.json")
