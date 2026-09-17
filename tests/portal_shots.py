"""PNGs of the workbench, for looking at. Not a test.

Drives the same headless Chrome and the same `window.fetch` shim the browser
walks in `test_portal_ui.py` use, so what is photographed is the page as the
walks prove it, not a hand-built mock of it. Run it:

    .venv/bin/python -m tests.portal_shots [out_dir]

Every shot is full-page. The dark ones ask Chrome for the media feature
rather than for a class, because `prefers-color-scheme` is the only thing the
stylesheet reads.
"""

import base64
import json
import pathlib
import sys
import tempfile

from tests import test_portal_ui as walk


def shoot(page, path):
    """One full-page PNG.

    The viewport is grown to the document rather than asking for
    `captureBeyondViewport`: the top bar and the stage's foot are sticky, and
    a capture past the window's own height leaves both of them stranded
    wherever the scroll position happened to be.
    """
    page.eval("window.scrollTo(0, 0)")
    tall = page.eval("Math.ceil(document.documentElement.scrollHeight) + 20")
    page.send("Emulation.setDeviceMetricsOverride",
              {"width": 1280, "height": tall, "deviceScaleFactor": 1,
               "mobile": False})
    shot = page.send("Page.captureScreenshot", {"format": "png"})
    page.send("Emulation.clearDeviceMetricsOverride")
    path.write_bytes(base64.b64decode(shot["data"]))
    print(path)


def scheme(page, name):
    page.send("Emulation.setEmulatedMedia",
              {"features": [{"name": "prefers-color-scheme", "value": name}]})


def lens(page, facet):
    page.click(f'#lens-bar button[data-facet="{facet}"]')
    page.until(f"STATE.lens.facet === {json.dumps(facet)}")


def span(page, days):
    page.click(f'#heat-days button[data-days="{days}"]')
    page.until(f"STATE.heat.data && STATE.heat.data.days.length === {days}")


