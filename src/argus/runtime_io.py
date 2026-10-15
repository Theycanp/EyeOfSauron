"""Bounded blocking I/O calls that cannot delay process shutdown."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import ParamSpec, TypeVar


_P = ParamSpec("_P")
_R = TypeVar("_R")
_network_slots = threading.BoundedSemaphore(16)
_local_slots = threading.BoundedSemaphore(2)


async def run_network_call(function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
    """Run blocking I/O with bounded threads; cancellation never waits for the call.

    A cancelled call may finish later, so the callable must not access the
    repository or mark durable work complete. Its slot stays occupied until it
    actually finishes, preventing an unlimited build-up of stranded threads.
    """
    return await _run_bounded_call(_network_slots, function, *args, **kwargs)


async def run_local_call(function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
    """Keep local probes independent of blocked DNS and outbound calls."""
    return await _run_bounded_call(_local_slots, function, *args, **kwargs)


async def _run_bounded_call(
    slot: threading.BoundedSemaphore, function: Callable[_P, _R], /,
    *args: _P.args, **kwargs: _P.kwargs,
) -> _R:
    while not slot.acquire(blocking=False):
        await asyncio.sleep(0.1)
    result: concurrent.futures.Future[_R] = concurrent.futures.Future()

    def invoke() -> None:
        try:
            if result.set_running_or_notify_cancel():
                try:
                    result.set_result(function(*args, **kwargs))
                except BaseException as exc:
                    result.set_exception(exc)
        finally:
            slot.release()

    try:
        threading.Thread(target=invoke, name="argus-io", daemon=True).start()
    except BaseException:
        slot.release()
        raise
    return await asyncio.wrap_future(result)
