#!/usr/bin/env python3
"""Generate dbwiki-dashboards.ndjson (Kibana saved objects). Stdlib only.

Hand-maintaining Lens saved objects as raw ndjson is unreadable; this script
is the source of truth and setup.sh regenerates + imports the ndjson on every
run. Objects use fixed ids (dbwiki-*) so re-imports overwrite in place.

Layout: three dashboards (pipeline observability, agent runs, loop overview),
ten data views (one per logs-dbwiki.* data stream), 30 Lens panels and six
saved searches.
"""

import json

DV_RUN = "dbwiki-dv-run-health"
DV_AGENT = "dbwiki-dv-agent-runs"
DV_DBRUNS = "dbwiki-dv-db-runs"
DV_LEDGER = "dbwiki-dv-ledger"
DV_CRON = "dbwiki-dv-cron"
DV_AUDIT = "dbwiki-dv-audit"
DV_RUN_STARTS = "dbwiki-dv-run-starts"
DV_SCHEDULE = "dbwiki-dv-schedule"
DV_QUEUE_STATE = "dbwiki-dv-queue-state"
DV_ALERTS = "dbwiki-dv-alerts"
# Every dbwiki stream at once: the workbench's "run's ELK history" deep link
# filters this by run_id (config/dbwiki.yaml portal.links.kibana.data_views).
DV_ALL = "dbwiki-dv-all"

LAYER = "layer1"
LAYER_REF = f"indexpattern-datasource-layer-{LAYER}"


def data_view(dv_id: str, name: str, title: str) -> dict:
    return {
        "id": dv_id,
        "type": "index-pattern",
        "attributes": {"title": title, "name": name, "timeFieldName": "@timestamp"},
        "references": [],
    }


# ---- Lens column builders ----------------------------------------------------

def col_date(interval: str = "auto") -> dict:
    return {
        "label": "@timestamp",
        "dataType": "date",
        "operationType": "date_histogram",
        "sourceField": "@timestamp",
        "isBucketed": True,
        "scale": "interval",
        "params": {"interval": interval, "includeEmptyRows": True, "dropPartials": False},
    }


def col_count(label: str = "Count", kql: str | None = None) -> dict:
    col = {
        "label": label,
        "dataType": "number",
        "operationType": "count",
        "isBucketed": False,
        "scale": "ratio",
        "sourceField": "___records___",
        # a zero count renders as 0, not N/A ("Problem runs" is usually 0)
        "params": {"emptyAsNull": False},
    }
    if kql:
        col["filter"] = {"query": kql, "language": "kuery"}
    return col


def col_metric(op: str, field: str, label: str | None = None) -> dict:
    return {
        "label": label or f"{op} of {field}",
        "dataType": "number",
        "operationType": op,
        "sourceField": field,
        "isBucketed": False,
        "scale": "ratio",
        "params": {"emptyAsNull": False},
    }


def col_terms(field: str, order_by_col: str, size: int = 5,
              label: str | None = None, other: bool = False,
              order_dir: str = "desc") -> dict:
    return {
        "label": label or field,
        "dataType": "string",
        "operationType": "terms",
        "scale": "ordinal",
        "sourceField": field,
        "isBucketed": True,
        "params": {
            "size": size,
            "orderBy": {"type": "column", "columnId": order_by_col},
            "orderDirection": order_dir,
            "otherBucket": other,
            "missingBucket": False,
            "parentFormat": {"id": "terms"},
        },
    }


def col_last_value(field: str, data_type: str = "string", label: str | None = None,
                    sort_field: str = "@timestamp", kql: str | None = None) -> dict:
    col = {
        "label": label or f"last {field}",
        "dataType": data_type,
        "operationType": "last_value",
        "sourceField": field,
        "isBucketed": False,
        "scale": "ratio" if data_type == "number" else "ordinal",
        "params": {"sortField": sort_field},
    }
    if kql:
        col["filter"] = {"query": kql, "language": "kuery"}
    return col


