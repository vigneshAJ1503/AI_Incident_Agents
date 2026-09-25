"""Deterministic sample Git repository for the Code agent (PR-026, MASTER_PLAN D7).

Builds a small monorepo with ~2 weeks of realistic history for the four sample
services and, per scenario, the "bad" change that explains the incident:

    services/<service>/app/*.py            small application code
    services/<service>/config/app.yaml     runtime env (rendered into a ConfigMap)
    services/<service>/k8s/deployment.yaml image tag, replicas, resources
    services/<service>/requirements.txt    dependencies
    services/inventory-service/migrations  SQL schema migrations

Release tags use a monorepo scheme ``<service>/<version>`` (e.g.
``payment-service/v1.8.2``) and match the versions in :mod:`aiops.seed.logs`.

Determinism: fixed authors, commit/tag dates relative to ``now`` (rounded to the
minute, like the log seeder) and an isolated git config, so the same
(scenario, now) always produces the same commit SHAs.

Like the log seeder, the repo is built per scenario: every scenario shares the
same background history, and S1-S4 add their incident change on top. S0 and S5
(an infrastructure-level Redis outage) have no suspicious change.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops.seed.logs import SCENARIOS

TAG_TEMPLATE = "{service}/{version}"
REGISTRY = "registry.example.com/acme"
MARKER = "aiops-sample-repo"  # file inside .git/ marking a repo we generated (safe to rebuild)

Files = dict[str, str]


class RepoSeedError(Exception):
    pass


@dataclass(frozen=True)
class Author:
    name: str
    email: str


PRIYA = Author("Priya Raman", "priya.raman@example.com")
MARCO = Author("Marco Silva", "marco.silva@example.com")
AIKO = Author("Aiko Tanaka", "aiko.tanaka@example.com")
SAM = Author("Sam Okafor", "sam.okafor@example.com")
JORDAN = Author("Jordan Lee", "jordan.lee@example.com")
BOT = Author("deps-bot", "deps-bot@example.com")


@dataclass(frozen=True)
class Change:
    """One commit: ``age`` before ``now``, an edit of the file tree, optional release tags."""

    age: timedelta
    author: Author
    subject: str
    apply: Callable[[Files], None]
    body: str = ""
    tags: tuple[str, ...] = ()
    scenario: str | None = None  # None = background history shared by every scenario


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    date: datetime
    author: str
    subject: str
    tags: tuple[str, ...]
    scenario: str | None


@dataclass(frozen=True)
class RepoSummary:
    path: Path
    scenario: str
    now: datetime
    commits: list[CommitInfo]

    @property
    def head(self) -> str:
        return self.commits[-1].sha

    @property
    def tags(self) -> dict[str, str]:
        return {tag: c.sha for c in self.commits for tag in c.tags}


def d(days: float = 0, hours: float = 0, minutes: float = 0) -> timedelta:
    return timedelta(days=days, hours=hours, minutes=minutes)


def tag(service: str, version: str) -> str:
    return TAG_TEMPLATE.format(service=service, version=version)


# --------------------------------------------------------------------------- file helpers


def edit(files: Files, path: str, old: str, new: str) -> None:
    """Replace ``old`` exactly once; fails loudly if the history and templates drift apart."""
    text = files[path]
    if text.count(old) != 1:
        raise RepoSeedError(f"expected exactly one {old!r} in {path}")
    files[path] = text.replace(old, new)


def svc(service: str, rel: str) -> str:
    return f"services/{service}/{rel}"


def set_env(files: Files, service: str, key: str, value: str) -> None:
    path = svc(service, "config/app.yaml")
    text, n = re.subn(
        rf'^(  {re.escape(key)}: )"[^"]*"$', rf'\g<1>"{value}"', files[path], flags=re.M
    )
    if n != 1:
        raise RepoSeedError(f"{key} not found in {path}")
    files[path] = text


def add_env(files: Files, service: str, key: str, value: str) -> None:
    path = svc(service, "config/app.yaml")
    files[path] = files[path] + f'  {key}: "{value}"\n'


def set_image(files: Files, service: str, version: str) -> None:
    path = svc(service, "k8s/deployment.yaml")
    text, n = re.subn(
        rf"(image: {re.escape(REGISTRY)}/{re.escape(service)}:)\S+", rf"\g<1>{version}", files[path]
    )
    if n != 1:
        raise RepoSeedError(f"image not found in {path}")
    files[path] = text


def set_version(files: Files, service: str, version: str) -> None:
    path = svc(service, "app/__init__.py")
    files[path] = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', files[path])


def release(files: Files, service: str, version: str) -> None:
    set_version(files, service, version)
    set_image(files, service, version)


def bump(files: Files, service: str, package: str, version: str) -> None:
    path = svc(service, "requirements.txt")
    text, n = re.subn(
        rf"^{re.escape(package)}==\S+$", f"{package}=={version}", files[path], flags=re.M
    )
    if n != 1:
        raise RepoSeedError(f"{package} not found in {path}")
    files[path] = text


def replace_all(files: Files, path: str, old: str, new: str) -> None:
    if old not in files[path]:
        raise RepoSeedError(f"{old!r} not found in {path}")
    files[path] = files[path].replace(old, new)


def append(files: Files, path: str, text: str) -> None:
    files[path] = files.get(path, "") + text


# --------------------------------------------------------------------------- initial tree


@dataclass(frozen=True)
class ServiceSpec:
    version: str  # first released version (later releases happen in the history)
    team: str
    replicas: int
    memory: tuple[str, str]  # request, limit
    env: dict[str, str]
    requirements: dict[str, str]


SERVICE_SPECS: dict[str, ServiceSpec] = {
    "payment-service": ServiceSpec(
        version="v1.8.0",
        team="payments",
        replicas=3,
        memory=("512Mi", "1Gi"),
        env={
            "DB_HOST": "postgres.prod.svc",
            "DB_POOL_SIZE": "20",
            "DB_TIMEOUT_MS": "5000",
            "REDIS_URL": "redis://redis.prod.svc:6379/0",
            "ISSUER_TIMEOUT_MS": "3000",
        },
        requirements={
            "fastapi": "0.115.0",
            "psycopg-pool": "3.2.2",
            "redis": "5.0.8",
            "structlog": "24.2.0",
        },
    ),
    "order-service": ServiceSpec(
        version="v2.2.1",
        team="commerce",
        replicas=3,
        memory=("512Mi", "1Gi"),
        env={
            "DB_HOST": "postgres.prod.svc",
            "DB_POOL_SIZE": "15",
            "PAYMENT_URL": "http://payment-service.prod.svc:8080",
            "INVENTORY_URL": "http://inventory-service.prod.svc:8080",
            "UPSTREAM_TIMEOUT_MS": "3000",
        },
        requirements={
            "fastapi": "0.115.0",
            "httpx": "0.27.0",
            "psycopg-pool": "3.2.2",
            "structlog": "24.2.0",
        },
    ),
    "user-service": ServiceSpec(
        version="v3.1.3",
        team="identity",
        replicas=3,
        memory=("256Mi", "512Mi"),
        env={
            "DB_HOST": "postgres.prod.svc",
            "DB_POOL_SIZE": "10",
            "REDIS_URL": "redis://redis.prod.svc:6379/1",
            "SESSION_TTL_S": "3600",
        },
        requirements={
            "fastapi": "0.115.0",
            "pydantic": "2.8.2",
            "redis": "5.0.8",
            "pyjwt": "2.9.0",
        },
    ),
    "inventory-service": ServiceSpec(
        version="v1.4.1",
        team="commerce",
        replicas=2,
        memory=("256Mi", "512Mi"),
        env={
            "DB_HOST": "postgres.prod.svc",
            "DB_POOL_SIZE": "10",
            "DB_STATEMENT_TIMEOUT_MS": "2000",
        },
        requirements={
            "fastapi": "0.115.0",
            "httpx": "0.27.0",
            "psycopg-pool": "3.2.2",
        },
    ),
}

APP_CODE: dict[str, dict[str, str]] = {
    "payment-service": {
        "app/main.py": '''\
"""payment-service HTTP API."""

from fastapi import FastAPI

from app.db import pool
from app.payments import PaymentRequest, charge

api = FastAPI(title="payment-service")


@api.post("/api/v1/pay")
async def pay(request: PaymentRequest) -> dict[str, str]:
    async with pool.connection() as conn:
        payment_id = await charge(conn, request)
    return {"payment_id": payment_id, "status": "accepted"}


@api.get("/api/v1/payments/{payment_id}")
async def get_payment(payment_id: str) -> dict[str, str]:
    async with pool.connection() as conn:
        row = await conn.execute("SELECT status FROM payments WHERE id = %s", (payment_id,))
        return {"payment_id": payment_id, "status": (await row.fetchone())[0]}
''',
        "app/db.py": '''\
"""Postgres connection pool, sized from the environment."""

import os

from psycopg_pool import AsyncConnectionPool

pool = AsyncConnectionPool(
    conninfo=f"host={os.environ['DB_HOST']} dbname=payments",
    max_size=int(os.environ.get("DB_POOL_SIZE", "20")),
    timeout=int(os.environ.get("DB_TIMEOUT_MS", "5000")) / 1000,
)
''',
        "app/payments.py": '''\
"""Card payment processing."""

import uuid

from pydantic import BaseModel


class PaymentRequest(BaseModel):
    order_id: str
    amount_cents: int
    currency: str = "EUR"


async def charge(conn, request: PaymentRequest) -> str:
    payment_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO payments (id, order_id, amount_cents, currency, status) "
        "VALUES (%s, %s, %s, %s, 'accepted')",
        (payment_id, request.order_id, request.amount_cents, request.currency),
    )
    return payment_id
''',
    },
    "order-service": {
        "app/main.py": '''\
"""order-service HTTP API."""

from fastapi import FastAPI

from app.orders import OrderRequest, create_order, load_order

api = FastAPI(title="order-service")


@api.post("/api/v1/orders")
async def post_order(request: OrderRequest) -> dict[str, str]:
    return {"order_id": await create_order(request)}


@api.get("/api/v1/orders/{order_id}")
async def get_order(order_id: str) -> dict[str, object]:
    return await load_order(order_id)
''',
        "app/orders.py": '''\
"""Order creation: reserve stock, charge the payment, persist."""

import os
import uuid

import httpx
from pydantic import BaseModel

TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT_MS", "3000")) / 1000


class OrderRequest(BaseModel):
    sku: str
    quantity: int
    amount_cents: int


async def create_order(request: OrderRequest) -> str:
    order_id = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await client.post(
            f"{os.environ['INVENTORY_URL']}/api/v1/reservations",
            json={"sku": request.sku, "quantity": request.quantity},
        )
        await client.post(
            f"{os.environ['PAYMENT_URL']}/api/v1/pay",
            json={"order_id": order_id, "amount_cents": request.amount_cents},
        )
    return order_id


async def load_order(order_id: str) -> dict[str, object]:
    return {"order_id": order_id, "status": "created"}
''',
    },
    "user-service": {
        "app/main.py": '''\
"""user-service HTTP API: login and profiles."""

from fastapi import FastAPI, HTTPException

from app.auth import Credentials, authenticate, issue_token

api = FastAPI(title="user-service")


@api.post("/api/v1/login")
async def login(credentials: Credentials) -> dict[str, str]:
    user_id = await authenticate(credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": issue_token(user_id)}


@api.get("/api/v1/users/{user_id}")
async def get_user(user_id: str) -> dict[str, str]:
    return {"user_id": user_id}
''',
        "app/auth.py": '''\
"""Password authentication and session tokens."""

import os
import time

import jwt
from pydantic import BaseModel

SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", "3600"))


class Credentials(BaseModel):
    username: str
    password: str


async def authenticate(credentials: Credentials) -> str | None:
    return None if not credentials.password else credentials.username


def issue_token(user_id: str) -> str:
    claims = {"sub": user_id, "exp": int(time.time()) + SESSION_TTL_S}
    return jwt.encode(claims, os.environ["JWT_SIGNING_KEY"], algorithm="HS256")
''',
    },
    "inventory-service": {
        "app/main.py": '''\
"""inventory-service HTTP API: stock levels."""

from fastapi import FastAPI

from app.queries import stock_level

api = FastAPI(title="inventory-service")


@api.get("/api/v1/stock/{sku}")
async def get_stock(sku: str) -> dict[str, object]:
    return {"sku": sku, "available": await stock_level(sku)}
''',
        "app/queries.py": '''\
"""SQL queries for stock levels."""

from app.db import pool

STOCK_LEVEL_SQL = "SELECT available FROM stock_levels WHERE sku = %s"


async def stock_level(sku: str) -> int:
    async with pool.connection() as conn:
        row = await (await conn.execute(STOCK_LEVEL_SQL, (sku,))).fetchone()
        return row[0] if row else 0
''',
        "app/db.py": '''\
"""Postgres connection pool."""

import os

from psycopg_pool import AsyncConnectionPool

pool = AsyncConnectionPool(
    conninfo=f"host={os.environ['DB_HOST']} dbname=inventory",
    max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
)
''',
        "migrations/0001_stock_levels.sql": """\
