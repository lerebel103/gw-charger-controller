"""Unit tests for the exponential backoff utility."""

from __future__ import annotations

from unittest.mock import patch

from app.backoff import exponential_backoff


class TestExponentialBackoff:
    """Tests for exponential_backoff()."""

    def test_attempt_zero_returns_initial(self):
        """Attempt 0 should return ~initial (within jitter)."""
        with patch("app.backoff.random.uniform", return_value=0.0):
            assert exponential_backoff(0) == 1.0

    def test_delay_doubles_each_attempt(self):
        """With no jitter the delay should double per attempt."""
        with patch("app.backoff.random.uniform", return_value=0.0):
            assert exponential_backoff(0) == 1.0
            assert exponential_backoff(1) == 2.0
            assert exponential_backoff(2) == 4.0
            assert exponential_backoff(3) == 8.0

    def test_delay_capped_at_max(self):
        """Delay should never exceed max_delay (before jitter)."""
        with patch("app.backoff.random.uniform", return_value=0.0):
            result = exponential_backoff(100, max_delay=60.0)
            assert result == 60.0

    def test_jitter_positive(self):
        """Positive jitter increases the delay."""
        with patch("app.backoff.random.uniform", return_value=0.1):
            result = exponential_backoff(0, initial=10.0, jitter=0.1)
            assert result == 11.0

    def test_jitter_negative(self):
        """Negative jitter decreases the delay."""
        with patch("app.backoff.random.uniform", return_value=-0.1):
            result = exponential_backoff(0, initial=10.0, jitter=0.1)
            assert result == 9.0

    def test_never_exceeds_absolute_max(self):
        """Even with max positive jitter, result <= max_delay * (1 + jitter)."""
        with patch("app.backoff.random.uniform", return_value=0.1):
            result = exponential_backoff(100, max_delay=60.0, jitter=0.1)
            assert result <= 60.0 * 1.1

    def test_custom_parameters(self):
        """Custom initial, multiplier, and max_delay work correctly."""
        with patch("app.backoff.random.uniform", return_value=0.0):
            assert exponential_backoff(0, initial=2.0, multiplier=3.0) == 2.0
            assert exponential_backoff(1, initial=2.0, multiplier=3.0) == 6.0
            assert exponential_backoff(2, initial=2.0, multiplier=3.0) == 18.0
            assert exponential_backoff(3, initial=2.0, multiplier=3.0, max_delay=20.0) == 20.0
