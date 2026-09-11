import pytest

from auto_eval.request_rate_limit import SmoothRequestRateLimiter


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_smooth_limiter_spaces_requests_evenly() -> None:
    fake_time = _FakeTime()
    limiter = SmoothRequestRateLimiter(
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    waits = [await limiter.acquire(2, 1.0) for _ in range(4)]

    assert waits == pytest.approx([0.0, 0.5, 0.5, 0.5])
    assert fake_time.sleeps == pytest.approx([0.5, 0.5, 0.5])


@pytest.mark.asyncio
async def test_smooth_limiter_uses_new_rate_for_each_reservation() -> None:
    fake_time = _FakeTime()
    limiter = SmoothRequestRateLimiter(
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    assert await limiter.acquire(10, 1.0) == 0.0
    assert await limiter.acquire(5, 1.0) == pytest.approx(0.1)
    assert await limiter.acquire(5, 1.0) == pytest.approx(0.2)
