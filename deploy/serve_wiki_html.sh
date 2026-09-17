#!/usr/bin/env bash
# Serve wiki/html (the daily DBA pages) read-only over the tailscale network.
#
# Usage: deploy/serve_wiki_html.sh [BIND] [PORT]
#   BIND defaults to this host's tailscale IPv4 (192.0.2.10 on dbhost),
#   PORT to 8766, one above the workbench. Run it by hand or through
#   deploy/dbwiki-html.service. It writes nothing and needs no config.
#
# The pages are plain static HTML the report loop regenerates every tick, so
# python's http.server is enough: no framework, no auth, no state. Do not
# expose this beyond tailscale; the pages name databases and error codes.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$repo/wiki/html"
bind="${1:-${DBWIKI_HTML_BIND:-$(tailscale ip -4 2>/dev/null || echo 127.0.0.1)}}"
port="${2:-${DBWIKI_HTML_PORT:-8766}}"

[ -d "$dir" ] || { echo "no $dir; is the wiki checked out?" >&2; exit 1; }

echo "wiki/html at http://$bind:$port/ (index.html is the latest day)"
exec python3 -m http.server "$port" --bind "$bind" --directory "$dir"
