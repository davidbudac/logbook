You are ingesting one daily Oracle log digest for database {{db}} (day {{day}}) into an operations wiki.
Judge it. Do NOT write files and do NOT name file paths — a program applies your answer.

Reply with ONE JSON object and NOTHING else: no prose before or
after it, no markdown code fences, no explanation.

{
  "schema_version": 1,
  "summary": "one line, at most 200 characters; a longer one is cut at a word and flagged",
  "notable": true or false,
  "journal_entry": "2-6 sentences of plain prose; no headings, no bullets",
  "error_updates": [{"code": "ORA-00600", "note": "what this code did today"}],
  "incident": {"action": "none" or "open" or "update",
               "slug": "short-kebab-slug; REQUIRED when action is open, else null",
               "title": "one-line incident title; REQUIRED when action is open, else null",
               "body": "incident prose; REQUIRED when action is open or update, never null for those",
               "existing_page": "incidents/<file>.md; REQUIRED when action is update, else null"},
  "flags": ["anything a human should look at"]
}

With "action": "none", every other incident field is null. Do not choose
"open" or "update" unless you are also writing the fields they require.

Rules:
- "notable": true only when something happened that an on-call DBA would want to know about.
- "error_updates": only codes from this digest ({{codes}}). Any other code is dropped.
- "incident": "open" only when the evidence shows a real problem and no open incident below already covers it; "update" to add today's evidence to one of them (copy its path into "existing_page"); otherwise "none". Never propose closing or resolving an incident.
- "flags": short strings; use [] when there is nothing to flag.
