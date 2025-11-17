import time
from functools import lru_cache
from src.queue.util import cache_maintainer


def test_cache_maintainer():
    calls = {"count": 0}

    @cache_maintainer(1)       # clear every 1 second
    @lru_cache(None)
    def compute(x):
        calls["count"] += 1
        return x * 2

    # First call — calls function
    assert compute(5) == 10
    assert calls["count"] == 1

    # Second call immediately — should use cache
    assert compute(5) == 10
    assert calls["count"] == 1  # NO new calls

    # Wait for 1 second → decorator clears cache
    time.sleep(1.1)

    # Next call — should trigger a new computation
    assert compute(5) == 10
    assert calls["count"] == 2