# ---- Lens saved-object builders ---------------------------------------------

def _lens(so_id: str, title: str, dv_id: str, vis_type: str,
          visualization: dict, columns: dict, column_order: list) -> dict:
    return {
        "id": so_id,
        "type": "lens",
        "attributes": {
            "title": title,
            "description": "",
            "visualizationType": vis_type,
            "state": {
                "visualization": visualization,
                "query": {"query": "", "language": "kuery"},
                "filters": [],
                "datasourceStates": {
                    "formBased": {
                        "layers": {
                            LAYER: {
                                "columns": columns,
                                "columnOrder": column_order,
                                "incompleteColumns": {},
                            }
                        }
                    }
                },
            },
        },
        "references": [{"type": "index-pattern", "id": dv_id, "name": LAYER_REF}],
    }


def lens_metric(so_id: str, title: str, dv_id: str, metric_col: dict) -> dict:
    vis = {"layerId": LAYER, "layerType": "data", "metricAccessor": "m1"}
    return _lens(so_id, title, dv_id, "lnsMetric", vis, {"m1": metric_col}, ["m1"])


def lens_donut(so_id: str, title: str, dv_id: str, slice_field: str,
               size: int = 8, kql: str | None = None) -> dict:
    cols = {
        "m1": col_count(kql=kql),
        "b1": col_terms(slice_field, "m1", size=size),
    }
    vis = {
        "shape": "donut",
        "layers": [{
            "layerId": LAYER,
            "primaryGroups": ["b1"],
            "metrics": ["m1"],
            "numberDisplay": "value",
            "categoryDisplay": "default",
            "legendDisplay": "default",
            "nestedLegend": False,
            "layerType": "data",
        }],
    }
    return _lens(so_id, title, dv_id, "lnsPie", vis, cols, ["b1", "m1"])


def lens_xy(so_id: str, title: str, dv_id: str, series_type: str,
            columns: dict, x: str | None, split: str | None,
            accessors: list) -> dict:
    layer = {
        "layerId": LAYER,
        "accessors": accessors,
        "position": "top",
        "seriesType": series_type,
        "showGridlines": False,
        "layerType": "data",
    }
    if x:
        layer["xAccessor"] = x
    if split:
        layer["splitAccessor"] = split
    order = [c for c in (x, split) if c] + accessors
    vis = {
        "legend": {"isVisible": True, "position": "right"},
        "valueLabels": "hide",
        "fittingFunction": "None",
        "preferredSeriesType": series_type,
        "layers": [layer],
    }
    return _lens(so_id, title, dv_id, "lnsXY", vis, columns, order)


def lens_table(so_id: str, title: str, dv_id: str,
               columns: dict, column_order: list) -> dict:
    vis = {
        "layerId": LAYER,
        "layerType": "data",
        "columns": [{"columnId": c, "isTransposed": False} for c in column_order],
    }
    return _lens(so_id, title, dv_id, "lnsDatatable", vis, columns, column_order)


def saved_search(so_id: str, title: str, dv_id: str, columns: list,
                 kql: str = "") -> dict:
    search_source = {
        "query": {"query": kql, "language": "kuery"},
        "filter": [],
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
    }
    return {
        "id": so_id,
        "type": "search",
        "attributes": {
            "title": title,
            "description": "",
            "columns": columns,
            "sort": [["@timestamp", "desc"]],
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(search_source)},
        },
        "references": [{
            "type": "index-pattern",
            "id": dv_id,
            "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
        }],
    }


# ---- Panels ------------------------------------------------------------------

