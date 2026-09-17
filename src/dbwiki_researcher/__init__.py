"""dbwiki-researcher: the lean, cloud-connected half of ADR-0002. Claims
anonymized research requests from the exchange repo, runs a web-capable
agent over nothing but the request, validates the answer and pushes it back
as a result file. No ES client, no compactor, no wiki clone, no LM Studio —
only git, the adapter harness and the exchange protocol."""
