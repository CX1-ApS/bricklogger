"""Fake plugins for the daemon tests: a source that emits counting values for
its assigned points, and a destination that keeps what it receives."""

from __future__ import annotations

import os
import socketserver
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from pyoxigraph import NamedNode, QuerySolutions

from bricklogger.sdk.contract import (
    AssignedPoint,
    Destination,
    GraphReader,
    ModelDocument,
    Observation,
    Outcome,
    PointMetadata,
    Sink,
    Source,
    StatusChannel,
)
from bricklogger.sdk.declaration import (
    CollectionMethod,
    DestinationDeclaration,
    SourceDeclaration,
    ToolDeclaration,
    ToolOffer,
)


class FakeSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[str] = []
    interval: float = 0.05
    crash_after: int | None = None


class EchoParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    times: int = 1


class NoParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DumpParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str | None = None


class FakeSource(Source):
    """Claims the points whose URI contains one of ``claims`` and emits a counter."""

    instances: ClassVar[dict[str, FakeSource]] = {}

    def __init__(self, name: str, config: BaseModel, graph: GraphReader) -> None:
        super().__init__(name, config, graph)
        self.settings = FakeSourceConfig.model_validate(config.model_dump())
        self._points: list[AssignedPoint] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sink: Sink | None = None
        self._status: StatusChannel | None = None
        self._counter = 0
        self._crashed = False
        self.assignments: list[list[AssignedPoint]] = []
        FakeSource.instances[name] = self

    def resources(self) -> Iterable[str]:
        return [f"fake:{self.name}"]

    def claim(self) -> set[str]:
        result = self.graph.query(
            "SELECT ?p WHERE { ?p a <https://brickschema.org/schema/Brick#Point> }"
        )
        claimed: set[str] = set()
        if isinstance(result, QuerySolutions):
            for solution in result:
                term = solution["p"]
                if isinstance(term, NamedNode) and any(
                    fragment in term.value for fragment in self.settings.claims
                ):
                    claimed.add(term.value)
        return claimed

    def start(self, sink: Sink, status: StatusChannel) -> None:
        self._sink, self._status = sink, status
        self._stop.clear()
        self._report_outcomes()
        loops = 0
        while not self._stop.wait(self.settings.interval):
            loops += 1
            if (
                self.settings.crash_after is not None
                and loops >= self.settings.crash_after
                and not self._crashed
            ):
                self._crashed = True
                raise RuntimeError("fake crash")
            with self._lock:
                points = list(self._points)
            self._counter += 1
            now = datetime.now(UTC)
            sink.observations(
                [
                    Observation(point.uri, now, "number", float(self._counter))
                    for point in points
                    if "Unsupported" not in point.uri or point.method == "poll"
                ]
            )

    def assign(self, points: Sequence[AssignedPoint]) -> None:
        with self._lock:
            self._points = list(points)
            self.assignments.append(list(points))
        self._report_outcomes()

    def stop(self) -> None:
        self._stop.set()

    def run_tool(self, name: str, parameters: Mapping[str, Any]) -> Any:
        if name == "echo":
            return {
                "instance": self.name,
                "echo": parameters["text"] * parameters["times"],
            }
        if name == "fail":
            raise RuntimeError("the fake tool broke")
        if name == "dump":
            return {
                "instance": self.name,
                "scope": parameters.get("scope"),
                "claims": list(self.settings.claims),
            }
        if name == "claims":
            return [
                {"claim": claim, "index": index}
                for index, claim in enumerate(self.settings.claims)
            ]
        return super().run_tool(name, parameters)

    def _report_outcomes(self) -> None:
        if self._status is None:
            return
        with self._lock:
            points = list(self._points)
        outcomes = [
            Outcome(point.uri, "unsupported", "the fake device cannot subscribe")
            if "Unsupported" in point.uri and point.method != "poll"
            else Outcome(point.uri, "active")
            for point in points
        ]
        self._status.outcomes(outcomes)
        if self._sink is not None and points:
            self._sink.metadata(
                [PointMetadata(point.uri, "number", "unit:DEG_C") for point in points]
            )


class FakeDestinationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_first_start: bool = False
    break_after: int | None = None


class FakeDestination(Destination):
    """Keeps every observation, metadata entry and model version it is given,
    and numbers the points it meets as its keys."""

    received: ClassVar[dict[str, list[Observation]]] = {}
    metadata_received: ClassVar[dict[str, list[PointMetadata]]] = {}
    models_received: ClassVar[dict[str, list[ModelDocument]]] = {}
    keys: ClassVar[dict[str, dict[str, str]]] = {}
    starts: ClassVar[dict[str, int]] = {}

    def __init__(self, name: str, config: BaseModel) -> None:
        super().__init__(name, config)
        self.settings = FakeDestinationConfig.model_validate(config.model_dump())
        self._failed_once = False
        self._broken_once = False
        self._connected = False
        self._writes = 0
        FakeDestination.received.setdefault(name, [])
        FakeDestination.metadata_received.setdefault(name, [])
        FakeDestination.models_received.setdefault(name, [])
        FakeDestination.keys.setdefault(name, {})
        FakeDestination.starts.setdefault(name, 0)

    def start(self) -> None:
        if self.settings.fail_first_start and not self._failed_once:
            self._failed_once = True
            raise ConnectionError("database not ready")
        self._connected = True
        FakeDestination.starts[self.name] = FakeDestination.starts.get(self.name, 0) + 1

    def write(self, batch: Sequence[Observation]) -> None:
        if not self._connected:
            raise ConnectionError("the connection is lost")
        if (
            self.settings.break_after is not None
            and self._writes >= self.settings.break_after
            and not self._broken_once
        ):
            # the far end goes away, the way a restarted database does: every
            # write on this connection fails until the destination is started again
            self._broken_once = True
            self._connected = False
            raise ConnectionError("the connection is lost")
        self._writes += 1
        FakeDestination.received[self.name].extend(batch)
        self._meet(o.point for o in batch)

    def write_metadata(self, entries: Sequence[PointMetadata]) -> None:
        FakeDestination.metadata_received[self.name].extend(entries)
        self._meet(e.point for e in entries)

    def timeseries_ids(self) -> Mapping[str, str]:
        return dict(FakeDestination.keys[self.name])

    def write_model(self, model: ModelDocument) -> None:
        FakeDestination.models_received[self.name].append(model)

    def _meet(self, points: Iterable[str]) -> None:
        keys = FakeDestination.keys[self.name]
        for point in points:
            keys.setdefault(point, str(len(keys) + 1))

    def stop(self) -> None:
        return None