def build_objects() -> list:
    objects = [
        data_view(DV_RUN, "dbwiki run health", "logs-dbwiki.run_health*"),
        data_view(DV_AGENT, "dbwiki agent runs", "logs-dbwiki.agent_runs*"),
        data_view(DV_DBRUNS, "dbwiki per-db runs", "logs-dbwiki.db_runs*"),
        data_view(DV_LEDGER, "dbwiki ledger", "logs-dbwiki.ledger*"),
        data_view(DV_CRON, "dbwiki cron output", "logs-dbwiki.cron*"),
        data_view(DV_AUDIT, "dbwiki agent audit", "logs-dbwiki.audit*"),
        data_view(DV_RUN_STARTS, "dbwiki run starts", "logs-dbwiki.run_starts*"),
        data_view(DV_SCHEDULE, "dbwiki schedule", "logs-dbwiki.schedule*"),
        data_view(DV_QUEUE_STATE, "dbwiki queue state", "logs-dbwiki.queue_state*"),
        data_view(DV_ALERTS, "dbwiki alerts", "logs-dbwiki.alerts*"),
        data_view(DV_ALL, "dbwiki all streams", "logs-dbwiki.*"),
    ]

    objects.append(lens_metric(
        "dbwiki-lens-runs-total", "Command runs", DV_RUN, col_count("Runs")))
    objects.append(lens_metric(
        "dbwiki-lens-runs-failed", "Failed runs", DV_RUN,
        col_count("Failed runs", kql='outcome : "failed"')))
    objects.append(lens_donut(
        "dbwiki-lens-outcomes", "Run outcomes", DV_RUN, "outcome", size=6))
    objects.append(lens_donut(
        "dbwiki-lens-error-categories", "Error categories", DV_RUN,
        "error_category", size=10))

    objects.append(lens_xy(
        "dbwiki-lens-runs-over-time", "Runs over time by outcome", DV_RUN,
        "bar_stacked",
        {"x1": col_date(), "s1": col_terms("outcome", "m1", size=6), "m1": col_count("Runs")},
        x="x1", split="s1", accessors=["m1"]))
    objects.append(lens_xy(
        "dbwiki-lens-run-duration", "Run duration by command (avg s)", DV_RUN,
        "line",
        {"x1": col_date(), "s1": col_terms("command", "m1", size=8),
         "m1": col_metric("average", "duration_s", "avg duration (s)")},
        x="x1", split="s1", accessors=["m1"]))

    objects.append(lens_xy(
        "dbwiki-lens-decisions-per-db", "Wake vs skip per database", DV_DBRUNS,
        "bar_horizontal_stacked",
        {"x1": col_terms("db", "m1", size=20, label="database"),
         "s1": col_terms("decision", "m1", size=4),
         "m1": col_count("Ticks")},
        x="x1", split="s1", accessors=["m1"]))
    objects.append(lens_donut(
        "dbwiki-lens-decision-reasons", "Decision reasons", DV_DBRUNS,
        "decision_reasons", size=10))
    objects.append(lens_xy(
        "dbwiki-lens-events-over-time", "Events vs notable events", DV_DBRUNS,
        "area",
        {"x1": col_date(),
         "m1": col_metric("sum", "events", "events"),
         "m2": col_metric("sum", "notable_events", "notable events")},
        x="x1", split=None, accessors=["m1", "m2"]))

    objects.append(lens_xy(
        "dbwiki-lens-agent-durations", "Agent stage duration (avg s)", DV_DBRUNS,
        "bar",
        {"x1": col_terms("telemetry.task", "m1", size=8, label="task"),
         "m1": col_metric("average", "telemetry.duration_s", "avg duration (s)")},
        x="x1", split=None, accessors=["m1"]))
    objects.append(lens_donut(
        "dbwiki-lens-ledger-status", "Ledger: digest statuses", DV_LEDGER,
        "status", size=8))
    objects.append(lens_table(
        "dbwiki-lens-ledger-table", "Ledger: events per digest", DV_LEDGER,
        {"b1": col_terms("digest_path", "m1", size=15, label="digest"),
         "b2": col_terms("status", "m1", size=4, label="status"),
         "m1": col_count("events")},
        ["b1", "b2", "m1"]))

    objects.append(saved_search(
        "dbwiki-search-cron", "Cron log (raw)", DV_CRON,
        ["log.level", "dbwiki.db", "dbwiki.decision", "dbwiki.reason", "message"]))
    objects.append(saved_search(
        "dbwiki-search-audit", "Agent audit trail", DV_AUDIT,
        ["dbwiki.task", "dbwiki.summary"]))

    # ---- dashboard -----------------------------------------------------------
    # (id, type, x, y, w, h) on Kibana's 48-column grid.
    grid = [
        ("dbwiki-lens-runs-total",        "lens",   0,  0,  8,  8),
        ("dbwiki-lens-runs-failed",       "lens",   8,  0,  8,  8),
        ("dbwiki-lens-outcomes",          "lens",  16,  0, 16,  8),
        ("dbwiki-lens-error-categories",  "lens",  32,  0, 16,  8),
        ("dbwiki-lens-runs-over-time",    "lens",   0,  8, 24, 12),
        ("dbwiki-lens-run-duration",      "lens",  24,  8, 24, 12),
        ("dbwiki-lens-decisions-per-db",  "lens",   0, 20, 16, 12),
        ("dbwiki-lens-decision-reasons",  "lens",  16, 20, 16, 12),
        ("dbwiki-lens-events-over-time",  "lens",  32, 20, 16, 12),
        ("dbwiki-lens-agent-durations",   "lens",   0, 32, 16, 12),
        ("dbwiki-lens-ledger-status",     "lens",  16, 32, 12, 12),
        ("dbwiki-lens-ledger-table",      "lens",  28, 32, 20, 12),
        ("dbwiki-search-cron",            "search", 0, 44, 28, 15),
        ("dbwiki-search-audit",           "search", 28, 44, 20, 15),
    ]
    panels = []
    references = []
    for n, (so_id, so_type, x, y, w, h) in enumerate(grid):
        panels.append({
            "type": so_type,
            "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(n)},
            "panelIndex": str(n),
            "embeddableConfig": {"enhancements": {}},
            "panelRefName": f"panel_{n}",
        })
        references.append({"type": so_type, "id": so_id, "name": f"panel_{n}"})

    objects.extend(build_agent_runs_objects())
    objects.extend(build_loop_overview_objects())

    objects.append({
        "id": "dbwiki-pipeline",
        "type": "dashboard",
        "attributes": {
            "title": "dbwiki — pipeline observability",
            "description": ("What the dbwiki pipeline is doing: command outcomes and "
                            "failures, wake/skip decisions per database, agent stage "
                            "timings, per-digest ledger events, and the raw cron + "
                            "agent audit streams. Fed by elk/ (filebeat + "
                            "emit_derived.py); recreate with elk/setup.sh."),
            "timeRestore": False,
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({
                "useMargins": True, "syncColors": False, "syncCursor": True,
                "syncTooltips": False, "hidePanelTitles": False,
            }),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}),
            },
        },
        "references": references,
    })
    return objects


