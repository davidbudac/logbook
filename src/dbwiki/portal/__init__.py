"""The incident action workbench over HTTP: a thin shell around
`incident_action`, `lifecycle` and `transaction`, served by the stdlib
`ThreadingHTTPServer`.

Module map (dependency arrows point down; nothing here is imported by a
domain module):

    portal/server.py    ThreadingHTTPServer, the route table, the localhost
                        baseline (bind, Host/Origin, JSON-only mutations),
                        `serve`, `make_server`, `add_portal_parser`.
    portal/api.py       `Workbench`: one method per endpoint; request ->
                        `incident_action.Action` -> outcome -> response
                        through tables. The only module here that calls
                        `transaction.preview` and `incident_action.publish`.
    portal/identity.py  `Provider` protocol, `TrustedOperator`, `Role`,
                        `ROLE_OF` (command type -> role), `require`.
    portal/wire.py      The JSON dialect: domain values -> dicts, request
                        bodies -> `WriteRequest`. Nothing else spells a key.
    portal/ui.py        The one page (`workbench.html` + `daily_html.CSS`)
                        and its static checks.
The advisory panel reaches `advisory.Runner` through three of those routes:
`GET .../tools` prices every row, `POST .../advisory` admits one run, and
`GET /api/advisory` polls it. `advisory.py` is not under `portal/` because it
is not the portal's: its runs are files under `.state/`, its ceilings are read
from the same ledger `health` writes, and a future CLI would start one without
a server.

Public surface, and all of it: `serve(cfg, bind=...)`, `make_server(...)`
for tests, `ROUTES`, `add_portal_parser(sub)`. A reader traces a click to a
commit sha through `workbench.html` (its `api("POST", ".../commit")` call),
`server.ROUTES` (the row), `api.Workbench.commit` (the method) and
`incident_action.publish`: three files past the page.

There is no schema endpoint. Every form the page draws is described by the
incident's own `allowed[*]`, so a verb's fields arrive with the reason the
verb is offered and cannot be read for an incident that does not offer it.

Rules that live elsewhere and are only *reached* from here: which verbs
exist and what they take (`incident_action`), which transitions are legal
(`lifecycle.TRANSITIONS`), which files a command touches (`lifecycle.build`),
what a record may contain (`incidents.ActionRecord`), what may be committed
and when (`transaction.commit`), who holds the wiki (`lock.single_flight`).
"""

from .server import ROUTES, add_portal_parser, make_server, serve

__all__ = ["ROUTES", "add_portal_parser", "make_server", "serve"]