FAKE_SOURCE = SourceDeclaration(
    type_name="fake-source",
    description="A test source that counts.",
    config_schema=FakeSourceConfig,
    reference_types=("ref:BACnetReference",),
    methods=(CollectionMethod("subscribe", "A pretend subscription."),),
    tools=(
        ToolDeclaration("echo", "Repeats a text.", EchoParameters),
        ToolDeclaration("fail", "Always fails.", NoParameters),
        ToolDeclaration(
            "dump",
            "What the fake knows, as a document.",
            DumpParameters,
            document=True,
            offered_on=ToolOffer("claims", {"scope": "claim"}),
        ),
        ToolDeclaration("claims", "The claims, one per row.", NoParameters),
    ),
    factory=FakeSource,
)

FAKE_DESTINATION = DestinationDeclaration(
    type_name="fake-destination",
    description="A test destination that remembers.",
    config_schema=FakeDestinationConfig,
    stores_metadata=True,
    factory=FakeDestination,
    stores_model=True,
)


class FakeSmtpServer:
    """A mail server that speaks just enough SMTP for ``smtplib``.

    Greeting, EHLO, MAIL, RCPT, DATA and QUIT, on a free port of its own. The
    project writes its own fakes rather than taking a dependency for one, as
    the simulated BACnet device does.
    """

    def __init__(self, *, reject: str | None = None, starttls: bool = False) -> None:
        self.received: list[bytes] = []
        self.reject = reject
        self.starttls = starttls
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 5

            def handle(self) -> None:
                self.wfile.write(b"220 fake.example.com ESMTP\r\n")
                body: list[bytes] = []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    command = line.strip().upper()
                    if command.startswith((b"EHLO", b"HELO")):
                        extra = b"250-STARTTLS\r\n" if fake.starttls else b""
                        self.wfile.write(
                            b"250-fake.example.com\r\n" + extra + b"250 HELP\r\n"
                        )
                    elif command == b"STARTTLS":
                        # Answer 220 and go no further: the client then
                        # tries to wrap the socket, which is the moment a
                        # client without a host name gives itself away.
                        self.wfile.write(b"220 go ahead\r\n")
                        return
                    elif command.startswith(b"MAIL"):
                        self.wfile.write(b"250 OK\r\n")
                    elif command.startswith(b"RCPT"):
                        text = line.decode("utf-8", "replace")
                        if fake.reject is not None and fake.reject in text:
                            self.wfile.write(b"550 no such recipient\r\n")
                        else:
                            self.wfile.write(b"250 OK\r\n")
                    elif command == b"DATA":
                        self.wfile.write(b"354 end with a lone dot\r\n")
                        while True:
                            chunk = self.rfile.readline()
                            if chunk in (b".\r\n", b".\n", b""):
                                break
                            body.append(chunk)
                        fake.received.append(b"".join(body))
                        body = []
                        self.wfile.write(b"250 queued as 1\r\n")
                    elif command in (b"RSET", b"NOOP"):
                        self.wfile.write(b"250 OK\r\n")
                    elif command == b"QUIT":
                        self.wfile.write(b"221 Bye\r\n")
                        return
                    else:
                        self.wfile.write(b"502 not implemented\r\n")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-smtp", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


# --- an installed distribution, on disk --------------------------------------------


def write_distribution(
    directory: Path,
    name: str,
    version: str,
    *,
    requires: Sequence[str] = (),
    entry_points: Mapping[str, str] | None = None,
) -> Path:
    """The metadata of an installed distribution and nothing else: what
    ``importlib.metadata`` finds in a site-packages or a plugin volume.
    ``requires`` are its ``Requires-Dist`` lines; ``entry_points`` maps a
    group to its ``name = module:attr`` lines, and a distribution with one in
    a plugin group is an installed plugin."""
    info = directory / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {line}\n" for line in requires)
    )
    if entry_points:
        body = "".join(
            f"[{group}]\n{lines}\n\n" for group, lines in entry_points.items()
        )
        (info / "entry_points.txt").write_text(body)
    return info


@contextmanager
def in_the_same_tick(directory: Path) -> Iterator[None]:
    """What is written inside reads back with the directory's mtime unchanged,
    as when it lands in the same tick of the coarse clock that stamps
    directories: a few milliseconds, which a fake install always stays within
    and a real one can. Without it, whether a test sees that happen is luck."""
    before = os.stat(directory)
    yield
    os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))