def build_agent_runs_objects() -> list:
    """The 'dbwiki — agent runs' dashboard: one row per LLM stage attempt
    (source .state/agent_runs.jsonl) with timings and token/cost usage."""
    objects = [
        lens_metric("dbwiki-lens-ar-total", "Agent runs", DV_AGENT,
                    col_count("Agent runs")),
        lens_metric("dbwiki-lens-ar-input-tokens", "Input tokens", DV_AGENT,
                    col_metric("sum", "input_tokens", "input tokens")),
        lens_metric("dbwiki-lens-ar-output-tokens", "Output tokens", DV_AGENT,
                    col_metric("sum", "output_tokens", "output tokens")),
        lens_metric("dbwiki-lens-ar-cost", "Cost (USD)", DV_AGENT,
                    col_metric("sum", "cost_usd", "cost USD")),
        lens_metric(
            "dbwiki-lens-ar-problems", "Problem runs", DV_AGENT,
            col_count("Problem runs",
                      kql="validation_ok : false or timed_out : true "
                          "or rolled_back : true")),
        lens_xy(
            "dbwiki-lens-ar-over-time", "Agent runs over time by task",
            DV_AGENT, "bar_stacked",
            {"x1": col_date(), "s1": col_terms("task", "m1", size=6),
             "m1": col_count("Runs")},
            x="x1", split="s1", accessors=["m1"]),
        lens_xy(
            "dbwiki-lens-ar-duration", "Stage duration over time (avg s)",
            DV_AGENT, "line",
            {"x1": col_date(), "s1": col_terms("task", "m1", size=6),
             "m1": col_metric("average", "duration_s", "avg duration (s)")},
            x="x1", split="s1", accessors=["m1"]),
        lens_xy(
            "dbwiki-lens-ar-tokens-time", "Tokens over time", DV_AGENT,
            "area",
            {"x1": col_date(),
             "m1": col_metric("sum", "input_tokens", "input tokens"),
             "m2": col_metric("sum", "output_tokens", "output tokens")},
            x="x1", split=None, accessors=["m1", "m2"]),
        lens_table(
            "dbwiki-lens-ar-by-model", "Per model: runs, timings, tokens, cost",
            DV_AGENT,
            {"b1": col_terms("model", "m1", size=10, label="model"),
             "b2": col_terms("model_tier", "m1", size=4, label="tier"),
             "m1": col_count("runs"),
             "m2": col_metric("average", "duration_s", "avg s"),
             "m3": col_metric("sum", "input_tokens", "input tokens"),
             "m4": col_metric("sum", "output_tokens", "output tokens"),
             "m5": col_metric("sum", "cost_usd", "cost USD")},
            ["b1", "b2", "m1", "m2", "m3", "m4", "m5"]),
        lens_donut("dbwiki-lens-ar-adapter", "Runs by adapter", DV_AGENT,
                   "adapter", size=6),
        lens_xy(
            "dbwiki-lens-ar-outcomes", "Problems by task", DV_AGENT, "bar",
            {"x1": col_terms("task", "m1", size=6, label="task"),
             "m1": col_count("validation failed",
                             kql="validation_ok : false"),
             "m2": col_count("rolled back", kql="rolled_back : true"),
             "m3": col_count("timed out", kql="timed_out : true")},
            x="x1", split=None, accessors=["m1", "m2", "m3"]),
        lens_xy(
            "dbwiki-lens-ar-db", "Ingest runs per database", DV_AGENT,
            "bar_horizontal_stacked",
            {"x1": col_terms("db", "m1", size=20, label="database"),
             "s1": col_terms("model_tier", "m1", size=4),
             "m1": col_count("Runs")},
            x="x1", split="s1", accessors=["m1"]),
        saved_search(
            "dbwiki-search-agent-runs", "Agent runs (raw)", DV_AGENT,
            ["task", "db", "mode", "adapter", "model", "model_tier",
             "duration_s", "input_tokens", "output_tokens", "cost_usd",
             "validation_ok", "rolled_back", "run_id"]),
    ]

    grid = [
        ("dbwiki-lens-ar-total",         "lens",   0,  0,  8,  8),
        ("dbwiki-lens-ar-input-tokens",  "lens",   8,  0, 10,  8),
        ("dbwiki-lens-ar-output-tokens", "lens",  18,  0, 10,  8),
        ("dbwiki-lens-ar-cost",          "lens",  28,  0, 10,  8),
        ("dbwiki-lens-ar-problems",      "lens",  38,  0, 10,  8),
        ("dbwiki-lens-ar-over-time",     "lens",   0,  8, 16, 12),
        ("dbwiki-lens-ar-duration",      "lens",  16,  8, 16, 12),
        ("dbwiki-lens-ar-tokens-time",   "lens",  32,  8, 16, 12),
        ("dbwiki-lens-ar-by-model",      "lens",   0, 20, 26, 12),
        ("dbwiki-lens-ar-adapter",       "lens",  26, 20, 10, 12),
        ("dbwiki-lens-ar-outcomes",      "lens",  36, 20, 12, 12),
        ("dbwiki-lens-ar-db",            "lens",   0, 32, 16, 13),
        ("dbwiki-search-agent-runs",     "search", 16, 32, 32, 13),
    ]
    panels = []
    references = []
    for n, (so_id, so_type, x, y, w, h) in enumerate(grid):
        panels.append({
            "type": so_type,
            "gridData": {"x": x, "y": y, "w": w, "h": h, "i": f"ar{n}"},
            "panelIndex": f"ar{n}",
            "embeddableConfig": {"enhancements": {}},
            "panelRefName": f"panel_{n}",
        })
        references.append({"type": so_type, "id": so_id, "name": f"panel_{n}"})

    objects.append({
        "id": "dbwiki-agent-runs",
        "type": "dashboard",
        "attributes": {
            "title": "dbwiki — agent runs",
            "description": ("Every LLM stage attempt (ingest/report/lint/"
                            "research) with duration, input/output tokens and "
                            "cost, by task, model, tier and database. Source: "
                            ".state/agent_runs.jsonl via elk/; recreate with "
                            "elk/setup.sh."),
            "timeRestore": False,
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({
                "useMargins": True, "syncColors": False, "syncCursor": True,
                "syncTooltips": False, "hidePanelTitles": False,
            }),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}),
            },
        },
        "references": references,
    })
    return objects


