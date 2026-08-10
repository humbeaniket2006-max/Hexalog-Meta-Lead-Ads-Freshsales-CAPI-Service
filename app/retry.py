import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


class RetryExhaustedError(Exception):
    pass


def retry_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    return min(cap, base * (2 ** (attempt - 1))) + random.uniform(0, base)


async def retry_async(operation: Callable[[], Awaitable[T]], attempts: int) -> T:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            response = getattr(exc, "response", None)
            retryable = response is None or response.status_code == 429 or 500 <= response.status_code <= 599
            if not retryable:
                raise
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(retry_delay(attempt))
    raise RetryExhaustedError(str(last_error))
