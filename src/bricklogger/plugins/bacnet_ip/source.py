"""The BACnet/IP source: devices found by instance with Who-Is, points read
with ReadPropertyMultiple at the rule's interval, status flags and errors
turned into ``null`` observations. See ``docs/features/sources.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bacpypes3.apdu import AbortPDU, ErrorPDU, ErrorRejectAbortNack, RejectPDU
from bacpypes3.app import Application
from bacpypes3.basetypes import ErrorType
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier
from pydantic import BaseModel

from bricklogger.plugins.bacnet_ip import tools
from bricklogger.plugins.bacnet_ip.config import BACnetIPConfig
from bricklogger.plugins.bacnet_ip.references import (
    BACnetReference,
    ResolvedReferences,
    resolve_references,
)
from bricklogger.plugins.bacnet_ip.values import (
    ANALOG_TYPES,
    BINARY_TYPES,
    INTEGER_TYPES,
    MULTISTATE_TYPES,
    PRESENT_VALUE,
    UNSUPPORTED,
    classify,
    enumeration_texts,
    protocol_unit,
)
from bricklogger.sdk.contract import (
    AssignedPoint,
    GraphReader,
    NullReason,
    Observation,
    Outcome,
    PointMetadata,
    Sink,
    Source,
    StatusChannel,
    ValueType,
)

log = logging.getLogger(__name__)

RPM_CHUNK = 20
STATUS_FLAGS = "status-flags"
DEFAULT_INTERVAL = timedelta(minutes=5)

SPREAD_MAX = 60.0
"""How far apart the first rounds are spread, at most, in seconds.

The whole interval when it is shorter, so hundreds of devices do not
begin in the same instant, but never so long that the first reading
looks like a failure."""

SEARCH_BACKOFF_START = 30.0
SEARCH_BACKOFF_MAX = 300.0
"""How long a device that does not answer is left alone before it is
looked for again, doubling from the first up to the second.

