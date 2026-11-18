"""Deterministic IoT aggregation one-shot container for the CEI teaching lab.

Approach A: every placement reconstructs the same synthetic sensor dataset.
The raw samples are NOT transferred from the laptop to the remote execution
node. Input/output byte counts describe a *logical* application data flow.
Only aggregation/transformation (not dataset generation or Orion reporting)
is included in computeTimeMs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time

from cei_lab.entities import status_patch
from cei_lab.orion import OrionClient


BYTES_PER_RAW_RECORD = 16  # modeled binary sample: 8-byte timestamp + 8-byte reading


def result_payload(
    records: int = 500_000,
    window: int = 500,
    seed: int = 42,
    threshold: int = 9500,
    work_factor: int = 45,
) -> dict:
    """Aggregate stable integer samples and return a compact, verifiable result.

    We deliberately perform additional deterministic per-sample arithmetic
    (work_factor) as part of a data-validation signature, so the workload has
    enough computational weight to compare heterogeneous CEI placements.
    """
    if not 100 <= records <= 2_000_000:
        raise ValueError("records must be between 100 and 2,000,000")
    if not 1 <= window <= records:
        raise ValueError("window must be between 1 and records")
    if not 0 <= threshold <= 10_000:
        raise ValueError("threshold must be in [0, 10000]")
    if not 1 <= work_factor <= 200:
        raise ValueError("work_factor must be between 1 and 200")
    if not 0 <= seed <= (2**32 - 1):
        raise ValueError("seed must be between 0 and 2**32-1")

    # Pre-provisioned-equivalent data: same seed -> same readings on each node.
    # This preparation is outside the timed processing boundary.
    rng = random.Random(seed)
    readings = [rng.randrange(0, 10_000) for _ in range(records)]

    t0 = time.perf_counter_ns()
    output_lines = []
    total_alerts = 0
    for start in range(0, records, window):
        segment = readings[start:start + window]
        count = len(segment)
        sum_values = 0
        min_value = 10_000
        max_value = -1
        alerts = 0
        signature = 0
        for value in segment:
            sum_values += value
            if value < min_value:
                min_value = value
            if value > max_value:
                max_value = value
            if value >= threshold:
                alerts += 1
            # A repeatable integer-only data-validation signature.
            # The result matters (it is included in the output checksum).
            for _ in range(work_factor):
                signature = (signature * 1664525 + value + 1013904223) & 0xFFFFFFFF
        total_alerts += alerts
        row = {
            "window": start // window,
            "samples": count,
            "averageMilliUnits": (sum_values * 1000) // count,
            "minimum": min_value,
            "maximum": max_value,
            "alerts": alerts,
            "signature": signature,
        }
        output_lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))

    encoded_output = ("\n".join(output_lines) + "\n").encode("utf-8")
    duration_ms = round((time.perf_counter_ns() - t0) / 1_000_000, 3)
    output_bytes = len(encoded_output)
    input_bytes = records * BYTES_PER_RAW_RECORD
    return {
        "computeTimeMs": duration_ms,
        "inputBytes": input_bytes,
        "outputBytes": output_bytes,
        "recordsProcessed": records,
        "windowsProduced": len(output_lines),
        "alertsCount": total_alerts,
        "dataReductionRatio": round(input_bytes / output_bytes, 4),
        "outputChecksum": "sha256:" + hashlib.sha256(encoded_output).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finite IoT aggregation job for CEI lab")
    parser.add_argument("--records", type=int, default=int(os.getenv("CEI_IOT_RECORDS", "500000")))
    parser.add_argument("--window", type=int, default=int(os.getenv("CEI_IOT_WINDOW", "500")))
    parser.add_argument("--seed", type=int, default=int(os.getenv("CEI_IOT_SEED", "42")))
    parser.add_argument("--threshold", type=int, default=int(os.getenv("CEI_IOT_THRESHOLD", "9500")))
    parser.add_argument("--work-factor", type=int, default=int(os.getenv("CEI_IOT_WORK_FACTOR", "45")))
    args = parser.parse_args(argv)

    execution_id = os.getenv("CEI_EXECUTION_ID")
    if not execution_id:
        print("CEI_EXECUTION_ID missing", file=sys.stderr)
        return 2
    try:
        client = OrionClient.from_env()
    except (ValueError, RuntimeError) as exc:
        print(f"Orion configuration failed: {exc}", file=sys.stderr)
        return 2
    try:
        client.patch_entity(execution_id, status_patch("running"))
        result = result_payload(args.records, args.window, args.seed, args.threshold, args.work_factor)
        client.patch_entity(execution_id, status_patch("completed", result=result))
    except Exception as exc:
        try:
            client.patch_entity(execution_id, status_patch("failed", error=type(exc).__name__))
        except Exception:
            pass
        print(f"IoT aggregation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"IoT job completed: {result['recordsProcessed']} records; "
          f"{result['inputBytes']} -> {result['outputBytes']} logical bytes; "
          f"{result['computeTimeMs']} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
