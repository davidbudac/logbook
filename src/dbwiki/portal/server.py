"""The HTTP shell: `ThreadingHTTPServer`, one handler class, a route table,
and the localhost baseline. Knows methods, paths, headers and status codes;
knows nothing about incidents.

Localhost baseline (Stage 1; Stage 4 hardens this, it is a seam and not
security):

- `serve` binds `cfg.portal["bind"]`, default `127.0.0.1:8765`. A
  non-loopback `--bind` is an explicit operator choice: it warns on stderr
  (every write still carries the one `TrustedOperator` identity) rather than
  refusing. `make_server` itself still refuses non-loopback by default;
  `serve` is the only caller that opts in via `allow_remote`.
- `Host` must name the bound socket, or a loopback spelling of the bound
  port (`127.0.0.1`, `localhost`, `[::1]`); otherwise 403 `bad_host`, so a
  DNS-rebinding page cannot reach the API through a resolved name. The docs
  point operators at `http://127.0.0.1:8765` rather than `localhost`,
  because a browser resolving `localhost` to `::1` against a `127.0.0.1`
  bind fails to connect before any header check runs.
- `Origin`, when present, must match an allowed host; otherwise 403
  `bad_origin`. Browsers send it on every cross-site POST.
- POST accepts only `Content-Type: application/json` (415 `json_only`) with
  a JSON object body (400). An HTML form cannot produce that content type
  without a CORS preflight this server never answers, so a cross-site form
  post cannot mutate.
- Responses carry `Cache-Control: no-store`, `X-Content-Type-Options:
  nosniff`, and a CSP whose `script-src` is the sha256 of the page's own
  inline block and nothing else, beside `connect-src 'self'`; the page loads
  nothing external.
- No cookie, no token, no credential stored anywhere. Identity is
  `identity.Provider`'s, per request.
"""

import ipaddress
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .. import advisory, transaction
from ..config import load_config
from ..links import load_links
from . import ui, wire
from .api import ApiError, Response, Workbench, utc_now
from .identity import (Forbidden, Principal, Provider, RequestContext,
                       TrustedOperator, Unidentified)
from .wire import BadRequest, WriteRequest

DEFAULT_BIND = "127.0.0.1:8765"
MAX_BODY = 1 << 20  # a command is a few kilobytes; a megabyte is not one
READ_TIMEOUT_S = 10.0

HEADERS: Mapping[str, str] = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; "
                                f"script-src {ui.script_src()}; "
                                "style-src 'unsafe-inline'; "
                                "connect-src 'self'; base-uri 'none'; "
                                "form-action 'none'"),
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class Request:
    """One HTTP request, already read. `headers` keys are lower-cased;
    `query` is the parsed query string (last value wins); `body` is raw bytes
    (empty for GET). `request_id` is minted per request and appears in the
    access log line and nowhere else: the run record already carries a
    `run_id`, and a second random field inside `facts` makes the audit stream
    harder to diff."""

    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes
    remote: str
    request_id: str


#: A route handler: the workbench, the identified caller, the request, the
#: path groups. Returns the response; raises `api.ApiError`,
#: `wire.BadRequest`, `identity.Forbidden` or `identity.Unidentified` for the
#: handler to render.
Handler = Callable[[Workbench, Principal, Request, Mapping[str, str]], Response]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern      # anchored; named groups become the handler's `match`
    name: str                # stable, appears in the access log
    handler: Handler


def _page(app, principal, req, m) -> Response:
    """`GET /` -> the workbench page (`ui.page()`), `text/html`."""
    return Response(200, ui.page(), "text/html; charset=utf-8")


def _queue(app, principal, req, m) -> Response:
    """`?all=1` -> resolved incidents too."""
    return Response(200, app.queue(principal, all_=req.query.get("all") == "1"))


def _incident(app, principal, req, m) -> Response:
    return Response(200, app.incident(principal, m["slug"]))


def _fleet(app, principal, req, m) -> Response:
    return Response(200, app.fleet(principal))


def _heat(app, principal, req, m) -> Response:
    """`?days=` names the span; an absent one is the default `heat` applies,
    and anything outside 1..90 is the 400 it raises."""
    return Response(200, app.heat(principal, req.query.get("days", "")))


def _database(app, principal, req, m) -> Response:
    """`?name=` names the database; an absent one is the 404 `database`
    raises for any name the snapshot does not hold."""
    return Response(200, app.database(principal, req.query.get("name", "")))


