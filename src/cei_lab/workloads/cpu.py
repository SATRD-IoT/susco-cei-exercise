"""Deterministic one-shot CPU job that directly PATCHes Orion-LD.

Requires CEI_EXECUTION_ID, CEI_ORION_URL, CEI_ORION_MODE and (if external)
CEI_ORION_JWT. It uses the exact entity-level PATCH endpoint requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

from cei_lab.entities import status_patch
from cei_lab.orion import OrionClient


def count_primes(start: int, stop: int) -> int:
    count = 0
    for value in range(max(2, start), stop):
        end = math.isqrt(value)
        prime = True
        for d in range(2, end + 1):
            if value % d == 0:
                prime = False
                break
        if prime:
            count += 1
    return count


def compute(limit: int, workers: int) -> int:
    if limit < 2:
        raise ValueError("limit must be >= 2")
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if workers == 1:
        return count_primes(2, limit + 1)
    # Disjoint ranges guarantee the same answer for any supported worker count.
    span = (limit - 1 + workers - 1) // workers
    ranges = [(lo, min(limit + 1, lo + span)) for lo in range(2, limit + 1, span)]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(count_primes, lo, hi) for lo, hi in ranges]
        return sum(f.result() for f in futures)


def result_payload(limit: int, workers: int) -> dict:
    args = {"limit": limit, "workers": workers}
    start_ns = time.perf_counter_ns()
    prime_count = compute(limit, workers)
    elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
    # The checksum covers only the mathematical result, not the worker count,
    # hardware, container metadata or timestamps.
    logical_output = json.dumps({"limit": limit, "primeCount": prime_count}, sort_keys=True, separators=(",", ":")).encode()
    logical_input = json.dumps({"limit": limit}, sort_keys=True, separators=(",", ":")).encode()
    return {
        "computeTimeMs": elapsed_ms,
        "inputBytes": len(logical_input),
        "outputBytes": len(logical_output),
        "recordsProcessed": limit - 1,
        "primeCount": prime_count,
        "outputChecksum": "sha256:" + hashlib.sha256(logical_output).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single-shot CPU workload for the CEI lab")
    parser.add_argument("--limit", type=int, default=int(os.getenv("CEI_CPU_LIMIT", "2000000")))
    parser.add_argument("--workers", type=int, default=int(os.getenv("CEI_CPU_WORKERS", "1")))
    args = parser.parse_args(argv)

    execution_id = os.environ.get("CEI_EXECUTION_ID")
    if not execution_id:
        print("Missing CEI_EXECUTION_ID", file=sys.stderr)
        return 2
    try:
        orion = OrionClient.from_env()
    except (ValueError, RuntimeError) as exc:
        print(f"Cannot configure Orion-LD client: {exc}", file=sys.stderr)
        return 2
    try:
        orion.patch_entity(execution_id, status_patch("running"))
        result = result_payload(args.limit, args.workers)
        # Atomically publish measurements + completion state in one PATCH.
        orion.patch_entity(execution_id, status_patch("completed", result=result))
    except Exception as exc:
        # Best-effort reporting. A hard crash or network failure can prevent this:
        # the controller must still enforce a timeout and/or failure state.
        try:
            orion.patch_entity(execution_id, status_patch("failed", error=type(exc).__name__))
        except Exception:
            pass
        print(f"CPU job failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"CPU job completed: primeCount={result['primeCount']}, computeTimeMs={result['computeTimeMs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
