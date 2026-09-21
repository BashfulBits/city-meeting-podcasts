"""Benchmark script for transcript_quality.py hot path parsing functions."""

import json
import time

from citypods.transcript_quality import _parse_words_payload


def generate_test_payload(num_segments: int = 500, words_per_segment: int = 100) -> bytes:
    payload = {
        "schema": "2",
        "basis": "served",
        "segments": [
            {
                "start": seg_i * 30.0,
                "end": (seg_i + 1) * 30.0,
                "text": f"Segment text {seg_i}",
                "words": [
                    {
                        "w": f"word_{seg_i}_{w_i}",
                        "s": seg_i * 30.0 + w_i * 0.3,
                        "e": seg_i * 30.0 + w_i * 0.3 + 0.25,
                        "p": 0.98,
                    }
                    for w_i in range(words_per_segment)
                ],
            }
            for seg_i in range(num_segments)
        ],
    }
    return json.dumps(payload).encode("utf-8")


def run_benchmark(iterations: int = 100):
    data = generate_test_payload(num_segments=500, words_per_segment=100)
    print(f"Payload size: {len(data) / 1024:.2f} KB ({500 * 100} words)")

    # Warmup
    _parse_words_payload(data)

    start_time = time.perf_counter()
    for _ in range(iterations):
        res = _parse_words_payload(data)
    end_time = time.perf_counter()

    total_time = end_time - start_time
    avg_time_ms = (total_time / iterations) * 1000
    print(f"Total time for {iterations} iterations: {total_time:.4f}s")
    print(f"Average time per invocation: {avg_time_ms:.3f} ms")
    print(f"Parsed items per payload: {len(res)}")
    return total_time, avg_time_ms


if __name__ == "__main__":
    run_benchmark()
