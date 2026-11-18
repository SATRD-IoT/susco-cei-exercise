"""Transparent teaching model: measured compute durations + assumed energy/network.

CPU: E=P_eff*T, neglect application transfer.
IoT: laptop is the logical source, cloud is the result destination.
  laptop: output travels laptop->cloud
  edge: input travels laptop->edge; output travels edge->cloud
  cloud: input travels laptop->cloud
Approach A: the above application transfers are modeled, not performed.
No physical energy metering, embodied carbon, setup/idle/facility allocation.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

PLACEMENTS = ("laptop", "edge", "cloud")
WORKLOADS = ("cpu_compute", "iot_aggregation")


def load_scenario(path: str | Path) -> dict[str, Any]:
    profile = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assumptions = profile.get("assumptions", {})
    for place in PLACEMENTS:
        if float(assumptions["power_w"][place]) <= 0:
            raise ValueError("All effective workload power assumptions must be positive")
    for path_id in ("laptop_edge", "edge_cloud", "laptop_cloud"):
        values = assumptions["paths"][path_id]
        if float(values["energy_uj_per_bit"]) < 0 or float(values["bandwidth_mbps"]) <= 0 or float(values["fixed_latency_ms"]) < 0:
            raise ValueError("Invalid network intensity, rate or fixed latency")
    for workload in WORKLOADS:
        if float(assumptions["deadline_s"][workload]) <= 0:
            raise ValueError("Deadlines must be positive")
    return profile


def observation(*, workload: str, placement: str, repetition: int, run: Any) -> dict[str, Any]:
    """Convert LocalRun/RemoteRun into a plain record for notebook and explorer."""
    if workload not in WORKLOADS or placement not in PLACEMENTS:
        raise ValueError("Unknown workload/placement")
    result = run.result
    return {
        "workload": workload,
        "placement": placement,
        "repetition": repetition,
        "executionId": run.execution_id,
        "computeTimeMs": result["computeTimeMs"],
        "jobWallTimeS": getattr(run, "observed_wall_time_s", getattr(run, "job_wall_time_s", None)),
        "inputBytes": result["inputBytes"],
        "outputBytes": result["outputBytes"],
        "recordsProcessed": result["recordsProcessed"],
        "outputChecksum": result["outputChecksum"],
    }


def compare(
    records: Sequence[Mapping[str, Any]],
    workload: str,
    profile: Mapping[str, Any],
    *,
    deadline_s: float | None = None,
    network_energy_multiplier: float = 1.0,
    bandwidth_multiplier: float = 1.0,
    laptop_power_multiplier: float = 1.0,
) -> list[dict[str, Any]]:
    """Compute J/job and modeled application seconds for three placements.

    Two measured repetitions per location give a median but not a robust
    uncertainty estimate. All runs must have matching deterministic output.
    """
    if workload not in WORKLOADS:
        raise ValueError("Unknown workload")
    if network_energy_multiplier < 0 or bandwidth_multiplier <= 0 or laptop_power_multiplier <= 0:
        raise ValueError("Invalid sensitivity multiplier")
    assumptions = profile["assumptions"]
    deadline = float(deadline_s if deadline_s is not None else assumptions["deadline_s"][workload])
    if deadline <= 0:
        raise ValueError("Deadline must be positive")
    selected = [r for r in records if r.get("workload") == workload]
    by_place = {p: [r for r in selected if r["placement"] == p] for p in PLACEMENTS}
    if any(not by_place[p] for p in PLACEMENTS):
        missing = [p for p in PLACEMENTS if not by_place[p]]
        raise ValueError(f"Missing successful run(s) for {workload}: {missing}")
    # Validate functional equivalence before deciding on energy.
    checksums = {r["outputChecksum"] for r in selected}
    inputs = {r["inputBytes"] for r in selected}
    outputs = {r["outputBytes"] for r in selected}
    processed = {r["recordsProcessed"] for r in selected}
    if any(len(items) != 1 for items in (checksums, inputs, outputs, processed)):
        raise ValueError("Workload outputs/data sizes do not match across locations/repetitions")
    in_bytes, out_bytes = int(next(iter(inputs))), int(next(iter(outputs)))

    def network_transfer(nbytes: int, path_name: str) -> tuple[float, float]:
        path = assumptions["paths"][path_name]
        bits = nbytes * 8.0
        # 1 microJ = 1e-6 J; 1 Mbps = 1e6 bits/s.
        energy_j = bits * float(path["energy_uj_per_bit"]) * 1e-6 * network_energy_multiplier
        elapsed_s = (bits / (float(path["bandwidth_mbps"]) * 1e6 * bandwidth_multiplier)
                     + float(path["fixed_latency_ms"]) / 1000.0)
        return energy_j, elapsed_s

    rows = []
    for place in PLACEMENTS:
        times = [float(r["computeTimeMs"]) / 1000 for r in by_place[place]]
        if any(not math.isfinite(t) or t <= 0 for t in times):
            raise ValueError("Invalid/nonpositive measured computation duration")
        t_exec = statistics.median(times)
        effective_power = float(assumptions["power_w"][place]) * (laptop_power_multiplier if place == "laptop" else 1)
        e_comp = effective_power * t_exec
        e_net, t_net = 0.0, 0.0
        if workload == "iot_aggregation":
            if place == "laptop":
                e_net, t_net = network_transfer(out_bytes, "laptop_cloud")
            elif place == "edge":
                a, b = network_transfer(in_bytes, "laptop_edge")
                c, d = network_transfer(out_bytes, "edge_cloud")
                e_net, t_net = a + c, b + d
            else:
                e_net, t_net = network_transfer(in_bytes, "laptop_cloud")
        app_s = t_exec + t_net
        rows.append({
            "placement": place,
            "repetitions": len(times),
            "compute_time_s": t_exec,
            "min_compute_time_s": min(times),
            "max_compute_time_s": max(times),
            "effective_power_w": effective_power,
            "compute_energy_j": e_comp,
            "network_energy_j": e_net,
            "total_energy_j": e_comp + e_net,
            "modeled_transfer_time_s": t_net,
            "modeled_app_time_s": app_s,
            "deadline_s": deadline,
            "feasible": app_s <= deadline + 1e-9,
            "input_bytes": in_bytes,
            "output_bytes": out_bytes,
        })
    return rows


def best_feasible(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return min((r for r in rows if r["feasible"]),
               key=lambda r: (r["total_energy_j"], r["modeled_app_time_s"]),
               default=None)


def export_for_explorer(
    records: Sequence[Mapping[str, Any]], workload: str, output_file: str | Path
) -> Path:
    """Optional bridge to independent HTML Explorer's Import runtimes JSON."""
    if workload not in WORKLOADS:
        raise ValueError("Unknown workload")
    timings = {p: [] for p in PLACEMENTS}
    for record in records:
        if record["workload"] == workload:
            timings[record["placement"]].append(float(record["computeTimeMs"]) / 1000)
    if not all(timings.values()):
        raise ValueError("All three placements required for HTML import")
    path = Path(output_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"workload": workload, "measurements": timings}, indent=2), encoding="utf-8")
    return path