def _wikipage(app, principal, req, m) -> Response:
    """`?path=` names the page; a path the snapshot's inventory does not hold
    is the 404 `page` raises, which is also the traversal refusal."""
    return Response(200, app.page(principal, req.query.get("path", "")))


def _search(app, principal, req, m) -> Response:
    return Response(200, app.search(principal, req.query.get("q", "")))


def _runs(app, principal, req, m) -> Response:
    return Response(200, app.runs(principal))


def _agents(app, principal, req, m) -> Response:
    """`?hours=` names the window; an absent one is the default `agents`
    applies, and anything outside 1..720 is the 400 it raises, the `_heat`
    precedent."""
    return Response(200, app.agents(principal, req.query.get("hours", "")))


def _links(app, principal, req, m) -> Response:
    """No parameters: the whole board or the empty one."""
    return Response(200, app.link_board(principal))


def _run(app, principal, req, m) -> Response:
    """`?id=` names the run; an absent one is the 404 `run` raises for any id
    `.state/` does not hold, the `_database` precedent."""
    return Response(200, app.run(principal, req.query.get("id", "")))


def _preview(app, principal, req, m) -> Response:
    """`wire.decode_object` -> `WriteRequest.from_json` -> `app.preview`."""
    return app.preview(principal, m["slug"],
                       WriteRequest.from_json(wire.decode_object(req.body)))


def _commit(app, principal, req, m) -> Response:
    return app.commit(principal, m["slug"],
                      WriteRequest.from_json(wire.decode_object(req.body)))


def _tools(app, principal, req, m) -> Response:
    return Response(200, app.tools(principal, m["slug"]))


def _advisory_start(app, principal, req, m) -> Response:
    """`wire.decode_object` -> `ToolRequest.from_json` -> `advisory_start`,
    which answers 202 rather than 200: the record it carries is `queued` and
    the answer is not written yet."""
    return app.advisory_start(
        principal, m["slug"],
        wire.ToolRequest.from_json(wire.decode_object(req.body)))


def _advisory(app, principal, req, m) -> Response:
    """`?run=` names the run; an absent one is the 404 `advisory` raises for
    any id the runner does not hold, the `_run` precedent."""
    return Response(200, app.advisory(principal, req.query.get("run", "")))


def _inbox(app, principal, req, m) -> Response:
    return Response(200, app.inbox(principal))


def _review(app, principal, req, m) -> Response:
    """`?id=` names the week; an absent one is the 404 `review` raises for any
    id `.state/` does not hold, the `_run` precedent. A literal path with a
    query parameter and never a `{...}` template: `ui.check_page` substitutes
    only `{slug}`."""
    return Response(200, app.review(principal, req.query.get("id", "")))


def _inbox_act(app, principal, req, m) -> Response:
    """`wire.decode_object` -> `InboxRequest.from_json` -> `inbox_act`, which
    dispatches on the body's `action`, the `_advisory_start` precedent."""
    return app.inbox_act(
        principal, wire.InboxRequest.from_json(wire.decode_object(req.body)))


def _health(app, principal, req, m) -> Response:
    return Response(200, app.health())


#: The whole HTTP surface. Order matters only for readability; patterns are
#: anchored and disjoint. `ui.check_page` asserts every `api("METHOD",
#: "/path")` literal in the page names a row here, and that no row is dead.
ROUTES: tuple[Route, ...] = (
    Route("GET", re.compile(r"\A/\Z"), "page", _page),
    Route("GET", re.compile(r"\A/api/health\Z"), "health", _health),
    Route("GET", re.compile(r"\A/api/fleet\Z"), "fleet", _fleet),
    Route("GET", re.compile(r"\A/api/heat\Z"), "heat", _heat),
    Route("GET", re.compile(r"\A/api/db\Z"), "database", _database),
    Route("GET", re.compile(r"\A/api/page\Z"), "wikipage", _wikipage),
    Route("GET", re.compile(r"\A/api/search\Z"), "search", _search),
    Route("GET", re.compile(r"\A/api/links\Z"), "links", _links),
    Route("GET", re.compile(r"\A/api/runs\Z"), "runs", _runs),
    Route("GET", re.compile(r"\A/api/run\Z"), "run", _run),
    Route("GET", re.compile(r"\A/api/agents\Z"), "agents", _agents),
    Route("GET", re.compile(r"\A/api/advisory\Z"), "advisory", _advisory),
    Route("GET", re.compile(r"\A/api/inbox\Z"), "inbox", _inbox),
    Route("GET", re.compile(r"\A/api/review\Z"), "review", _review),
    Route("POST", re.compile(r"\A/api/inbox\Z"), "inbox_act", _inbox_act),
    Route("GET", re.compile(r"\A/api/incidents\Z"), "queue", _queue),
    Route("GET", re.compile(r"\A/api/incidents/(?P<slug>[^/]+)\Z"), "incident", _incident),
    Route("GET", re.compile(r"\A/api/incidents/(?P<slug>[^/]+)/tools\Z"), "tools", _tools),
    Route("POST", re.compile(r"\A/api/incidents/(?P<slug>[^/]+)/preview\Z"), "preview", _preview),
    Route("POST", re.compile(r"\A/api/incidents/(?P<slug>[^/]+)/commit\Z"), "commit", _commit),
    Route("POST", re.compile(r"\A/api/incidents/(?P<slug>[^/]+)/advisory\Z"), "advisory_start", _advisory_start),
)

