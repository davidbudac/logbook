#!/usr/bin/env bash
# Serve wiki/html (the daily DBA pages) read-only over the tailscale network.
#
# Usage: deploy/serve_wiki_html.sh [BIND] [PORT]
#   BIND defaults to this host's tailscale IPv4 (192.0.2.10 on dbhost),
#   PORT to 8766, one above the workbench. Run it by hand or through
#   deploy/dbwiki-html.service. It writes nothing and needs no config.
#
# At boot tailscaled may not have an address yet, and a user unit cannot
# order itself after the system's network-online.target, so with no BIND the
# script asks `tailscale ip -4` every DBWIKI_HTML_WAIT_S seconds (default 2),
# DBWIKI_HTML_WAIT_TRIES times (default 30), and then exits 1 rather than
# serving on 127.0.0.1, where nobody on tailscale could ever reach it; the
# unit's Restart=on-failure tries again. To serve on loopback, say so:
# `deploy/serve_wiki_html.sh 127.0.0.1`.
#
# The pages are plain static HTML the report loop regenerates every tick, so
# python's http.server is enough: no framework, no auth, no state. Do not
# expose this beyond tailscale; the pages name databases and error codes.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$repo/wiki/html"
port="${2:-${DBWIKI_HTML_PORT:-8766}}"

[ -d "$dir" ] || { echo "no $dir; is the wiki checked out?" >&2; exit 1; }

bind="${1:-${DBWIKI_HTML_BIND:-}}"
if [ -z "$bind" ]; then
  tries="${DBWIKI_HTML_WAIT_TRIES:-30}"
  wait_s="${DBWIKI_HTML_WAIT_S:-2}"
  for ((try = 1; try <= tries; try++)); do
    bind="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    if [ -n "$bind" ]; then break; fi
    if [ "$try" -lt "$tries" ]; then sleep "$wait_s"; fi
  done
  if [ -z "$bind" ]; then
    echo "no tailscale IPv4 after $tries tries; is tailscaled up?" \
         "Not falling back to 127.0.0.1: pass a BIND to serve elsewhere." >&2
    exit 1
  fi
fi

echo "wiki/html at http://$bind:$port/ (index.html is the latest day)"
exec python3 -m http.server "$port" --bind "$bind" --directory "$dir"
