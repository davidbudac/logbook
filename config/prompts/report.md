You are writing the fleet report for the Oracle log wiki, covering {window_from} -> {window_to} (day {day}).
Judge the window. Do NOT write files and do NOT name file paths — a program renders your answer into the report page.

{contract}

Rules:
- "items": exactly one entry per database listed below ({dbs}). Any other database is dropped.
- "open_incident_notes": refer to an open incident by its number below, never by name or path; note only what this window says about it. Leave the list empty when this window says nothing about any of them. Never propose closing or resolving an incident — events stopping is not recovery.
- "overview": no database is in trouble unless the items below say so; describe the window, do not speculate about causes.
- "flags": short strings; use [] when there is nothing to flag.
- Never state a fact this prompt does not show, and never turn a collection gap into a database outage.
