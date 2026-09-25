"""Requires `make infra-up`. Run with: make test-integration"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx2
import pytest

from aiops.seed.elasticsearch import ElasticsearchSeeder
from aiops.seed.logs import LogGenerator, SeedWindow, index_name

pytestmark = pytest.mark.integration
ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")


def test_seed_s1_and_query_signature() -> None:
    window = SeedWindow.build(datetime.now(UTC), hours=25)
    generator = LogGenerator("S1", window)
    seeder = ElasticsearchSeeder(ES_URL)
    try:
        seeder.ping()
        seeder.ensure_template()
        seeder.delete_seeded_indices()
        docs = (
            (index_name(d["service"], "production", datetime.fromisoformat(d["@timestamp"])), d)
            for d in generator.generate()
        )
        counts = seeder.bulk(docs)
        seeder.write_meta("S1", window, 42, counts)
        meta = seeder.read_meta()
    finally:
        seeder.close()

    assert meta is not None and meta["scenario"] == "S1"
    esql = (
        'FROM payment-prod-* | WHERE level == "ERROR" AND error_type == "ConnectionTimeoutException" '
        "| STATS n = COUNT(*) BY version"
    )
    response = httpx2.post(f"{ES_URL}/_query", json={"query": esql}, timeout=30)
    response.raise_for_status()
    rows = response.json()["values"]
    assert rows and rows[0][1] == "v1.8.2" and rows[0][0] > 20
