"""Shared test data and helpers: the example model, rule sets and instance
configurations the daemon tests use, a builder for a config directory, and a
wait helper."""

from __future__ import annotations

import gc
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.daemon.core import Daemon
from bricklogger.daemon.state import STATE_FILE, RuntimeState
from bricklogger.model import ModelStore, WorkingGraph, activate_version
from bricklogger.model.working_graph import GRAPH_DIR
from bricklogger.sdk.registry import PluginRegistry
from tests.fakes import FAKE_DESTINATION, FAKE_SOURCE

EX = "https://example.com/bldg#"
REGISTRY = PluginRegistry.of(FAKE_SOURCE, FAKE_DESTINATION)

MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix unit: <http://qudt.org/vocab/unit/> .
@prefix ex: <https://example.com/bldg#> .

ex:AHU_01 a brick:AHU .
ex:Room_1_17 a brick:Room .
ex:Room_1_18 a brick:Room .
ex:Ctrl a bacnet:BACnetDevice ; bacnet:device-instance 1201 .

ex:SAT a brick:Supply_Air_Temperature_Sensor ; brick:isPointOf ex:AHU_01 ;
    brick:hasUnit unit:DEG_F ;
    ref:hasExternalReference [ bacnet:object-identifier "analog-input,3" ;
                               bacnet:objectOf ex:Ctrl ] .
ex:ZAT_1_17 a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_17 ;
    ref:hasExternalReference [ bacnet:object-identifier "analog-input,7" ;
                               bacnet:objectOf ex:Ctrl ] .
ex:ZAT_1_18 a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_18 ;
    ref:hasExternalReference [ bacnet:object-identifier "analog-input,8" ;
                               bacnet:objectOf ex:Ctrl ] .
ex:Unsupported_Damper a brick:Damper_Position_Sensor ; brick:isPointOf ex:AHU_01 ;
    ref:hasExternalReference [ bacnet:object-identifier "analog-input,9" ;
                               bacnet:objectOf ex:Ctrl ] .
ex:CO2 a brick:CO2_Sensor ; brick:isPointOf ex:Room_1_17 ;
    ref:hasExternalReference [ a ref:TimeseriesReference ; ref:hasTimeseriesId "abc" ] .
ex:Meter_kWh a brick:Energy_Sensor ; brick:isPointOf ex:AHU_01 .
"""

RULES_ALL = """\
- name: Everything
  match: { class: brick:Point }
  action: accept
  method: poll
  interval: 5m
"""

MODEL_WITH_RAT = MODEL + (
    "ex:RAT a brick:Return_Air_Temperature_Sensor ; brick:isPointOf ex:AHU_01 .\n"
)

INVALID_MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ex: <https://example.com/bldg#> .

ex:Bad a brick:Temperature_Sensor ; brick:hasLocation "not a location" .
"""

SOURCES = "fake_a:\n  type: fake-source\n  claims: [SAT, ZAT_1_17, Unsupported]\n"
DESTINATIONS = "sink_a:\n  type: fake-destination\n  batch: { size: 5, interval: 1s }\n"


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll a condition until it holds, or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def finished_job(client: Any, job_id: str, timeout: float = 90.0) -> dict[str, Any]:
    """Poll a job through a test client until it is done or failed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/v1/jobs/{job_id}").json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("the job did not finish in time")


def build_template(data_dir: Path) -> Path:
    """A data directory with the model stored, marked and activated."""
    store = ModelStore(data_dir)
    store.store(MODEL.encode(), "turtle")
    graph = WorkingGraph(data_dir / GRAPH_DIR)
    activate_version(store, graph, 1)
    state = RuntimeState(data_dir / STATE_FILE)
    state.record_activation(1)
    state.close()
    del graph
    gc.collect()
    return data_dir


def make_config(
    tmp_path: Path,
    template: Path,
    *,
    sources: str,
    destinations: str,
    rules: str = RULES_ALL,
    daemon_extra: str = "",
) -> Path:
    """A config directory pointing at a fresh copy of the template data directory."""
    config_dir = tmp_path / "etc"
    data_dir = tmp_path / "var"
    config_dir.mkdir()
    shutil.copytree(template, data_dir)
    (config_dir / "daemon.yaml").write_text(f"data_dir: {data_dir}\n{daemon_extra}")
    (config_dir / "sources.yaml").write_text(sources)
    (config_dir / "destinations.yaml").write_text(destinations)
    (config_dir / "rules.yaml").write_text(rules)
    return config_dir


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Served:
    """A daemon serving its API on its own port, and a way to drive the CLI at it."""

    config_dir: Path
    daemon: Daemon
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def invoke(self, *args: str, api: bool = False) -> tuple[int, str]:
        options = ["--config-dir", str(self.config_dir)]
        if api:
            options += ["--api", self.url]
        result = CliRunner().invoke(app, [*options, *args])
        return result.exit_code, result.output
