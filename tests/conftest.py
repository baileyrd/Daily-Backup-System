"""Shared test fixtures and helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx
import pytest
from pydantic import BaseModel

from dbs.core.capabilities import Capabilities, ItemKind
from dbs.core.connector import Connector
from dbs.core.engine import Engine
from dbs.core.models import RunContext
from dbs.core.registry import RegisteredConnector
from dbs.core.secrets import Secrets
from dbs.storage.sqlite import SqliteStorage

UTC = timezone.utc


class FixedClock:
    """A controllable monotonic-ish clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2024, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def storage(tmp_path, clock) -> SqliteStorage:
    st = SqliteStorage(tmp_path / "test.sqlite3", clock=clock)
    st.migrate()
    yield st
    st.close()


class FakeConfig(BaseModel):
    pass


def make_connector(
    *,
    caps: Capabilities | None = None,
    kinds: tuple[str, ...] = ("note",),
    volatile: tuple[str, ...] = (),
) -> type[Connector]:
    """Build a fresh Connector subclass whose fetch() replays ``self.script``."""

    class _Fake(Connector):
        type = "fake"
        config_model = FakeConfig
        secret_keys = ()
        item_kinds = tuple(ItemKind(k, k) for k in kinds)
        volatile_fields = volatile
        capabilities = caps or Capabilities(
            supports_incremental=True,
            supports_full_enumeration=True,
            supports_native_deletes=True,
            requires_auth=False,
        )
        script: list = []
        fail_after: int | None = None

        def fetch(self, ctx):
            count = 0
            for event in type(self).script:
                yield event
                count += 1
                if type(self).fail_after is not None and count >= type(self).fail_after:
                    from dbs.core.errors import TransientFetchError

                    raise TransientFetchError("simulated failure")

    return _Fake


def registered(cls: type[Connector]) -> RegisteredConnector:
    return RegisteredConnector(
        type=cls.type, plugin_id=f"test:{cls.type}",
        dist_name="test", cls=cls, is_builtin=False,
    )


def make_ctx(
    *,
    source_id: int,
    run_id: int,
    mode: str = "incremental",
    cursor=None,
    since=None,
    clock=None,
    config: BaseModel | None = None,
    http=None,
    secrets: Secrets | None = None,
) -> RunContext:
    return RunContext(
        source_id=source_id,
        source_name="fake",
        config=config or FakeConfig(),
        secrets=secrets or Secrets({}, ()),
        cursor=cursor,
        since=since,
        http=http,
        logger=logging.getLogger("test"),
        run_id=run_id,
        mode=mode,
        now=clock or (lambda: datetime(2024, 1, 1, tzinfo=UTC)),
    )


def run_fake(
    storage: SqliteStorage,
    cls: type[Connector],
    *,
    mode="incremental",
    cursor=None,
    since=None,
    on_progress=None,
):
    """Create a source + run row and execute the fake connector through the engine."""
    source = storage.upsert_source("fake", "fake", "test:fake", "{}", 1)
    cur_before = None
    run_id = storage.begin_run(source.id, "test:fake", mode, cur_before)
    engine = Engine(storage)
    ctx = make_ctx(source_id=source.id, run_id=run_id, mode=mode, cursor=cursor, since=since)
    result = engine.run_source(registered(cls), ctx, on_progress=on_progress)
    storage.increment_run_count(source.id)
    return source, result


def mock_http(handler) -> "httpx.Client":
    return httpx.Client(transport=httpx.MockTransport(handler))


def collect(events: Iterable) -> list:
    return list(events)
