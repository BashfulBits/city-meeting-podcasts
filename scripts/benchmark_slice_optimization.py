"""Benchmark script comparing slicing materialized lists vs `itertools.islice`.
"""

import itertools
import sys
import time


def benchmark():
    # Simulate a large records dictionary with 100,000 items
    num_items = 100_000
    sample_size = 10
    records = {
        f"guid-{i}": {"title": f"Episode {i}", "body": f"Body {i}"} for i in range(num_items)
    }

    iterations = 1000

    # 1. Full list materialization
    t0 = time.perf_counter()
    for _ in range(iterations):
        sample_titles_old = [rec.get("title") for rec in list(records.values())[:sample_size]]
    t_old = time.perf_counter() - t0

    # 2. itertools.islice
    t0 = time.perf_counter()
    for _ in range(iterations):
        sample_titles_new = [
            rec.get("title") for rec in itertools.islice(records.values(), sample_size)
        ]
    t_new = time.perf_counter() - t0

    assert sample_titles_old == sample_titles_new

    # Memory allocation check for list materialization vs dict values iterator
    list_mem = sys.getsizeof(list(records.values()))
    islice_mem = sys.getsizeof(itertools.islice(records.values(), sample_size))

    print(f"Iterations: {iterations}, Record count: {num_items}, Sample size: {sample_size}")
    print(f"List materialization time: {t_old * 1000:.3f} ms")
    print(f"itertools.islice time:     {t_new * 1000:.3f} ms")
    print(f"Speedup:                   {t_old / t_new:.2f}x faster")
    print(f"Memory for full list:      {list_mem / 1024:.2f} KB")
    print(f"Memory for islice iterator:{islice_mem} bytes")


if __name__ == "__main__":
    benchmark()
