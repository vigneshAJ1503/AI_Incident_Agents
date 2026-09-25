"""Load generated logs into a local Elasticsearch (plain REST, no extra client)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx2

from aiops.seed.logs import (
    ENV_SHORT,
    INDEX_TEMPLATE,
    SERVICES,
    LogGenerator,
    SeedWindow,
    index_name,
)

META_INDEX = "aiops-seed-meta"
TEMPLATE_NAME = "aiops-app-logs"


class SeedError(Exception):
    pass


class ElasticsearchSeeder:
    def __init__(self, url: str, *, timeout_s: float = 60.0) -> None:
        self._client = httpx2.Client(base_url=url.rstrip("/"), timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def _check(self, response: httpx2.Response, action: str) -> Any:
        if response.status_code >= 400:
            raise SeedError(f"{action} failed: HTTP {response.status_code} {response.text[:300]}")
        return response.json() if response.content else {}

    def ping(self) -> str:
        try:
            info = self._check(self._client.get("/"), "ping")
        except httpx2.HTTPError as exc:
            raise SeedError(f"Elasticsearch not reachable: {exc}. Run `make infra-up`.") from exc
        return str(info.get("version", {}).get("number", "?"))

    def ensure_template(self) -> None:
        self._check(
            self._client.put(f"/_index_template/{TEMPLATE_NAME}", json=INDEX_TEMPLATE),
            "create index template",
        )

    def delete_seeded_indices(self, environment_short: str = "prod") -> None:
        # Explicit names (action.destructive_requires_name=true forbids wildcards on delete).
        response = self._client.get(
            "/_cat/indices", params={"format": "json", "h": "index", "expand_wildcards": "open"}
        )
        existing = [row["index"] for row in self._check(response, "list indices")]
        prefixes = tuple(f"{s.short}-{environment_short}-" for s in SERVICES.values())
        targets = [name for name in existing if name.startswith(prefixes)]
        if META_INDEX in existing:
            targets.append(META_INDEX)
        for name in targets:
            self._check(self._client.delete(f"/{name}"), f"delete {name}")

    def bulk(
        self, docs: Iterable[tuple[str, dict[str, Any]]], batch_size: int = 5_000
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        batch: list[str] = []

        def flush() -> None:
            if not batch:
                return
            body = "\n".join(batch) + "\n"
            result = self._check(
                self._client.post(
                    "/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"}
                ),
                "bulk",
            )
            if result.get("errors"):
                first = next(i for i in result["items"] if "error" in next(iter(i.values())))
                raise SeedError(f"bulk indexing errors, first: {json.dumps(first)[:400]}")
            batch.clear()

        for index, doc in docs:
            batch.append(json.dumps({"index": {"_index": index}}))
            batch.append(json.dumps(doc))
            counts[index] += 1
            if len(batch) >= batch_size * 2:
                flush()
        flush()
        self._check(self._client.post("/_refresh"), "refresh")
        return counts

    def write_meta(
        self, scenario: str, window: SeedWindow, seed: int, counts: Counter[str]
    ) -> None:
        meta = {
            "scenario": scenario,
            "seed": seed,
            "now": window.now.isoformat(),
            "start": window.start.isoformat(),
            "incident_start": window.incident_start.isoformat(),
            "deploy_time": window.deploy_time.isoformat(),
            "documents": sum(counts.values()),
        }
        self._check(
            self._client.put(f"/{META_INDEX}/_doc/current", params={"refresh": "true"}, json=meta),
            "write seed metadata",
        )

    def read_meta(self) -> dict[str, Any] | None:
        response = self._client.get(f"/{META_INDEX}/_doc/current")
        if response.status_code == 404:
            return None
        source: dict[str, Any] = self._check(response, "read seed metadata").get("_source", {})
        return source


def seed_scenario_logs(
    url: str,
    scenario: str,
    now: datetime,
    *,
    hours: float = 26.0,
    seed: int = 42,
    environment: str = "production",
) -> tuple[SeedWindow, Counter[str]]:
    """Replace the seeded log indices with ``scenario`` anchored at ``now``."""
    window = SeedWindow.build(now, hours)
    generator = LogGenerator(scenario, window, seed=seed, environment=environment)
    seeder = ElasticsearchSeeder(url)
    try:
        seeder.ping()
        seeder.ensure_template()
        seeder.delete_seeded_indices(ENV_SHORT[environment])
        docs = (
            (index_name(d["service"], environment, datetime.fromisoformat(d["@timestamp"])), d)
            for d in generator.generate()
        )
        counts = seeder.bulk(docs)
        seeder.write_meta(scenario, window, seed, counts)
    finally:
        seeder.close()
    return window, counts
