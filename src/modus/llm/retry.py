from __future__ import annotations

import random
import threading
import time
from typing import Any

_jitter_counter = 0
_jitter_lock = threading.Lock()

def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """带抖动的指数退避，防止多个会话同时重试造成 thundering herd"""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        seed = _jitter_counter
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = random.Random(seed + attempt).uniform(0, jitter_ratio * delay)
    return delay + jitter