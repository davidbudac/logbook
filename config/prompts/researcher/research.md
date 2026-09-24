TASK: research Oracle error {{code}}. Read the approved documentation for it (Oracle's error-help page for {{code}} is the natural start), consider the estate facts and the redacted synopsis in request.json (message samples, co-occurring codes, occurrence counts, current reference text), and produce:
- cause: plain factual prose, what the error means and what causes it in a setup like the one described; no markdown headings, no URLs, at most {{max_chars}} characters;
- action: plain factual prose (a short "- " bulleted list as one string is fine), what to check and do; same limits;
- references: one object per page you actually read and relied on: {"source": <slug>, "url": <exact URL>, "accessed": "{{today}}"} (at most {{max_refs}}; every claim must be traceable to one of them);
- related_codes: other error codes the sources tie to this one (may be empty);
- flags: free-text notes for a human, e.g. "propose source: <domain>" if a valuable page sits on an unapproved site (may be empty).

Never restate the occurrences back, never invent a step the sources do not support, never mention pseudonyms you did not need.

Write the result to `.agent-result.json` in this directory, exactly:
{"task": "research", "kind": "research", "code": "{{code}}", "cause": "...", "action": "...", "references": [...], "related_codes": [...], "flags": [...]}
