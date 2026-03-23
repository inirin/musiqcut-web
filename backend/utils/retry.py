import asyncio
import functools
from typing import Callable


def with_retry(max_attempts: int = 3, backoff_base: float = 1.0):
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt == max_attempts - 1:
                        raise
                    wait = backoff_base * (2 ** attempt)
                    await asyncio.sleep(wait)
            raise last_exc
        return wrapper
    return decorator