CREATE TABLE stock_levels (
    sku        TEXT PRIMARY KEY,
    available  INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
        "migrations/0002_stock_levels_sku_idx.sql": """\
CREATE INDEX idx_stock_levels_sku ON stock_levels (sku) INCLUDE (available);
""",
    },
}


def _config(service: str, env: dict[str, str]) -> str:
    lines = [
        f"# {service} runtime configuration (rendered into ConfigMap {service}-config).",
        f"service: {service}",
        "env:",
        *(f'  {k}: "{v}"' for k, v in env.items()),
    ]
    return "\n".join(lines) + "\n"


def _deployment(service: str, version: str, replicas: int, memory: tuple[str, str]) -> str:
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {service}
  namespace: prod
  labels:
    app: {service}
spec:
  replicas: {replicas}
  selector:
    matchLabels:
      app: {service}
  template:
    metadata:
      labels:
        app: {service}
    spec:
      containers:
        - name: {service}
          image: {REGISTRY}/{service}:{version}
          ports:
            - containerPort: 8080
          envFrom:
            - configMapRef:
                name: {service}-config
          resources:
            requests:
              cpu: 250m
              memory: {memory[0]}
            limits:
              cpu: "1"
              memory: {memory[1]}
"""


def initial_tree() -> Files:
    files: Files = {
        "README.md": "# acme-shop\n\nMonorepo for the acme-shop services.\n\n"
        "| Service | Team |\n|---------|------|\n"
        + "".join(f"| {s} | {spec.team} |\n" for s, spec in SERVICE_SPECS.items())
        + "\nReleases are tagged `<service>/<version>`, e.g. `payment-service/v1.8.1`.\n",
        ".gitignore": "__pycache__/\n.venv/\n",
    }
    for service, spec in SERVICE_SPECS.items():
        files[svc(service, "README.md")] = (
            f"# {service}\n\nOwned by the {spec.team} team. "
            f"Configuration lives in `config/app.yaml`; the Kubernetes manifest in `k8s/`.\n"
        )
        files[svc(service, "app/__init__.py")] = f'__version__ = "{spec.version}"\n'
        files[svc(service, "config/app.yaml")] = _config(service, spec.env)
        files[svc(service, "k8s/deployment.yaml")] = _deployment(
            service, spec.version, spec.replicas, spec.memory
        )
        files[svc(service, "requirements.txt")] = "".join(
            f"{name}=={ver}\n" for name, ver in spec.requirements.items()
        )
        for rel, text in APP_CODE[service].items():
            files[svc(service, rel)] = text
    return files


# --------------------------------------------------------------------------- history

PAY, ORD, USR, INV = "payment-service", "order-service", "user-service", "inventory-service"


def _noop(files: Files) -> None:
    return None


def _all(*steps: Callable[[Files], None]) -> Callable[[Files], None]:
    def run(files: Files) -> None:
        for step in steps:
            step(files)

    return run


BACKGROUND: list[Change] = [
    Change(
        d(14, 1),
        SAM,
        "chore: initial monorepo layout",
        _noop,
        tags=(tag(PAY, "v1.8.0"), tag(ORD, "v2.2.1"), tag(USR, "v3.1.3"), tag(INV, "v1.4.1")),
    ),
    Change(
        d(13, 20),
        SAM,
        "docs: add local development guide",
        lambda f: append(
            f,
            "docs/development.md",
            "# Local development\n\n1. `make up`\n2. `make test`\n",
        ),
    ),
    Change(
        d(13, 3),
        PRIYA,
        "payment-service: add idempotency key to /pay",
        lambda f: edit(
            f,
            svc(PAY, "app/payments.py"),
            '    currency: str = "EUR"\n',
            '    currency: str = "EUR"\n    idempotency_key: str | None = None\n',
        ),
    ),
    Change(
        d(12, 22),
        MARCO,
        "order-service: validate quantity is positive",
        lambda f: edit(
            f,
            svc(ORD, "app/orders.py"),
            "    order_id = str(uuid.uuid4())\n",
            '    if request.quantity <= 0:\n        raise ValueError("quantity must be positive")\n'
            "    order_id = str(uuid.uuid4())\n",
        ),
    ),
    Change(
        d(12, 5),
        BOT,
        "chore(deps): bump pydantic from 2.8.2 to 2.9.2 in user-service",
        lambda f: bump(f, USR, "pydantic", "2.9.2"),
    ),
    Change(
        d(11, 23),
        AIKO,
        "user-service: rate-limit failed logins",
        _all(
            lambda f: add_env(f, USR, "LOGIN_MAX_FAILURES_PER_MIN", "10"),
            lambda f: append(
                f,
                svc(USR, "app/auth.py"),
                "\n\nMAX_FAILURES_PER_MIN = int(os.environ.get('LOGIN_MAX_FAILURES_PER_MIN', '10'))\n",
            ),
        ),
    ),
    Change(
        d(11, 2),
        MARCO,
        "inventory-service: add reservations endpoint",
        lambda f: append(
            f,
            svc(INV, "app/main.py"),
            '\n\n@api.post("/api/v1/reservations")\n'
            "async def reserve(body: dict[str, object]) -> dict[str, str]:\n"
            '    return {"status": "reserved"}\n',
        ),
    ),
    Change(
        d(10, 21),
        MARCO,
        "release order-service v2.3.0",
        lambda f: release(f, ORD, "v2.3.0"),
        tags=(tag(ORD, "v2.3.0"),),
    ),
    Change(
        d(10, 4),
        PRIYA,
        "payment-service: retry issuer timeouts once",
        _all(
            lambda f: add_env(f, PAY, "ISSUER_RETRIES", "1"),
            lambda f: append(
                f,
                svc(PAY, "app/payments.py"),
                "\n\nISSUER_RETRIES = 1  # overridden by the ISSUER_RETRIES env var\n",
            ),
        ),
    ),
    Change(
        d(9, 22),
        SAM,
        "ci: cache pip downloads",
        lambda f: append(
            f,
            ".ci/pipeline.yaml",
            "steps:\n  - uses: actions/cache@v4\n    with:\n      path: ~/.cache/pip\n",
        ),
    ),
    Change(
        d(9, 1),
        PRIYA,
        "release payment-service v1.8.1",
        lambda f: release(f, PAY, "v1.8.1"),
        tags=(tag(PAY, "v1.8.1"),),
    ),
    Change(
        d(8, 20),
        AIKO,
        "user-service: extract token claims helper",
        lambda f: edit(
            f,
            svc(USR, "app/auth.py"),
            '    claims = {"sub": user_id, "exp": int(time.time()) + SESSION_TTL_S}\n'
            '    return jwt.encode(claims, os.environ["JWT_SIGNING_KEY"], algorithm="HS256")\n',
            '    return jwt.encode(_claims(user_id), os.environ["JWT_SIGNING_KEY"], algorithm="HS256")\n'
            "\n\ndef _claims(user_id: str) -> dict[str, object]:\n"
            '    return {"sub": user_id, "exp": int(time.time()) + SESSION_TTL_S}\n',
        ),
    ),
    Change(
        d(8, 3),
        AIKO,
        "release user-service v3.1.4",
        lambda f: release(f, USR, "v3.1.4"),
        tags=(tag(USR, "v3.1.4"),),
    ),
    Change(
        d(7, 22),
        BOT,
        "chore(deps): bump httpx from 0.27.0 to 0.27.2 in inventory-service",
        lambda f: bump(f, INV, "httpx", "0.27.2"),
    ),
    Change(
        d(7, 2),
        MARCO,
        "release inventory-service v1.4.2",
        lambda f: release(f, INV, "v1.4.2"),
        tags=(tag(INV, "v1.4.2"),),
    ),
    Change(
        d(6, 21),
        PRIYA,
        "docs: payment-service architecture notes",
        lambda f: append(
            f,
            svc(PAY, "docs/architecture.md"),
            "# payment-service architecture\n\nStateless API in front of Postgres; "
            "idempotency keys are cached in Redis.\n",
        ),
    ),
    Change(
        d(6, 1),
        MARCO,
        "order-service: log order totals at debug level",
        lambda f: edit(
            f,
            svc(ORD, "app/orders.py"),
            "    return order_id\n\n\nasync def load_order",
            '    print(f"order {order_id} total={request.amount_cents}")  # debug\n'
            "    return order_id\n\n\nasync def load_order",
        ),
    ),
    Change(
        d(5, 20),
        BOT,
        "chore(deps): bump structlog from 24.2.0 to 24.4.0 in payment-service",
        lambda f: bump(f, PAY, "structlog", "24.4.0"),
    ),
    Change(
        d(5, 2),
        AIKO,
        "user-service: add avatar URL to profiles",
        lambda f: edit(
            f,
            svc(USR, "app/main.py"),
            '    return {"user_id": user_id}\n',
            '    return {"user_id": user_id, "avatar_url": f"/avatars/{user_id}.png"}\n',
        ),
    ),
    Change(
        d(4, 23),
        MARCO,
        "inventory-service: warn when stock runs low",
        lambda f: edit(
            f,
            svc(INV, "app/queries.py"),
            "        return row[0] if row else 0\n",
            "        available = row[0] if row else 0\n"
            "        if available < 5:\n"
            '            print(f"low stock for {sku}: {available}")\n'
            "        return available\n",
        ),
    ),
    Change(
        d(4, 3),
        SAM,
        "docs: update on-call contacts",
        lambda f: append(
            f,
            "README.md",
            "\n## On-call\n\nSee the team channels: #payments-oncall, #commerce-oncall, "
            "#identity-oncall.\n",
        ),
    ),
    Change(
        d(3, 21),
        PRIYA,
        "payment-service: tidy up exception handling",
        lambda f: edit(
            f,
            svc(PAY, "app/main.py"),
            "        payment_id = await charge(conn, request)\n",
            "        try:\n"
            "            payment_id = await charge(conn, request)\n"
            "        except ValueError as exc:\n"
            '            return {"payment_id": "", "status": f"rejected: {exc}"}\n',
        ),
    ),
    Change(
        d(3, 1),
        MARCO,
        "test(order-service): cover quantity validation",
        lambda f: append(
            f,
            svc(ORD, "tests/test_orders.py"),
            "import pytest\n\nfrom app.orders import OrderRequest, create_order\n\n\n"
            "async def test_rejects_zero_quantity() -> None:\n"
            "    with pytest.raises(ValueError):\n"
            '        await create_order(OrderRequest(sku="A1", quantity=0, amount_cents=100))\n',
        ),
    ),
    Change(
        d(2, 22),
        SAM,
        "chore: add editorconfig",
        lambda f: append(
            f, ".editorconfig", "root = true\n\n[*]\nindent_style = space\nindent_size = 4\n"
        ),
    ),
    Change(
        d(2, 2),
        AIKO,
        "docs(user-service): document session settings",
        lambda f: append(
            f,
            svc(USR, "README.md"),
            "\n## Sessions\n\n`SESSION_TTL_S` controls how long login tokens stay valid.\n",
        ),
    ),
    Change(
        d(1, 20),
        MARCO,
        "inventory-service: normalise SKU casing in the API",
        lambda f: edit(
            f,
            svc(INV, "app/main.py"),
            '    return {"sku": sku, "available": await stock_level(sku)}\n',
            '    sku = sku.strip()\n    return {"sku": sku, "available": await stock_level(sku)}\n',
        ),
    ),
    # ---- last 24h: harmless changes every scenario sees (the agent must not flag them)
    Change(
        d(0, 20),
        AIKO,
        "docs(user-service): clarify the login flow",
        lambda f: append(
            f,
            svc(USR, "README.md"),
            "\n## Login flow\n\n`POST /api/v1/login` returns a signed session token.\n",
        ),
    ),
    Change(
        d(0, 14),
        PRIYA,
        "payment-service: extract receipt formatting helper",
        lambda f: append(
            f,
            svc(PAY, "app/receipts.py"),
            '"""Receipt formatting."""\n\n\n'
            "def format_amount(amount_cents: int, currency: str) -> str:\n"
            '    return f"{amount_cents / 100:.2f} {currency}"\n',
        ),
    ),
    Change(
        d(0, 9),
        MARCO,
        "order-service: rename internal variables for clarity",
        lambda f: replace_all(f, svc(ORD, "app/orders.py"), "client", "http"),
    ),
    Change(
        d(0, 6),
        PRIYA,
        "docs(payment-service): link the connection pool runbook",
        lambda f: append(
            f,
            svc(PAY, "README.md"),
            "\nRunbook: `runbooks/database-connection-pool.md`.\n",
        ),
    ),
    Change(
        d(0, 2, 40),
        MARCO,
        "test(inventory-service): add stock lookup test",
        lambda f: append(
            f,
            svc(INV, "tests/test_queries.py"),
            "from app.queries import STOCK_LEVEL_SQL\n\n\n"
            "def test_query_filters_by_sku() -> None:\n"
            '    assert "WHERE" in STOCK_LEVEL_SQL\n',
        ),
    ),
]

SCENARIO_CHANGES: list[Change] = [
    # S1: DB pool 20 -> 2, released as payment-service v1.8.2 (rolled out at now-22m).
    Change(
        d(0, 2, 10),
        JORDAN,
        "tune db pool",
        lambda f: set_env(f, PAY, "DB_POOL_SIZE", "2"),
        body="Lower DB_POOL_SIZE to reduce the number of connections on the shared Postgres.",
        scenario="S1",
    ),
    Change(
        d(0, 0, 35),
        PRIYA,
        "release payment-service v1.8.2",
        lambda f: release(f, PAY, "v1.8.2"),
        tags=(tag(PAY, "v1.8.2"),),
        scenario="S1",
    ),
    # S2: unbounded in-memory cache + a lower memory limit -> OOMKilled.
    Change(
        d(0, 5),
        MARCO,
        "order-service: cache order payloads in memory",
        _all(
            lambda f: add_env(f, ORD, "ORDER_CACHE_ENABLED", "true"),
            lambda f: append(
                f,
                svc(ORD, "app/cache.py"),
                '"""In-process cache of full order payloads (avoids a DB round trip)."""\n\n'
                "_ORDER_CACHE: dict[str, dict[str, object]] = {}\n\n\n"
                "def remember(order_id: str, payload: dict[str, object]) -> None:\n"
                "    _ORDER_CACHE[order_id] = payload  # no eviction\n\n\n"
                "def lookup(order_id: str) -> dict[str, object] | None:\n"
                "    return _ORDER_CACHE.get(order_id)\n",
            ),
        ),
        body="Keep every order payload in a process-local dict for faster reads.",
        scenario="S2",
    ),
    Change(
        d(0, 1, 10),
        MARCO,
        "order-service: right-size memory limit",
        lambda f: edit(
            f,
            svc(ORD, "k8s/deployment.yaml"),
            '              cpu: "1"\n              memory: 1Gi\n',
            '              cpu: "1"\n              memory: 512Mi\n',
        ),
        body="Pods use ~300Mi on average; halve the limit to save cluster capacity.",
        scenario="S2",
    ),
    # S3: inventory stock query can no longer use its index -> slow queries -> order timeouts.
    Change(
        d(0, 3),
        MARCO,
        "inventory-service: simplify stock level query",
        _all(
            lambda f: edit(
                f,
                svc(INV, "app/queries.py"),
                'STOCK_LEVEL_SQL = "SELECT available FROM stock_levels WHERE sku = %s"\n',
                'STOCK_LEVEL_SQL = "SELECT available FROM stock_levels WHERE lower(sku) = lower(%s)"\n',
            ),
            lambda f: append(
                f,
                svc(INV, "migrations/0003_drop_stock_levels_sku_idx.sql"),
                "-- Case-insensitive lookups make this index unused; drop it.\n"
                "DROP INDEX IF EXISTS idx_stock_levels_sku;\n",
            ),
        ),
        body="Match SKUs case-insensitively and drop the now-unused index.",
        scenario="S3",
    ),
    # S4: manifest points at an image tag that was never built or released.
    Change(
        d(0, 0, 28),
        AIKO,
        "deploy user-service v3.2.0",
        lambda f: set_image(f, USR, "v3.2.0"),
        body="Roll out the new session handling.",
        scenario="S4",
    ),
    # S5: Redis outage is infrastructure-level; no code or config change explains it.
]


def changes_for(scenario: str) -> list[Change]:
    """Background history + the scenario's changes, oldest first."""
    if scenario not in SCENARIOS:
        raise RepoSeedError(f"Unknown scenario '{scenario}' (known: {', '.join(SCENARIOS)})")
    selected = [*BACKGROUND, *(c for c in SCENARIO_CHANGES if c.scenario == scenario)]
    return sorted(selected, key=lambda c: -c.age.total_seconds())


