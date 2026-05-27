"""
Shared utilities — retry with exponential backoff for external API calls.
"""

import logging
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


def retry(max_attempts: int = 3, base_delay: float = 2.0) -> Callable[[Callable[..., T]], T]:
    """Decorator: retry an API call with exponential backoff on any exception."""
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        log.warning(f"Retry {attempt}/{max_attempts} after {delay:.0f}s: {e}")
                        time.sleep(delay)
                    else:
                        log.error(f"All {max_attempts} attempts failed: {e}")
            raise last_exc  # type: ignore
        return wrapper
    return decorator