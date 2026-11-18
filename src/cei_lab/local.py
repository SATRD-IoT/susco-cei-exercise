"""Laptop Docker runner: Jupyter and its Docker jobs use external Orion + JWT."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from .entities import create_execution_entity, get_prop, new_execution_id, status_patch
from .orion import OrionClient


class LocalRunError(RuntimeError):
    pass


@dataclass
class LocalRun:
    execution_id: str
    job_wall_time_s: float
    entity: dict

    @property
    def result(self) -> dict:
        return get_prop(self.entity, "result") or {}


class LocalDockerRunner:
    def __init__(
        self, orion: OrionClient, *, image: str = "cei-lab-cpu:0.2.0",
        iot_image: str = "cei-lab-iot:0.3.0",
    ) -> None:
        if orion.mode != "external":
            raise ValueError("Notebook must use authenticated external Orion access")
        self.orion = orion
        self.image = image
        self.iot_image = iot_image
        # Both the notebook and Docker workloads are external to the cluster.
        # The authenticated gateway is already configured in `orion`.

    def run_cpu(
        self,
        *,
        group_id: str,
        repetition: int,
        limit: int = 2_000_000,
        workers: int = 1,
        timeout_s: float = 100.0,
    ) -> LocalRun:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if limit < 2 or not 1 <= workers <= 8:
            raise ValueError("invalid CPU workload parameters")

        execution_id = new_execution_id()
        entity = create_execution_entity(
            execution_id=execution_id,
            group_id=group_id,
            workload="cpu_compute",
            placement="laptop",
            repetition=repetition,
            parameters={"limit": limit, "workers": workers},
            workload_version="0.2.0",
        )
        self.orion.create_entity(entity)
        self.orion.patch_entity(execution_id, status_patch("submitted"))

        # The laptop Docker workload is outside Kubernetes: route through
        # the same JWT-protected Orion gateway as Jupyter, not the cluster IP.
        # Pass only environment-variable NAMES as docker CLI arguments: the
        # token is inherited by Docker, not printed in the process arguments.
        env = os.environ.copy()
        env.update(self.orion.external_workload_env(execution_id))
        cmd = [
            "docker", "run", "--rm",
            "-e", "CEI_EXECUTION_ID",
            "-e", "CEI_ORION_URL",
            "-e", "CEI_ORION_MODE",
            "-e", "CEI_ORION_JWT",
            self.image,
            "--limit", str(limit), "--workers", str(workers),
        ]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
        except FileNotFoundError:
            self._mark_failed(execution_id, "Docker CLI not found")
            raise LocalRunError("Docker CLI not found; verify Docker Desktop/WSL integration") from None
        except subprocess.TimeoutExpired:
            self._mark_failed(execution_id, "timed_out", status="timed_out")
            raise LocalRunError(f"CPU container timed out after {timeout_s}s") from None
        wall_s = time.perf_counter() - t0

        try:
            observed = self.orion.get_entity(execution_id)
        except Exception as exc:
            raise LocalRunError(f"Container stopped, but Orion-LD result retrieval failed: {type(exc).__name__}") from None
        status = get_prop(observed, "executionStatus")
        if proc.returncode != 0 or status != "completed":
            if status not in {"failed", "timed_out", "completed"}:
                self._mark_failed(execution_id, f"Docker exited {proc.returncode}; last state {status}")
            raise LocalRunError(f"CPU job unsuccessful (Docker exit={proc.returncode}, Orion status={status})")
        return LocalRun(execution_id=execution_id, job_wall_time_s=wall_s, entity=observed)

    def run_iot(
        self, *, group_id: str, repetition: int,
        records: int = 500_000, window: int = 500, seed: int = 42,
        threshold: int = 9500, work_factor: int = 45,
        timeout_s: float = 150.0,
    ) -> LocalRun:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not 100 <= records <= 2_000_000 or not 1 <= window <= records:
            raise ValueError("invalid IoT records/window")
        if not 0 <= seed <= 2**32 - 1 or not 0 <= threshold <= 10000 or not 1 <= work_factor <= 200:
            raise ValueError("invalid IoT seed/threshold/work factor")
        parameters = {"records": records, "window": window, "seed": seed,
                      "threshold": threshold, "workFactor": work_factor}
        execution_id = new_execution_id()
        entity = create_execution_entity(
            execution_id=execution_id, group_id=group_id,
            workload="iot_aggregation", placement="laptop", repetition=repetition,
            parameters=parameters, workload_version="0.3.0",
        )
        self.orion.create_entity(entity)
        self.orion.patch_entity(execution_id, status_patch("submitted"))
        env = os.environ.copy()
        env.update(self.orion.external_workload_env(execution_id))
        cmd = ["docker", "run", "--rm",
               "-e", "CEI_EXECUTION_ID", "-e", "CEI_ORION_URL", "-e", "CEI_ORION_MODE",
               "-e", "CEI_ORION_JWT",
               self.iot_image,
               "--records", str(records), "--window", str(window),
               "--seed", str(seed), "--threshold", str(threshold),
               "--work-factor", str(work_factor)]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
        except FileNotFoundError:
            self._mark_failed(execution_id, "Docker CLI not found")
            raise LocalRunError("Docker CLI not found; verify Docker Desktop/WSL") from None
        except subprocess.TimeoutExpired:
            self._mark_failed(execution_id, "IoT job timed out", status="timed_out")
            raise LocalRunError("Local IoT job timed out") from None
        wall_s = time.perf_counter() - t0
        try:
            observed = self.orion.get_entity(execution_id)
        except Exception as exc:
            raise LocalRunError(f"IoT stopped but Orion retrieval failed: {type(exc).__name__}") from None
        status = get_prop(observed, "executionStatus")
        if proc.returncode != 0 or status != "completed":
            if status not in {"failed", "timed_out", "completed"}:
                self._mark_failed(execution_id, f"IoT Docker exit {proc.returncode}; status {status}")
            raise LocalRunError(f"IoT job unsuccessful (Docker exit={proc.returncode}, Orion status={status})")
        return LocalRun(execution_id=execution_id, job_wall_time_s=wall_s, entity=observed)

    def _mark_failed(self, execution_id: str, message: str, *, status: str = "failed") -> None:
        # The controller is also authenticated (it runs outside K8s).
        try:
            self.orion.patch_entity(execution_id, status_patch(status, error=message))
        except Exception:
            pass
