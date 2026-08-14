"""Runtime helpers shared by media-effect commands and processors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


def cancellable_to_thread(func: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    """Run blocking media work without abandoning it when the caller times out.

    Python cannot forcibly stop a running worker thread. Shielding the worker and
    waiting for it during cancellation keeps the media semaphore held until the
    work has actually stopped, preventing timed-out commands from accumulating
    untracked CPU work in the background.
    """

    @wraps(func)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, partial(func, *args, **kwargs))
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(worker)
                if cancelled is not None:
                    raise cancelled
                return result
            except asyncio.CancelledError as error:
                cancelled = error
                if worker.done():
                    # Retrieve a completed worker exception before propagating
                    # cancellation so asyncio does not report it as unhandled.
                    try:
                        worker.result()
                    except Exception:
                        pass
                    raise
            except Exception:
                if cancelled is not None:
                    raise cancelled
                raise

    return wrapped