#: Routes that skip the provider: the health probe has no browser behind it,
#: and `dbwiki health` must be able to ask whether the workbench is up
#: without an identity. Everything else is identified before it is routed.
ANONYMOUS = frozenset({"health"})

#: Refusal type -> status, for the exceptions that carry no status of their
#: own. `api.ApiError` brings its own, so it is absent here; all four carry a
#: stable `.error`, which is what lets `_refusal` render every one of them.
STATUS_OF: dict[type, int] = {BadRequest: 400, Unidentified: 401,
                              Forbidden: 403}


def route_for(method: str, path: str) -> tuple[Route, Mapping[str, str]] | None:
    """The matching route and its groups; None for 404. A path that matches a
    pattern under a different method is a 405, which the caller distinguishes
    by asking again with every method."""
    for route in ROUTES:
        if route.method != method:
            continue
        match = route.pattern.match(path)
        if match is not None:
            return route, match.groupdict()
    return None


def loopback(host: str) -> bool:
    """`127.0.0.0/8`, `::1` (bare or bracketed), or `localhost`."""
    text = host.strip()
    if text.startswith("["):
        text = text[1:].partition("]")[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _split_bind(text: str) -> tuple[str, int]:
    """`HOST:PORT` or `[V6]:PORT` -> the host without brackets and the port.
    A spelling with no port answers port 0, the ephemeral bind."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif ":" in text:
        host, _, port = text.rpartition(":")
    else:
        host, port = text, ""
    return host, int(port) if port else 0


def allowed_hosts(bind: str) -> frozenset[str]:
    """The `Host` values the server answers to: the bind itself plus every
    loopback spelling of the same port."""
    _, port = _split_bind(bind)
    return frozenset({bind, f"127.0.0.1:{port}", f"localhost:{port}",
                      f"[::1]:{port}"})


def _refusal(exc: Exception) -> Response:
    """Every refusal rendered by one line, whether it is a bad `Host` this
    module raised or a `Forbidden` raised four calls deep: each carries a
    stable `.error`, `ApiError` carries `.status`, and `ApiError` and
    `Forbidden` carry `.extra`."""
    return Response(getattr(exc, "status", None) or STATUS_OF[type(exc)],
                    wire.error_json(exc.error, str(exc),
                                    **getattr(exc, "extra", {})))


class _Handler(BaseHTTPRequestHandler):
    """One request per instance, one thread per request
    (`ThreadingHTTPServer`, daemon threads). Order per request:

    1. mint `request_id`;
    2. baseline: `Host` must be an `allowed_hosts` spelling and `Origin`,
       when present, `http://` one of them -> 403;
    3. for POST only: `Content-Type: application/json` -> 415,
       `Content-Length` over `MAX_BODY` -> 413, then exactly that many bytes
       under a socket read timeout, a short read being 400;
    4. `route_for` -> 404 / 405;
    5. `provider.identify(RequestContext)` unless the route is ANONYMOUS
       -> 401;
    6. the handler; render its `Response`, or a refusal through `_refusal`,
       or anything else as a 500 carrying the exception's type and message
       with the traceback on stderr and never a stack in the body;
    7. one access-log line through `server.log`: `<request_id> <method>
       <path> <status> <ms>`, never a body and never a header.

    The body read runs after the baseline, not before it, so a request from
    a page that has no business here is refused without this process reading
    a megabyte for it.

    `timeout` is the same constant the body read arms, so a client that opens
    a connection and then says nothing releases its thread rather than
    holding one for the life of the server.

    Nothing is written to the socket inside `incident_action.publish`'s
    recorder: a client that disconnects mid-response raises here, after the
    audit event has landed."""

    server_version = "dbwiki-portal"
    protocol_version = "HTTP/1.1"
    timeout = READ_TIMEOUT_S

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        request_id = secrets.token_hex(4)
        path, _, query = self.path.partition("?")
        unread = method == "POST"
        try:
            headers = {key.lower(): value
                       for key, value in self.headers.items()}
            hosts = allowed_hosts(self.server.app.bind)
            host = headers.get("host", "")
            if host not in hosts:
                raise ApiError(403, "bad_host",
                               f"{host!r} is not this workbench, which "
                               f"answers to {', '.join(sorted(hosts))}")
            origin = headers.get("origin")
            if origin is not None and origin not in {f"http://{h}"
                                                     for h in hosts}:
                raise ApiError(403, "bad_origin",
                               f"{origin} is not this workbench")

            body = b""
            if method == "POST":
                offered = headers.get("content-type", "")
                if offered.split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "json_only",
                                   f"this endpoint takes application/json, "
                                   f"not {offered!r}")
                declared = headers.get("content-length", "0")
                try:
                    length = int(declared)
                except ValueError:
                    raise ApiError(400, "bad_length",
                                   f"Content-Length {declared!r} is not a "
                                   f"number") from None
                if length > MAX_BODY:
                    raise ApiError(413, "too_large",
                                   f"{length} bytes is over the "
                                   f"{MAX_BODY}-byte body cap")
                self.connection.settimeout(READ_TIMEOUT_S)
                try:
                    body = self.rfile.read(length)
                except OSError as e:
                    raise ApiError(400, "short_body",
                                   f"the body did not arrive: {e}") from None
                if len(body) != length:
                    raise ApiError(400, "short_body",
                                   f"Content-Length announced {length} bytes "
                                   f"and {len(body)} arrived")
                unread = False

            found = route_for(method, path)
            if found is None:
                if any(route_for(row.method, path) for row in ROUTES):
                    raise ApiError(405, "method",
                                   f"{path} does not answer {method}")
                raise ApiError(404, "no_route", f"no route for {path}")
            route, groups = found

            principal = None
            if route.name not in ANONYMOUS:
                principal = self.server.provider.identify(RequestContext(
                    self.client_address[0], headers, request_id))

            req = Request(method=method, path=path,
                          query={key: values[-1]
                                 for key, values in parse_qs(query).items()},
                          headers=headers, body=body,
                          remote=self.client_address[0],
                          request_id=request_id)
            response = route.handler(self.server.app, principal, req, groups)
            payload = (response.body.encode()
                       if isinstance(response.body, str)
                       else wire.encode(response.body))
        except (ApiError, BadRequest, Forbidden, Unidentified) as exc:
            response = _refusal(exc)
            payload = wire.encode(response.body)
        except Exception as exc:  # noqa: BLE001 — a handler bug is a 500, never a dead server
            traceback.print_exc(file=sys.stderr)
            response = Response(500, wire.error_json(
                "internal", f"{type(exc).__name__}: {exc}"))
            payload = wire.encode(response.body)

        if unread:
            # the unread body is still in the socket, and the next request on
            # this keep-alive connection would parse that JSON as its request
            # line
            self.close_connection = True
        self.send_response(response.status)
        for key, value in HEADERS.items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

        if self.server.log is not None:
            self.server.log(f"{request_id} {method} {path} {response.status} "
                            f"{round((time.monotonic() - started) * 1000)}")

    def log_message(self, fmt, *args) -> None:
        """Replaced by the one-line access log above; `http.server`'s default
        prints the full request line for every static hit."""


class WorkbenchServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` carrying the `Workbench` and the provider, so
    the handler class needs no globals. `daemon_threads` so `shutdown()` does
    not wait on a hung push.

    Takes the config rather than a built `Workbench`, because it is the only
    thing that knows the bound port when `bind` names port 0, and so the only
    thing that can hand the `Workbench` a truthful `bind` string for
    `GET /api/health` and the `Host` check to compare against.

    `advisory.Runner.from_config` is built here, so a misconfigured
    `advisory:` block refuses to start the server rather than surfacing on an
    operator's first click. `load_links` is read here for both halves of that
    reason: a mistyped url stops the server, and the board a running portal
    serves is the one on disk when it started."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind: tuple[str, int], cfg, provider: Provider,
                 now: Callable[[], str] | None = None):
        super().__init__(bind, _Handler)
        host, port = self.server_address[0], self.server_address[1]
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.app = Workbench(cfg, provider, authority, now or utc_now,
                             runner=advisory.Runner.from_config(cfg),
                             board=load_links(cfg.root))
        self.provider = provider
        self.log = None


def make_server(cfg, *, bind: tuple[str, int] = ("127.0.0.1", 0),
                provider: Provider | None = None,
                now: Callable[[], str] | None = None,
                allow_remote: bool = False) -> WorkbenchServer:
    """The server without `serve_forever`, for tests: bind an ephemeral
    loopback port, run it in a daemon thread, `shutdown()` + `server_close()`
    in teardown. `provider` defaults to `TrustedOperator.from_config(cfg)`.
    Refuses a non-loopback `bind` unless `allow_remote` — the CLI's `--bind`
    sets it, an explicit operator choice; every caller who does not pass it
    (every test) keeps the loopback-only guarantee."""
    if not allow_remote and not loopback(bind[0]):
        raise ValueError(f"the workbench binds loopback only, not {bind[0]}")
    return WorkbenchServer(bind, cfg,
                           provider or TrustedOperator.from_config(cfg), now)


def serve(cfg, *, bind: str | None = None) -> int:
    """`dbwiki portal serve`: resolve the operator first, so a portal with no
    attributable actor refuses to start (exit 2) rather than serving a page
    that fails on the first commit; bind `bind or cfg.portal["bind"] or
    DEFAULT_BIND`; print one line naming the URL and the actor; then
    `serve_forever` until SIGTERM or SIGINT calls `shutdown()`. Exit 0.

    Never takes the wiki lock and is never in `cli.LOCKED`: `_with_lock`
    holds the flock around the whole of `args.fn(args)`, and a server's `fn`
    returns when the process dies. The lock is taken per commit, inside
    `incident_action.publish`, capped by `portal.lock_wait_s`."""
    try:
        provider = TrustedOperator.from_config(cfg)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    host, port = _split_bind(bind or cfg.portal["bind"] or DEFAULT_BIND)
    if not loopback(host):
        print(f"warning: binding non-loopback ({host}) exposes the "
              f"workbench to every host that can reach it, all writing as "
              f"{provider.identify(RequestContext('', {}, '')).actor.email}",
              file=sys.stderr)

    try:
        revision = transaction.head(cfg.wiki_repo)
    except RuntimeError as e:
        print(f"the wiki at {cfg.wiki_repo} has no HEAD to serve: {e}",
              file=sys.stderr)
        return 2

    server = make_server(cfg, bind=(host, port), provider=provider,
                         allow_remote=True)
    server.log = lambda line: print(line, file=sys.stderr, flush=True)
    actor = provider.identify(RequestContext("", {}, "")).actor.email
    print(f"workbench at http://{server.app.bind} as {actor} "
          f"(wiki {revision[:7]}, "
          f"push {'on' if cfg.portal['push'] else 'off'})", flush=True)

    def stop(signum, frame) -> None:
        # shutdown() blocks until serve_forever returns, so calling it on the
        # serving thread the signal interrupts would deadlock
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
    except ValueError:
        pass

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def cmd_portal_serve(args) -> int:
    """`load_config()` once (cwd-rooted, like every command; the systemd unit
    sets `WorkingDirectory`) and `serve(cfg, bind=args.bind)`."""
    return serve(load_config(), bind=args.bind)


def add_portal_parser(sub) -> None:
    """`dbwiki portal serve [--bind HOST:PORT]`. Registered from inside
    `cli.main()`, the `incident` precedent, so `cli` never imports
    `http.server` at module scope. Takes no `--lock-wait`: the server holds
    the lock per commit with `portal.lock_wait_s`, and never inherits
    `DBWIKI_LOCK_WAIT`, whose ambient hour would turn a click during a tick
    into an hour-long hang instead of a 423."""
    p = sub.add_parser("portal", help="the incident workbench over HTTP")
    verbs = p.add_subparsers(dest="portal_cmd", required=True)
    s = verbs.add_parser("serve", help="serve the workbench (loopback by "
                                        "default; --bind can widen it)")
    s.add_argument("--bind", metavar="HOST:PORT",
                   help=f"default portal.bind, then {DEFAULT_BIND}")
    s.set_defaults(fn=cmd_portal_serve)
