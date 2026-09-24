# Contributing

Logbook is a solo-maintained tool that runs daily against one Oracle fleet.
Issues and pull requests are welcome; there is no support commitment.

## Development loop

```sh
uv sync                     # Python 3.13+, see .python-version
uv run pytest tests/        # ~3200 tests, hermetic: no Elasticsearch, no model, no wiki
uv run ruff check .         # the only lint CI enforces
```

The suite blocks sockets to the ports a live deployment would use, so a test
that tries to reach Elasticsearch or a model server fails loudly instead of
depending on your machine.

## Rules that will save you a round-trip

- **Goldens are regenerated on purpose.** `tests/fixtures/golden/` holds the
  compactor's expected output. Change it only with `DBWIKI_UPDATE_GOLDENS=1`
  and say why in the commit; CI fails if a plain test run rewrites one.
- **Formatting is not enforced.** `ruff format` would rewrite most of the
  tree; the code is hand-aligned. Do not send a formatting-only change.
- **`docs/site.html` is generated.** After editing `docs/*.md`, run
  `uv run python docs/build_site.py` and commit the result; CI checks it is current.
- **The wiki is a separate repository.** Nothing under `wiki/` belongs in
  this repo; `wiki-template/` is the only wiki content tracked here, and
  `wiki-template/AGENTS.md` is the contract agents write against.
- **Names.** The project is Logbook; the CLI, Python package, config file and
  telemetry prefixes are `dbwiki` and stay that way.

## Where things live

`CHANGELOG.md` explains why a setting has the value it has. `DESIGN.md` is
the architecture, `docs/adr/` the decisions, `docs/reference.md` every knob.