def build_loop_overview_objects() -> list:
    """The 'dbwiki — loop overview' dashboard: the individual agent loops —
    the recurring commands in run_health (run, analyst, lint, research,
    retry, ingest, report, ...) and the agent stages inside each round —
    answering "what was run, when, and how did it end up", round by round.
    Queue/alerts/starts survive as a compact operational strip at the bottom;
    deeper detail lives on the other two dashboards."""
    objects = [
        lens_table(
            "dbwiki-lens-lo-loop-status", "Loops at a glance", DV_RUN,
            {"b1": col_terms("command", "m1", size=8, label="loop"),
             "m1": col_last_value("started", "date", "last run"),
             "m2": col_last_value("outcome", "string", "last outcome"),
             "m3": col_count("runs"),
             "m4": col_count("failed", kql='outcome : "failed"'),
             "m5": col_metric("median", "duration_s", "median s")},
            ["b1", "m1", "m2", "m3", "m4", "m5"]),
        lens_table(
            "dbwiki-lens-lo-schedule", "What will run next", DV_SCHEDULE,
            {"b1": col_terms("entry", "m1", size=15, label="entry", order_dir="asc"),
             "m1": col_last_value("next_run_at", "date", "next run"),
             "m2": col_last_value("cron_expr", "string", "cron"),
             "m3": col_last_value("command", "string", "command"),
             "m4": col_last_value("node", "string", "node")},
            ["b1", "m1", "m2", "m3", "m4"]),
        lens_xy(
            "dbwiki-lens-lo-rounds-by-loop", "Rounds over time by loop",
            DV_RUN, "bar_stacked",
            {"x1": col_date(), "s1": col_terms("command", "m1", size=8, label="loop"),
             "m1": col_count("rounds")},
            x="x1", split="s1", accessors=["m1"]),
        lens_xy(
            "dbwiki-lens-lo-how-rounds-ended", "Failed rounds by error category",
            DV_RUN, "bar_stacked",
            {"x1": col_date(),
             "s1": col_terms("error_category", "m1", size=8, label="error category"),
             "m1": col_count("failed rounds", kql='outcome : "failed"')},
            x="x1", split="s1", accessors=["m1"]),
        saved_search(
            "dbwiki-search-lo-recent-runs", "Rounds — every recorded run",
            DV_RUN,
            ["started", "command", "outcome", "duration_s", "error_category",
             "run_id"]),
        saved_search(
            "dbwiki-search-lo-agent-stages", "Agent stages within each round",
            DV_AGENT,
            ["task", "db", "mode", "model", "duration_s", "validation_ok",
             "timed_out", "rolled_back", "run_id"]),
        saved_search(
            "dbwiki-search-lo-run-starts", "Recently started (in flight?)",
            DV_RUN_STARTS, ["started", "command", "run_host", "run_id"]),
        lens_table(
            "dbwiki-lens-lo-queue-snapshot", "Queue snapshot (last)",
            DV_QUEUE_STATE,
            {"m1": col_last_value("pending_count", "number", "pending"),
             "m2": col_last_value("claimed_count", "number", "claimed"),
             "m3": col_last_value("failed_count", "number", "failed"),
             "m4": col_last_value("oldest_pending_hours", "number", "oldest pending (h)"),
             "m5": col_last_value("stale_claimed_count", "number", "stale claims")},
            ["m1", "m2", "m3", "m4", "m5"]),
        lens_xy(
            "dbwiki-lens-lo-alerts-time", "Alerts over time by category",
            DV_ALERTS, "bar_stacked",
            {"x1": col_date(), "s1": col_terms("category", "m1", size=10),
             "m1": col_count("Alerts")},
            x="x1", split="s1", accessors=["m1"]),
    ]

    # (id, type, x, y, w, h) on Kibana's 48-column grid. No panel pins its own
    # timeRange: every panel follows the dashboard time picker.
    grid = [
        ("dbwiki-lens-lo-loop-status",      "lens",   0,  0, 28, 10),
        ("dbwiki-lens-lo-schedule",         "lens",  28,  0, 20, 10),
        ("dbwiki-lens-lo-rounds-by-loop",   "lens",   0, 10, 24, 12),
        ("dbwiki-lens-lo-how-rounds-ended", "lens",  24, 10, 24, 12),
        ("dbwiki-search-lo-recent-runs",    "search", 0, 22, 48, 12),
        ("dbwiki-search-lo-agent-stages",   "search", 0, 34, 48, 12),
        ("dbwiki-search-lo-run-starts",     "search", 0, 46, 16, 10),
        ("dbwiki-lens-lo-queue-snapshot",   "lens",  16, 46, 12, 10),
        ("dbwiki-lens-lo-alerts-time",      "lens",  28, 46, 20, 10),
    ]
    panels = []
    references = []
    for n, (so_id, so_type, x, y, w, h) in enumerate(grid):
        panels.append({
            "type": so_type,
            "gridData": {"x": x, "y": y, "w": w, "h": h, "i": f"lo{n}"},
            "panelIndex": f"lo{n}",
            "embeddableConfig": {"enhancements": {}},
            "panelRefName": f"panel_{n}",
        })
        references.append({"type": so_type, "id": so_id, "name": f"panel_{n}"})

    objects.append({
        "id": "dbwiki-loop-overview",
        "type": "dashboard",
        "attributes": {
            "title": "dbwiki — loop overview",
            "description": ("The agent loops, round by round: per-loop last "
                            "run/outcome/median duration, what runs next, "
                            "rounds over time by loop, failed rounds by error "
                            "category, every recorded round and the agent "
                            "stages inside it, plus a queue/starts/alerts "
                            "strip. Detail lives on 'dbwiki — pipeline "
                            "observability' and 'dbwiki — agent runs'; "
                            "recreate with elk/setup.sh."),
            "timeRestore": False,
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({
                "useMargins": True, "syncColors": False, "syncCursor": True,
                "syncTooltips": False, "hidePanelTitles": False,
            }),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}),
            },
        },
        "references": references,
    })
    return objects


if __name__ == "__main__":
    # A JSON array for Kibana's _bulk_create API (setup.sh). The _import API
    # is deliberately NOT used: it treats objects without migration-version
    # stamps as ancient and runs 7.x Lens migrations that crash on modern
    # state; _bulk_create stores them as current-version, no transform.
    print(json.dumps(build_objects(), sort_keys=True, indent=1))
