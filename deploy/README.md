# Deploying the incident workbench

**Status: one deployment's units.** Every path below is absolute and belongs to
the machine this was written on; adapt the paths and the user before use.

`dbwiki-portal.service` runs `uv run dbwiki portal serve` as a systemd **user**
service on the machine that holds the checkout. It is the browser front end for
incident work. The `dbwiki incident` CLI does the same work from a terminal and
is the fallback when the service is down (`docs/monitoring.md`, the "Portal
down" runbook entry).

Every path in the unit is absolute and specific to this machine. On another
host, edit `WorkingDirectory` and `ExecStart` before you install it.

## Install

```sh
cp deploy/dbwiki-portal.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dbwiki-portal
```

`enable --now` starts the service and starts it again at every login. To keep
it running after you log out and to start it at boot, the user manager needs
lingering:

```sh
loginctl enable-linger <user>
loginctl show-user db -p Linger       # Linger=yes
```

It is already `Linger=yes` for `db` on this host, so there is nothing to do
here.

## Check that it worked

```sh
systemctl --user status dbwiki-portal
journalctl --user -u dbwiki-portal -f
```

The journal carries the startup line, then one line per request (request id,
method, path, status, milliseconds) and nothing else. Never a body, never a
header. The startup line names the URL and the actor:

```
workbench at http://127.0.0.1:8765 as dba@example.com (wiki 6765a05, push on)
```

Then open http://127.0.0.1:8765 in a browser. Use that address rather than
`localhost`. The server binds `127.0.0.1`, and a browser that resolves
`localhost` to `::1` fails to connect before any check runs.

## Reaching it by another name

The workbench answers only to the `Host` it was bound to and that port's
loopback spellings (any case), so a DNS-rebinding page cannot reach it
through a name it resolved. To open it by a host name, a LAN or tailscale
address, or through a reverse proxy, list those names in
`portal.allowed_hosts` (see `config/dbwiki.yaml.example`): a bare name
matches on any port, `name:port` on that port only, and a TLS proxy's
`Origin: https://wiki.example.com` is `wiki.example.com:443`. A proxy that
rewrites `Host` to `127.0.0.1:8765` goes in `portal.trusted_proxies` (its IP),
and then its first `X-Forwarded-Host` is the name checked. A wildcard or a URL
in either list stops the server at start. Binding non-loopback with no
`allowed_hosts` prints a warning, because every browser would get 403
`bad_host`. The portal authenticates nobody; the proxy has to.

## What `dbwiki health` says

`dbwiki health` reports the portal only when `config/dbwiki.yaml` has a
`portal:` block. `portal.bind` has a default of its own, so probing on that
default alone would report every install that never wanted a workbench as
having a dead one. Once the config names one, the `dependencies` block of the
health output gains a line:

```
  portal      up http://127.0.0.1:8765/api/health [wiki 6765a05 as dba@example.com]
```

The unreachable case, the restart, and the fallback are the "Portal down"
runbook entry in `docs/monitoring.md`. The service takes the wiki lock per commit rather than
for its lifetime. `docs/scheduling.md` explains what that means for a click
that meets a tick, under "One command at a time".

## The daily pages over tailscale (optional)

`wiki/html/` is the daily DBA page, rendered by the report loop. The workbench
does not serve it, and the GitHub link base shows the file as source. To give
it a URL on the tailscale network, `deploy/serve_wiki_html.sh` runs python's
`http.server` on the host's tailscale address, port 8766, read-only:

```sh
deploy/serve_wiki_html.sh                     # by hand; Ctrl-C stops it
cp deploy/dbwiki-html.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dbwiki-html     # as a service
```

Then http://192.0.2.10:8766/ is the latest day and
http://192.0.2.10:8766/2026-09-09.html one specific day. Nothing is
installed until someone runs the lines above; the script writes nothing and
needs no config. Keep it on tailscale: the pages name databases and codes.

At boot tailscaled may not have an address yet, and a user unit cannot wait
for the system's `network-online.target`. With no BIND the script asks
`tailscale ip -4` every 2 s, 30 times (`DBWIKI_HTML_WAIT_S`,
`DBWIKI_HTML_WAIT_TRIES`), then exits 1 and the unit restarts it. It never
falls back to 127.0.0.1 on its own; `deploy/serve_wiki_html.sh 127.0.0.1`
serves on loopback when that is what you want.
