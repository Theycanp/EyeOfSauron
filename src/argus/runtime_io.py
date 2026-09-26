"""Bounded blocking network calls that cannot delay process shutdown."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import ParamSpec, TypeVar


_P = ParamSpec("_P")
_R = TypeVar("_R")
_network_slots = threading.BoundedSemaphore(16)


async def run_network_call(function: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
    """Run blocking I/O with bounded threads; cancellation never waits for the call.

    A cancelled call may finish later, so the callable must not access the
    repository or mark durable work complete. Its slot stays occupied until it
    actually finishes, preventing an unlimited build-up of stranded threads.
    """
    slot = _network_slots
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
        threading.Thread(target=invoke, name="argus-network", daemon=True).start()
    except BaseException:
        slot.release()
        raise
    return await asyncio.wrap_future(result)
