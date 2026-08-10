import hashlib
import hmac
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status
from pydantic import SecretStr


def verify_meta_signature(body: bytes, header_value: str | None, app_secret: SecretStr) -> bool:
    if not header_value or not header_value.startswith("sha256="):
        return False
    supplied = header_value.removeprefix("sha256=")
    expected = hmac.new(app_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def verify_shared_secret(supplied: str | None, expected: SecretStr) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected.get_secret_value())


def pii_ref(value: str | None) -> str | None:
    if value is None:
        return None
    digest = hashlib.sha256(value.strip().lower().encode()).hexdigest()
    return digest[:12]


class InMemoryRateLimiter:
    def __init__(self, limit_per_minute: int, clock: Callable[[], float] = time.time) -> None:
        self.limit = limit_per_minute
        self.clock = clock
        self.requests: defaultdict[str, deque[float]] = defaultdict(deque)

    async def check(self, request: Request) -> None:
        now = self.clock()
        client = request.client.host if request.client else "unknown"
        window = self.requests[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
        window.append(now)
