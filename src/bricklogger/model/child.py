"""Validation and inference in a child process.

The work is CPU-bound Python for minutes on a large model; in the daemon's own
process it would hold the interpreter lock against the API and the sources.
The child is spawned, not forked, because the daemon has threads and an open
store. See ``docs/architecture.md``, "Inference".
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Iterable
from multiprocessing.connection import Connection
from typing import Any

from bricklogger.model.inference import (
    Inference,
    ModelInvalid,
    ModelReport,
    ModelUnreadable,
    validate_and_infer,
    validate_model,
)
from bricklogger.sdk.declaration import Vocabulary

JOIN_SECONDS = 10.0


class ChildEnded(Exception):
    """The child process ended without handing back a result."""


def _work(
    conn: Connection,
    data: bytes,
    fmt: str,
    vocabularies: tuple[Vocabulary, ...],
    infer: bool,
) -> None:
    """The child's side: every message is a ``(kind, payload)`` pair."""

    def step(name: str) -> None:
        conn.send(("step", name))

    try:
        result: Inference | ModelReport
        if infer:
            result = validate_and_infer(data, fmt, step, vocabularies=vocabularies)
        else:
            step("validating")
            result = validate_model(data, fmt, vocabularies)
    except ModelInvalid as exc:
        conn.send(("invalid", exc.report))
    except ModelUnreadable as exc:
        conn.send(("unreadable", str(exc)))
    except Exception as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    else:
        conn.send(("result", result))
    finally:
        conn.close()


def _run(
    data: bytes,
    fmt: str,
    vocabularies: Iterable[Vocabulary],
    on_step: Callable[[str], None] | None,
    infer: bool,
) -> Any:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_work,
        args=(sender, data, fmt, tuple(vocabularies), infer),
        name="bricklogger-model",
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        while True:
            try:
                kind, payload = receiver.recv()
            except EOFError:
                process.join(JOIN_SECONDS)
                raise ChildEnded(
                    "the validation process ended without a result "
                    f"(exit code {process.exitcode})"
                ) from None
            if kind == "step":
                if on_step is not None:
                    on_step(payload)
            elif kind == "result":
                return payload
            elif kind == "invalid":
                raise ModelInvalid(payload)
            elif kind == "unreadable":
                raise ModelUnreadable(payload)
            else:
                raise RuntimeError(payload)
    finally:
        receiver.close()
        process.join(JOIN_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()


def validate_and_infer_in_child(
    data: bytes,
    fmt: str,
    on_step: Callable[[str], None] | None,
    vocabularies: Iterable[Vocabulary],
) -> Inference:
    """:func:`validate_and_infer` in a child process, with the same exceptions;
    :class:`ChildEnded` when the child dies without a result."""
    result: Inference = _run(data, fmt, vocabularies, on_step, infer=True)
    return result


def validate_model_in_child(
    data: bytes, fmt: str, vocabularies: Iterable[Vocabulary]
) -> ModelReport:
    """:func:`validate_model` in a child process."""
    result: ModelReport = _run(data, fmt, vocabularies, None, infer=False)
    return result
