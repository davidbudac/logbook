Reply with ONE JSON object and NOTHING else: no prose
before or after it, no markdown code fences, no explanation.

{
  "schema_version": 1,
  "summary": "one line, at most 200 characters",
  "overview": "2-5 sentences of plain prose about this window; no headings, no bullets",
  "items": [{"db": "<database>", "status_line": "one line: what this database did"}],
  "open_incident_notes": [{"incident": 1, "note": "one line: what this window adds"}],
  "notable_analysis": [{"db": "<database>", "analysis": "3-10 sentences of markdown prose"}],
  "flags": ["anything a human should look at"]
}