A search that cannot find the device at the address the model gives has
to broadcast, and every device on the network has to process that, so a
controller switched off for a working day must not become a broadcast
every few seconds."""


def spread_offset(key: str, seconds: float) -> float:
    """A device's stable place in the interval, so rounds do not gather.

    The offset comes from the key rather than from chance, so a device keeps
    its place across restarts, and it never exceeds the interval or
    ``SPREAD_MAX``, whichever is shorter.
    """
    digest = int(hashlib.sha1(key.encode()).hexdigest()[:4], 16)
    return min(seconds, SPREAD_MAX) * digest / 0xFFFF


def search_delay(failures: int) -> float:
    """How long a device is left alone after this many failed searches."""
    return min(SEARCH_BACKOFF_START * 2.0 ** (failures - 1), SEARCH_BACKOFF_MAX)


@dataclass(frozen=True)
class PollPoint:
    reference: BACnetReference
    interval: timedelta


class BACnetIPSource(Source):
    """One ``bacnet-ip`` instance: its own socket, its own loop, its own devices."""

    def __init__(self, name: str, config: BaseModel, graph: GraphReader) -> None:
        super().__init__(name, config, graph)
        self.settings = (
            config
            if isinstance(config, BACnetIPConfig)
            else BACnetIPConfig.model_validate(config.model_dump())
        )
        self._lock = threading.Lock()
        self._assignment: list[AssignedPoint] = []
        self._resolved: ResolvedReferences | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        #: Set while ``start`` runs, and ``_ready`` once the loop and the
        #: application exist, so a tool asked for during start-up waits for
        #: them instead of opening a second application on the same port.
        self._starting = threading.Event()
        self._ready = threading.Event()
        self.sink: Sink | None = None
        self.status: StatusChannel | None = None
        self.app: Application | None = None
        #: The ceiling on requests in flight, built when the loop starts.
        self.gate: asyncio.Semaphore | None = None
        self._devices: dict[int, DevicePoller] = {}

    # --- binding -------------------------------------------------------------

    def resources(self) -> Iterable[str]:
        return [f"udp:{self.settings.local_ip}:{self.settings.port}"]

    def claim(self) -> set[str]:
        """Every point with a BACnet reference whose device is in this instance's scope.

        A reference the source recognises but cannot use is claimed too, when
        its device can be placed in scope or the instance has no restriction,
        so that the point is reported as rejected rather than unclaimed.
        """
        resolved = resolve_references(self.graph)
        with self._lock:
            self._resolved = resolved
        claimed = {
            point
            for point, reference in resolved.references.items()
            if self.settings.claims_device(
                reference.device_instance, reference.device_ip
            )
        }
        unrestricted = not self.settings.devices and self.settings.subnet is None
        for point, problem in resolved.problems.items():
            if problem.device_instance is not None:
                if self.settings.claims_device(
                    problem.device_instance, problem.device_ip
                ):
                    claimed.add(point)
            elif unrestricted:
                claimed.add(point)
        return claimed

    # --- operation -----------------------------------------------------------

    def start(self, sink: Sink, status: StatusChannel) -> None:
        self.sink, self.status = sink, status
        self._starting.set()
        try:
            asyncio.run(self._run())
        finally:
            self._starting.clear()

    def assign(self, points: Sequence[AssignedPoint]) -> None:
        with self._lock:
            self._assignment = list(points)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._reconcile)

    def stop(self) -> None:
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        # Built here rather than in __init__: a semaphore binds to the
        # loop it is first used on, and a restart brings a new loop.
        self.gate = asyncio.Semaphore(self.settings.max_in_flight)
        self.app = self._make_application()
        self._ready.set()
        try:
            self._reconcile()
            await self._stop_event.wait()
        finally:
            self._ready.clear()
            for poller in self._devices.values():
                poller.cancel()
            self._devices.clear()
            await asyncio.sleep(0)
            if self.app is not None:
                self.app.close()
            self.app = None
            self.gate = None
            self._loop = None
            self._stop_event = None

    def _make_application(self) -> Application:
        settings = self.settings
        device = DeviceObject(
            objectIdentifier=("device", settings.device_instance),
            objectName=settings.device_name or self.name,
            vendorIdentifier=settings.vendor_id,
            apduTimeout=int(settings.timeout.total_seconds() * 1000),
            numberOfApduRetries=settings.retries,
        )
        port_arguments: dict[str, Any] = {}
        if settings.bbmd is not None:
            try:
                from bacpypes3.basetypes import HostNPort

                port_arguments = {
                    "bacnetIPMode": "foreign",
                    "fdBBMDAddress": HostNPort(settings.bbmd.address),
                    "fdSubscriptionLifetime": int(settings.bbmd.ttl.total_seconds()),
                }
            except Exception:
                log.exception(
                    "%s: foreign device registration could not be set up", self.name
                )
        port = NetworkPortObject(
            f"{settings.local_ip}/{settings.prefix_length}:{settings.port}",
            objectIdentifier=("network-port", 1),
            objectName="bricklogger",
            **port_arguments,
        )
        return Application.from_object_list([device, port])

    def _reconcile(self) -> None:
        """Apply the assignment: resolve, group by device, start and stop pollers."""
        with self._lock:
            assignment = list(self._assignment)
            resolved = self._resolved
        if resolved is None or any(
            point.uri not in resolved.references and point.uri not in resolved.problems
            for point in assignment
        ):
            resolved = resolve_references(self.graph)
            with self._lock:
                self._resolved = resolved
        outcomes: list[Outcome] = []
        wanted: dict[int, dict[str, PollPoint]] = {}
        for point in assignment:
            reference = resolved.references.get(point.uri)
            if reference is None:
                problem = resolved.problems.get(point.uri)
                reason = problem.reason if problem else "no BACnet reference found"
                outcomes.append(Outcome(point.uri, "rejected", reason))
                continue
            if point.method != "poll":
                outcomes.append(
                    Outcome(
                        point.uri,
                        "unsupported",
                        f"method {point.method!r} is not offered",
                    )
                )
                continue
            interval = point.parameters.get("interval") or DEFAULT_INTERVAL
            wanted.setdefault(reference.device_instance, {})[point.uri] = PollPoint(
                reference, interval
            )
            outcomes.append(Outcome(point.uri, "active"))
        for instance in set(self._devices) - set(wanted):
            self._devices.pop(instance).cancel()
        for instance, points in wanted.items():
            poller = self._devices.get(instance)
            if poller is None:
                hint = next(iter(points.values())).reference.device_ip
                poller = DevicePoller(self, instance, hint)
                self._devices[instance] = poller
            poller.set_points(points)
        if self.status is not None and outcomes:
            self.status.outcomes(outcomes)

    # --- tools ---------------------------------------------------------------

    def run_tool(self, name: str, parameters: Mapping[str, Any]) -> Any:
        """Run a protocol tool: on the source's own loop while it runs, and on a
        temporary application of its own when the source is not started.
        """
        if self._starting.is_set() and not self._ready.is_set():
            # the daemon reports the instance running before the loop is up
            self._ready.wait(5.0)
        loop, app = self._loop, self.app
        if loop is not None and loop.is_running() and app is not None:
            future = asyncio.run_coroutine_threadsafe(
                tools.run(name, app, self.settings, self.graph, parameters, self.name),
                loop,
            )
            return future.result(timeout=tools.timeout_for(name, parameters))
        if name == "resolve":
            raise tools.ToolError("resolve needs the running daemon's working graph")
        return asyncio.run(self._run_offline(name, parameters))

    async def _run_offline(self, name: str, parameters: Mapping[str, Any]) -> Any:
        app = self._make_application()
        try:
            return await tools.run(
                name, app, self.settings, None, parameters, self.name
            )
        finally:
            app.close()

    def reject(self, point: str, reason: str) -> None:
        """Report a point as rejected after a read showed it cannot be logged."""
        if self.status is not None:
            self.status.outcomes([Outcome(point, "rejected", reason)])


class DevicePoller:
    """All the points of one device: discovery, one round per interval, metadata."""

    def __init__(
        self, source: BACnetIPSource, instance: int, ip_hint: str | None
    ) -> None:
        self.source = source
        self.instance = instance
        self.ip_hint = ip_hint
        self.address: Address | None = None
        self.points: dict[str, PollPoint] = {}
        self.tasks: dict[timedelta, asyncio.Task[None]] = {}
        self.skipped = 0
        self.search_failures = 0
        self.next_search = 0.0
        self.rpm_supported = True
        self.metadata_sent: set[str] = set()
        self.reachable: bool | None = None
        self.rejected: set[str] = set()

    def set_points(self, points: dict[str, PollPoint]) -> None:
        self.points = {
            uri: pp for uri, pp in points.items() if uri not in self.rejected
        }
        intervals = {pp.interval for pp in self.points.values()}
        for interval in set(self.tasks) - intervals:
            self.tasks.pop(interval).cancel()
        for interval in intervals - set(self.tasks):
            self.tasks[interval] = asyncio.create_task(self._poll_loop(interval))

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    async def _poll_loop(self, interval: timedelta) -> None:
        seconds = interval.total_seconds()
        await asyncio.sleep(spread_offset(f"{self.instance}:{seconds}", seconds))
        while True:
            started = time.monotonic()
            try:
                await self._poll_once(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "%s: polling device %d failed", self.source.name, self.instance
                )
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                missed = int(elapsed // seconds)
                self.skipped += missed
                self._report_device(reachable=self.reachable is not False)
                await asyncio.sleep(seconds - (elapsed % seconds))
            else:
                await asyncio.sleep(seconds - elapsed)

    async def _poll_once(self, interval: timedelta) -> None:
        points = [pp for pp in self.points.values() if pp.interval == interval]
        if not points:
            return
        if self.address is None and not await self._search():
            self._emit(
                [self._null(pp, "unreachable", datetime.now(UTC)) for pp in points]
            )
            return
        fresh = [pp for pp in points if pp.reference.point not in self.metadata_sent]
        if fresh:
            await self._read_metadata(fresh)
        observations: list[Observation] = []
        for start in range(0, len(points), RPM_CHUNK):
            chunk = points[start : start + RPM_CHUNK]
            observations.extend(await self._read_chunk(chunk))
            if self.address is None:
                break
        self._emit(observations)

    # --- discovery -----------------------------------------------------------

    def _gate(self) -> asyncio.Semaphore:
        """The instance's ceiling on requests in flight; every request passes it."""
        gate = self.source.gate
        assert gate is not None
        return gate

    async def _search(self) -> bool:
        """Look for the device, no oftener than the backoff allows.

        A failed search costs a broadcast that every device on the network has
        to process, so a device that is simply switched off is left alone for
        longer and longer instead of being searched for on every round.
        """
        now = time.monotonic()
        if now < self.next_search:
            return False
        if await self._discover():
            self.search_failures = 0
            self.next_search = 0.0
            return True
        self.search_failures += 1
        self.next_search = now + search_delay(self.search_failures)
        return False

    async def _discover(self) -> bool:
        app = self.source.app
        if app is None:
            return False
        async with self._gate():
            address = await tools.locate(
                app,
                self.source.settings,
                self.instance,
                self.ip_hint,
                self.source.settings.timeout.total_seconds(),
            )
        if address is not None:
            self.address = address
            self._report_device(reachable=True)
            return True
        self._report_device(reachable=False, error="no answer to Who-Is")
        return False

    def _report_device(self, *, reachable: bool, error: str | None = None) -> None:
        status = self.source.status
        changed = reachable != self.reachable
        self.reachable = reachable
        if status is not None and (changed or self.skipped or error):
            status.device(
                str(self.instance),
                reachable=reachable,
                error=error,
                skipped_rounds=self.skipped,
            )

    # --- reading -------------------------------------------------------------

    async def _read_chunk(self, chunk: list[PollPoint]) -> list[Observation]:
        app = self.source.app
        if app is None or self.address is None:
            return []
        if self.rpm_supported:
            by_object: dict[str, list[str]] = {}
            for pp in chunk:
                properties = by_object.setdefault(
                    pp.reference.object_identifier, [STATUS_FLAGS]
                )
                if pp.reference.property_name not in properties:
                    properties.append(pp.reference.property_name)
            parameters: list[Any] = []
            for identifier, properties in by_object.items():
                parameters += [ObjectIdentifier(identifier), properties]
            try:
                async with self._gate():
                    results = await app.read_property_multiple(self.address, parameters)
            except RejectPDU as exc:
                log.info(
                    "%s: device %d rejects RPM (%s); reading one by one",
                    self.source.name,
                    self.instance,
                    exc,
                )
                self.rpm_supported = False
                return await self._read_one_by_one(chunk)
            except AbortPDU as exc:
                return self._lost(chunk, f"abort: {exc}")
            except ErrorPDU as exc:
                stamp = datetime.now(UTC)
                return [self._null(pp, "read_error", stamp, str(exc)) for pp in chunk]
            except TimeoutError:
                return self._lost(chunk, "timeout")
            stamp = datetime.now(UTC)
            read: dict[tuple[str, str], Any] = {}
            for objid, prop, _, value in results:
                read[(str(objid), str(prop))] = value
            observations: list[Observation] = []
            for pp in chunk:
                key = pp.reference.object_identifier
                value = read.get((key, pp.reference.property_name))
                if value is None:
                    value = ErrorType(
                        errorClass="property", errorCode="unknown-property"
                    )
                flags = read.get((key, STATUS_FLAGS))
                observation = self._observe(pp, value, flags, stamp)
                if observation is not None:
                    observations.append(observation)
            self._report_device(reachable=True)
            return observations
        return await self._read_one_by_one(chunk)

    async def _read_one_by_one(self, chunk: list[PollPoint]) -> list[Observation]:
        app = self.source.app
        observations: list[Observation] = []
        for pp in chunk:
            if app is None or self.address is None:
                break
            objid = ObjectIdentifier(pp.reference.object_identifier)
            try:
                async with self._gate():
                    value = await app.read_property(
                        self.address, objid, pp.reference.property_name
                    )
            except ErrorPDU as exc:
                observations.append(
                    self._null(pp, "read_error", datetime.now(UTC), str(exc))
                )
                continue
            except (AbortPDU, TimeoutError) as exc:
                observations.extend(self._lost([pp], f"abort: {exc}"))
                break
            except RejectPDU as exc:
                observations.append(
                    self._null(pp, "read_error", datetime.now(UTC), str(exc))
                )
                continue
            flags: Any = None
            try:
                async with self._gate():
                    flags = await app.read_property(self.address, objid, STATUS_FLAGS)
            except (ErrorRejectAbortNack, Exception):
                flags = None
            observation = self._observe(pp, value, flags, datetime.now(UTC))
            if observation is not None:
                observations.append(observation)
        if self.address is not None:
            self._report_device(reachable=True)
        return observations

    def _lost(self, chunk: list[PollPoint], error: str) -> list[Observation]:
        """No answer: every point yields unreachable; the device is looked up again."""
        stamp = datetime.now(UTC)
        self.address = None
        self._report_device(reachable=False, error=error)
        return [self._null(pp, "unreachable", stamp) for pp in chunk]

    def _observe(
        self, pp: PollPoint, value: Any, flags: Any, stamp: datetime
    ) -> Observation | None:
        reference = pp.reference
        if isinstance(value, ErrorType):
            detail = f"{value.errorClass}: {value.errorCode}"
            return self._null(pp, "read_error", stamp, detail)
        if (
            reference.property_name == PRESENT_VALUE
            and flags is not None
            and not isinstance(flags, ErrorType)
        ):
            try:
                bits = list(flags)
            except TypeError:
                bits = []
            if len(bits) >= 4:
                if bits[1]:
                    return self._null(pp, "fault", stamp)
                if bits[3]:
                    return self._null(pp, "out_of_service", stamp)
                if bits[2]:
                    return self._null(pp, "overridden", stamp)
        classified = classify(reference.object_type, reference.property_name, value)
        if classified is None:
            self.rejected.add(reference.point)
            self.points.pop(reference.point, None)
            self.source.reject(reference.point, UNSUPPORTED)
            return None
        value_type, python_value = classified
        if value_type == "null":
            return Observation(reference.point, stamp, "null", reason="no_value")
        if (
            reference.point not in self.metadata_sent
            and reference.property_name != PRESENT_VALUE
        ):
            self._send_metadata(
                [
                    PointMetadata(
                        reference.point,
                        value_type,
                        None,
                        enumeration_texts(value),
                        None,
                    )
                ]
            )
        return Observation(reference.point, stamp, value_type, python_value)

    @staticmethod
    def _null(
        pp: PollPoint, reason: NullReason, stamp: datetime, detail: str | None = None
    ) -> Observation:
        if detail:
            log.debug("%s: %s (%s)", pp.reference.point, reason, detail)
        return Observation(pp.reference.point, stamp, "null", reason=reason)

    # --- metadata ------------------------------------------------------------

    async def _read_metadata(self, points: list[PollPoint]) -> None:
        """Units and texts for Present_Value points; others wait for a first value."""
        app = self.source.app
        if app is None or self.address is None:
            return
        wanted: list[PollPoint] = []
        parameters: list[Any] = []
        for pp in points:
            reference = pp.reference
            if reference.property_name != PRESENT_VALUE:
                continue
            properties = _metadata_properties(reference.object_type)
            if not properties:
                expected = _expected_type(reference.object_type)
                self._send_metadata([PointMetadata(reference.point, expected)])
                continue
            wanted.append(pp)
            parameters += [ObjectIdentifier(reference.object_identifier), properties]
        if not wanted:
            return
        try:
            async with self._gate():
                results = await app.read_property_multiple(self.address, parameters)
        except (ErrorRejectAbortNack, Exception):
            results = []
        by_object: dict[str, dict[str, Any]] = {}
        for objid, prop, _, value in results:
            if not isinstance(value, ErrorType):
                by_object.setdefault(str(objid), {})[str(prop)] = value
        entries: list[PointMetadata] = []
        for pp in wanted:
            reference = pp.reference
            read = by_object.get(reference.object_identifier, {})
            entries.append(_metadata_from(reference, read))
        self._send_metadata(entries)

    def _send_metadata(self, entries: list[PointMetadata]) -> None:
        sink = self.source.sink
        for entry in entries:
            self.metadata_sent.add(entry.point)
        if sink is not None and entries:
            sink.metadata(entries)

    def _emit(self, observations: list[Observation]) -> None:
        sink = self.source.sink
        if sink is not None and observations:
            sink.observations(observations)