# --------------------------------------------------------------------------- building


def anchor(now: datetime) -> datetime:
    """UTC, rounded down to the minute (same convention as ``SeedWindow``)."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return now.replace(second=0, microsecond=0)


def _git_env() -> dict[str, str]:
    """Isolated git: ignore user/system config and any GIT_* vars (e.g. from a git hook)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
        TZ="UTC",
    )
    return env


GIT_OPTIONS = [
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.hooksPath=/dev/null",
]


class RepoBuilder:
    def __init__(self, path: Path, git: str = "git") -> None:
        self.path = path
        self.git_bin = shutil.which(git) or git
        self.env = _git_env()

    def git(self, *args: str, extra_env: dict[str, str] | None = None) -> str:
        env = {**self.env, **(extra_env or {})}
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self.git_bin, *GIT_OPTIONS, "-C", str(self.path), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RepoSeedError("git is not installed") from exc
        if proc.returncode != 0:
            raise RepoSeedError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def prepare(self) -> None:
        """Create an empty directory. Only a previously generated repo may be replaced."""
        path = self.path
        if path.exists() and any(path.iterdir()):
            if not (path / ".git" / MARKER).is_file():
                raise RepoSeedError(
                    f"{path} is not empty and was not created by `aiops seed repo`; refusing to "
                    "overwrite it. Choose another --path."
                )
            # Empty it in place: keeps the directory inode, so read-only bind mounts
            # (docker compose git-mcp) see the new repo without a restart.
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        path.mkdir(parents=True, exist_ok=True)

    def write_tree(self, files: Files) -> None:
        tracked = {p.relative_to(self.path).as_posix() for p in self._worktree_files()}
        for rel in tracked - set(files):
            (self.path / rel).unlink()
        for rel, text in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_text() != text:
                target.write_text(text)

    def _worktree_files(self) -> list[Path]:
        return [
            p
            for p in self.path.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.path).parts
        ]

    def build(self, scenario: str, now: datetime) -> RepoSummary:
        now = anchor(now)
        changes = changes_for(scenario)
        self.prepare()
        self.git("init", "-q", "--template=", "--initial-branch=main")
        (self.path / ".git" / MARKER).write_text(f"scenario={scenario}\nnow={now.isoformat()}\n")

        files = initial_tree()
        commits: list[CommitInfo] = []
        for change in changes:
            change.apply(files)
            self.write_tree(files)
            when = (now - change.age).isoformat()
            identity = {
                "GIT_AUTHOR_NAME": change.author.name,
                "GIT_AUTHOR_EMAIL": change.author.email,
                "GIT_AUTHOR_DATE": when,
                "GIT_COMMITTER_NAME": change.author.name,
                "GIT_COMMITTER_EMAIL": change.author.email,
                "GIT_COMMITTER_DATE": when,
            }
            self.git("add", "-A")
            message = ["-m", change.subject] + (["-m", change.body] if change.body else [])
            self.git("commit", "-q", "--allow-empty", *message, extra_env=identity)
            sha = self.git("rev-parse", "HEAD")
            for name in change.tags:
                self.git("tag", "-a", name, "-m", name.replace("/", " "), sha, extra_env=identity)
            commits.append(
                CommitInfo(
                    sha=sha,
                    date=now - change.age,
                    author=change.author.name,
                    subject=change.subject,
                    tags=change.tags,
                    scenario=change.scenario,
                )
            )
        return RepoSummary(path=self.path, scenario=scenario, now=now, commits=commits)


def build_sample_repo(path: Path, scenario: str, now: datetime) -> RepoSummary:
    return RepoBuilder(path).build(scenario, now)
