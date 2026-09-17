# Security

Logbook is maintained by one person and has no formal disclosure process.
Report a vulnerability by opening a GitHub issue with the label `security`,
or, if the details should not be public, by emailing the maintainer at the
address on the GitHub profile.

## What to keep in mind when you deploy it

- **Prompts embed real log lines.** Nothing leaves the machine unless you
  point an adapter at a hosted model, enable the Langfuse exporter, or turn
  on research. `dbwiki redact` shows exactly what an off-box request would
  carry; `docs/adr/0002-offload-research-anonymized.md` is the design.
- **Credentials come from the environment.** `DBWIKI_ES_PASSWORD` and the
  alert webhook URL override anything in `config/dbwiki.yaml`; the config
  file is yours and is ignored by git in this repository.
- **The portal is an unauthenticated HTTP server.** `dbwiki portal serve`
  binds to the address in `portal.bind`; keep it on a private network or
  behind a reverse proxy that authenticates. The same goes for the daily
  HTML pages served by `deploy/serve_wiki_html.sh`.
- **The wiki is a git repository the model writes into.** Every agent edit
  passes validation and lint before it is committed, and a failed run is
  rolled back, but treat the wiki remote as internal.
