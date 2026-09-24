"""Thin Elasticsearch client: PIT + search_after scan, terms aggs, doc fetch."""

import sys

import requests

DB_BUCKETS = 500  # terms buckets per db field in dbs_in_window


class ES:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.headers["Content-Type"] = "application/json"

    def _post(self, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        r = self.s.post(f"{self.url}{path}", json=body, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.s.get(f"{self.url}{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def ping(self) -> dict:
        return self._get("/")

    def count(self, index: str, query: dict) -> int:
        return self._post(f"/{index}/_count", {"query": query})["count"]

    def search(self, index: str, body: dict) -> dict:
        return self._post(f"/{index}/_search", body, params={"ignore_unavailable": "true"})

    def get_doc(self, index: str, doc_id: str) -> dict:
        return self._get(f"/{index}/_doc/{doc_id}")

    @staticmethod
    def window_query(ts_field: str, t0: str, t1: str,
                     db_fields: list[str] | str | None = None,
                     db: str | None = None, extra: list[dict] | None = None) -> dict:
        """`db_fields` are exact ES field names tried as alternatives (index
        generations disagree on where the db name lives). A bare string keeps
        the legacy single-field spelling with its `.keyword` suffix."""
        if isinstance(db_fields, str):
            db_fields = [f"{db_fields}.keyword"]
        must: list[dict] = [{"range": {ts_field: {"gte": t0, "lt": t1}}}]
        if db is not None and db_fields:
            terms = [{"term": {f: db}} for f in db_fields]
            must.append(terms[0] if len(terms) == 1 else
                        {"bool": {"should": terms, "minimum_should_match": 1}})
        must.extend(extra or [])
        return {"bool": {"filter": must}}

    def dbs_in_window(self, index_patterns: list[str], db_fields: list[str] | str,
                      ts_field: str, t0: str, t1: str) -> dict[str, int]:
        if isinstance(db_fields, str):
            db_fields = [f"{db_fields}.keyword"]
        body = {
            "size": 0,
            "query": self.window_query(ts_field, t0, t1),
            "aggs": {f"db{i}": {"terms": {"field": f, "size": DB_BUCKETS}}
                     for i, f in enumerate(db_fields)},
        }
        res = self.search(",".join(index_patterns), body)
        found: dict[str, int] = {}
        for i, f in enumerate(db_fields):
            agg = res.get("aggregations", {}).get(f"db{i}")
            if agg is None:
                continue
            for b in agg["buckets"]:
                found[b["key"]] = found.get(b["key"], 0) + b["doc_count"]
            # one page of DB_BUCKETS buckets: past it ES folds the rest into
            # this count, and those databases would silently go undiscovered
            # (already-known ones still come in via the registry)
            if agg.get("sum_other_doc_count", 0) > 0:
                print(f"dbs_in_window: more than {DB_BUCKETS} databases in "
                      f"{t0} -> {t1} on {f}; list truncated, "
                      f"{agg['sum_other_doc_count']} events from the rest",
                      file=sys.stderr)
        return found

    def scan(self, index_patterns: list[str], query: dict, ts_field: str, page_size: int = 2000):
        """Yield hits (dicts with _index/_id/_source) in stable @timestamp order."""
        index = ",".join(index_patterns)
        pit = self._post(f"/{index}/_pit", params={"keep_alive": "5m", "ignore_unavailable": "true"})
        pit_id = pit["id"]
        search_after = None
        try:
            while True:
                body: dict = {
                    "size": page_size,
                    "query": query,
                    "pit": {"id": pit_id, "keep_alive": "5m"},
                    "sort": [{ts_field: "asc"}, {"_shard_doc": "asc"}],
                    "track_total_hits": False,
                }
                if search_after:
                    body["search_after"] = search_after
                res = self._post("/_search", body)
                hits = res["hits"]["hits"]
                if not hits:
                    return
                pit_id = res.get("pit_id", pit_id)
                yield from hits
                search_after = hits[-1]["sort"]
        finally:
            try:
                self.s.delete(f"{self.url}/_pit", json={"id": pit_id}, timeout=10)
            except requests.RequestException:
                pass
