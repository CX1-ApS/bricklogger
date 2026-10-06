"""Fixtures shared by the test modules."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon
from tests.fakes import FakeDestination, FakeSource
from tests.support import (
    DESTINATIONS,
    REGISTRY,
    SOURCES,
    Served,
    build_template,
    free_port,
    make_config,
    wait_for,
)


@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A data directory with the example model activated, built once per run."""
    return build_template(tmp_path_factory.mktemp("template"))


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeDestination.received.clear()
    FakeDestination.metadata_received.clear()
    FakeDestination.starts.clear()
    FakeSource.instances.clear()


@pytest.fixture
def served(tmp_path: Path, template: Path) -> Iterator[Served]:
    """A daemon with the fake plugins, its API served by uvicorn in a thread."""
    port = free_port()
    config_dir = make_config(
        tmp_path,
        template,
        sources=SOURCES,
        destinations=DESTINATIONS,
        daemon_extra=f"api:\n  port: {port}\n",
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(daemon), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, name="api", daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    yield Served(config_dir, daemon, port)
    server.should_exit = True
    thread.join(5)
    daemon.stop()
