"""Builder for incident page text, so a test states only the fields it cares
about and every other test moves with the writer when the page shape changes."""


def incident_page(db: str, title: str, *, status: str = "open",
                  opened: str = "2026-07-01T00:00:00Z", updated: str = "",
                  body: str = "", error_codes: tuple[str, ...] = ()) -> str:
    text = (f"---\ntype: incident\nstatus: {status}\ndb: {db}\n"
            f"opened: {opened}\n"
            + (f"updated: {updated}\n" if updated else "")
            + f"---\n\n# {title}\n")
    if body:
        text += f"\n{body}\n"
    if error_codes:
        bullets = "".join(f"- [[errors/{c}]]\n" for c in error_codes)
        text += f"\n## Errors\n\n{bullets}"
    return text
