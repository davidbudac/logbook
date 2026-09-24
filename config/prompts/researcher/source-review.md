TASK: review the approved source `{{slug}}` (url: {{url}}; domains: {{domains}}). It was last reviewed on {{last_reviewed}}. Previous notes:
{{previous_notes}}

Check whether the site is still live, still authoritative for Oracle Database error/administration content, and still where the domains say. Then write `.agent-result.json` exactly:
{"task": "research", "kind": "source-review", "slug": "{{slug}}", "still_valid": true|false, "notes": "<=2000 chars", "checked": "{{today}}", "proposed_status": "approved|deprecated" (optional; only when you recommend a change)}
