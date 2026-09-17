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
