"""Exponential backoff utility for Modbus and MQTT reconnection."""

from __future__ import annotations

import random


def exponential_backoff(
    attempt: int,
    initial: float = 1.0,
    multiplier: float = 2.0,
    max_delay: float = 60.0,
    jitter: float = 0.1,
) -> float:
    """Compute an exponential backoff delay with random jitter.

    Args:
        attempt: Zero-indexed attempt number.
        initial: Base delay in seconds for the first attempt.
        multiplier: Factor applied per attempt.
        max_delay: Upper bound on the base delay before jitter.
        jitter: Fractional jitter range (±). E.g. 0.1 means ±10%.

    Returns:
        Delay in seconds. Never exceeds ``max_delay * (1 + jitter)``.
    """
    base = min(initial * (multiplier**attempt), max_delay)
    jitter_factor = 1.0 + random.uniform(-jitter, jitter)
    return base * jitter_factor