def _metadata_properties(object_type: str) -> list[str]:
    if object_type in ANALOG_TYPES or object_type in INTEGER_TYPES:
        return ["units"]
    if object_type in BINARY_TYPES:
        return ["active-text", "inactive-text"]
    if object_type in MULTISTATE_TYPES:
        return ["state-text"]
    return []


def _expected_type(object_type: str) -> ValueType:
    if object_type in ANALOG_TYPES:
        return "number"
    if object_type in BINARY_TYPES:
        return "boolean"
    if object_type in MULTISTATE_TYPES:
        return "enum"
    if object_type in INTEGER_TYPES:
        return "integer"
    if object_type == "character-string-value":
        return "string"
    if object_type == "date-time-value":
        return "datetime"
    return "number"


def _metadata_from(reference: BACnetReference, read: dict[str, Any]) -> PointMetadata:
    value_type = _expected_type(reference.object_type)
    unit = protocol_unit(read.get("units"))
    enum_texts: dict[int, str] | None = None
    boolean_texts: tuple[str, str] | None = None
    if "state-text" in read:
        enum_texts = {
            index + 1: str(text) for index, text in enumerate(read["state-text"])
        }
    if "active-text" in read or "inactive-text" in read:
        inactive = str(read.get("inactive-text", "inactive"))
        active = str(read.get("active-text", "active"))
        boolean_texts = (inactive, active)
    return PointMetadata(reference.point, value_type, unit, enum_texts, boolean_texts)