def at(page, hash_, screen):
    """The screen an address opens, waited for by name rather than by a
    request count: some screens fetch more than one thing."""
    page.eval("location.hash = " + json.dumps(hash_))
    page.until(f"STATE.screen === {json.dumps(screen)}")


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="workbench-shots-"))
    board = walk.mock_page(tmp, "board", tools=walk.ASK_TOOLS,
                           starts=walk.ASK_STARTS, polls=walk.ASK_POLLS,
                           fleet=walk.BIG_FLEET, runs=walk.BIG_RUNS)
    with walk.Chrome() as page:
        page.goto(board.as_uri())
        page.until("STATE.screen === 'queue'")
        page.until("STATE.heat.data !== null")
        for facet in walk.FACET_NAMES:
            lens(page, facet)
            shoot(page, out / f"queue-{facet}.png")

        lens(page, "db")
        shoot(page, out / "heat-30.png")
        span(page, 90)
        shoot(page, out / "heat-90.png")
        span(page, 30)

        scheme(page, "dark")
        shoot(page, out / "queue-dark.png")
        shoot(page, out / "heat-dark.png")
        scheme(page, "light")

        page.click("#nav-fleet")
        page.until("STATE.screen === 'fleet'")
        shoot(page, out / "fleet.png")

        page.click("#nav-agents")
        page.until("STATE.screen === 'agents'")
        shoot(page, out / "agents.png")
        scheme(page, "dark")
        shoot(page, out / "agents-dark.png")
        scheme(page, "light")

        page.click("#nav-inbox")
        page.until("STATE.screen === 'inbox'")
        shoot(page, out / "inbox.png")
        scheme(page, "dark")
        shoot(page, out / "inbox-dark.png")
        scheme(page, "light")

        at(page, "#/review/" + walk.REVIEW_ID, "review")
        shoot(page, out / "review.png")
        scheme(page, "dark")
        shoot(page, out / "review-dark.png")
        scheme(page, "light")

        page.click("#nav-runs")
        page.until("STATE.screen === 'runs'")
        shoot(page, out / "runs.png")
        scheme(page, "dark")
        shoot(page, out / "runs-dark.png")
        scheme(page, "light")

        at(page, "#/run/" + walk.RUN_ID, "run")
        shoot(page, out / "run.png")

        at(page, "#/db/" + walk.DB_NAME, "db")
        shoot(page, out / "db.png")
        scheme(page, "dark")
        shoot(page, out / "db-dark.png")
        scheme(page, "light")

        at(page, "#/page/" + walk.PAGE_PATH, "wikipage")
        shoot(page, out / "wikipage.png")

        at(page, "#/search/" + walk.SEARCH["query"], "search")
        shoot(page, out / "search.png")

        page.click("#nav-links")
        page.until("STATE.screen === 'links'")
        shoot(page, out / "links.png")
        scheme(page, "dark")
        shoot(page, out / "links-dark.png")
        scheme(page, "light")

        page.click("#nav-queue")
        page.until("STATE.screen === 'queue'")
        walk.open_incident(page)
        shoot(page, out / "incident.png")

        page.click("#tools summary")
        page.until("STATE.tools !== null")
        shoot(page, out / "incident-assist.png")

        walk.open_ask(page)
        walk.ask_question(page, "Why is the window still not met?")
        page.until("STATE.thread[0].run.status === 'succeeded'", timeout=20)
        shoot(page, out / "interview.png")

        scheme(page, "dark")
        shoot(page, out / "interview-dark.png")

    quiet = walk.mock_page(tmp, "quiet", queue=walk.QUIET_QUEUE,
                           queue_all=walk.QUIET_QUEUE)
    with walk.Chrome() as page:
        page.goto(quiet.as_uri())
        page.until("STATE.screen === 'queue'")
        page.until("STATE.heat.data !== null")
        shoot(page, out / "quiet-queue.png")
        scheme(page, "dark")
        shoot(page, out / "quiet-queue-dark.png")
        scheme(page, "light")

        page.click("#lens-quiet")
        page.until("STATE.lens.quiet === true")
        shoot(page, out / "quiet-only.png")
        scheme(page, "dark")
        shoot(page, out / "quiet-only-dark.png")

    crowd = walk.mock_page(tmp, "crowd", queue=walk.CROWD_QUEUE,
                           queue_all=walk.CROWD_QUEUE)
    with walk.Chrome() as page:
        page.goto(crowd.as_uri())
        page.until("STATE.screen === 'queue'")
        page.until("STATE.heat.data !== null")
        shoot(page, out / "crowd-queue.png")

    crowded = walk.mock_page(tmp, "crowded", agents=walk.CROWDED_AGENTS)
    with walk.Chrome() as page:
        page.goto(crowded.as_uri() + "#/agents")
        page.until("STATE.screen === 'agents'")
        shoot(page, out / "crowded-agents.png")

    week = walk.mock_page(tmp, "bigweek", week=walk.BIG_REVIEW)
    with walk.Chrome() as page:
        page.goto(week.as_uri() + "#/review/" + walk.BIG_REVIEW_ID)
        page.until("STATE.screen === 'review'")
        shoot(page, out / "review-big.png")
        scheme(page, "dark")
        shoot(page, out / "review-big-dark.png")

    weeks = walk.mock_page(tmp, "biginbox", inbox=walk.BIG_INBOX,
                           week=dict(walk.REVIEW,
                                     review_id=walk.BIG_INBOX_ID))
    with walk.Chrome() as page:
        page.goto(weeks.as_uri() + "#/inbox")
        page.until("STATE.screen === 'inbox'")
        shoot(page, out / "inbox-big.png")
        scheme(page, "dark")
        shoot(page, out / "inbox-big-dark.png")

    big = walk.mock_page(tmp, "bigdb", db=walk.BIG_DB, heat=walk.BIG_HEAT)
    with walk.Chrome() as page:
        page.goto(big.as_uri() + "#/db/" + walk.DB_NAME)
        page.until("STATE.screen === 'db'")
        page.until("STATE.heat30.data !== null")
        shoot(page, out / "db-big.png")
        scheme(page, "dark")
        shoot(page, out / "db-big-dark.png")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "shots"))
