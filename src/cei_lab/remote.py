"""Remote CPU job runner using aeriOS POST and Orion-LD result completion.

Only assumes aeriOS 2xx means submission/deletion request accepted. No aeriOS
status API is invented. Individual jobs report measurements directly to Orion.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .aerios import AeriOSHttpClient
from .entities import (
    create_execution_entity, get_prop, new_execution_id, new_service_id, status_patch,
)
from .manifest import make_cpu_job_yaml, make_iot_job_yaml
from .orion import OrionClient
from .session import SessionRegistry


class RemoteRunError(RuntimeError):
    pass


@dataclass
class RemoteRun:
    execution_id: str
    service_id: str
    placement: str
    observed_wall_time_s: float
    entity: dict

    @property
    def result(self) -> dict:
        return get_prop(self.entity, "result") or {}


class RemoteAeriOSRunner:
    def __init__(
        self,
        *,
        orion_external: OrionClient,
        aerios: AeriOSHttpClient,
        session: SessionRegistry,
        orion_internal_url: str,
        edge_node_id: str,
        cloud_node_id: str,
        cpu_image: str,
        iot_image: str | None = None,
    ) -> None:
        if orion_external.mode != "external":
            raise ValueError("Jupyter must use the authenticated external Orion-LD endpoint")
        if not edge_node_id or not cloud_node_id or edge_node_id == cloud_node_id:
            raise ValueError("Distinct edge and cloud node IDs are required")
        self.orion = orion_external
        self.aerios = aerios
        self.session = session
        self.orion_internal_url = orion_internal_url
        self.node_ids = {"edge": edge_node_id, "cloud": cloud_node_id}
        self.cpu_image = cpu_image
        self.iot_image = iot_image

    def preview_yaml(self, *, placement: str, limit: int = 2_000_000, workers: int = 1) -> str:
        if placement not in self.node_ids:
            raise ValueError("Remote placement must be edge or cloud")
        return make_cpu_job_yaml(
            node_id=self.node_ids[placement], image=self.cpu_image,
            execution_id="urn:ngsi-ld:CEIExecution:000000000000000000000000",
            orion_internal_url=self.orion_internal_url,
            limit=limit, workers=workers,
        )

    def run_cpu(
        self, *, group_id: str, placement: str, repetition: int,
        limit: int = 2_000_000, workers: int = 1,
        timeout_s: float = 180.0, poll_interval_s: float = 1.5,
    ) -> RemoteRun:
        if placement not in self.node_ids:
            raise ValueError("Remote placement must be edge or cloud")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        execution_id, service_id = new_execution_id(), new_service_id()
        yaml_body = make_cpu_job_yaml(
            node_id=self.node_ids[placement], image=self.cpu_image,
            execution_id=execution_id, orion_internal_url=self.orion_internal_url,
            limit=limit, workers=workers,
        )
        entity = create_execution_entity(
            execution_id=execution_id, group_id=group_id,
            workload="cpu_compute", placement=placement, repetition=repetition,
            parameters={"limit": limit, "workers": workers}, aerios_service_id=service_id,
            workload_version="0.2.0",
        )
        self.orion.create_entity(entity)
        self.orion.patch_entity(execution_id, status_patch("submitted"))
        # Persist BEFORE POST: network failures can leave an unknown remote state.
        self.session.record_submission_attempt(
            service_id=service_id, execution_id=execution_id, placement=placement,
        )
        t0 = time.perf_counter()
        try:
            self.aerios.post_yaml(service_id=service_id, yaml_body=yaml_body)
            self.session.set_submission(service_id, "accepted")
        except Exception as exc:
            self.session.set_submission(service_id, "unknown")
            self._mark_failure_if_pending(execution_id, f"aeriOS submission: {type(exc).__name__}")
            raise RemoteRunError(
                f"aeriOS submission failed for service {service_id}; cleanup registry retained the ID"
            ) from None
        try:
            observed = self.orion.wait_for_terminal(
                execution_id, timeout_s=timeout_s, interval_s=poll_interval_s,
            )
        except TimeoutError:
            self._mark_failure_if_pending(execution_id, "Orion result timeout", status="timed_out")
            raise RemoteRunError(
                f"Result for service {service_id} timed out; it remains tracked for Clear all"
            ) from None
        except Exception as exc:
            raise RemoteRunError(
                f"Orion result polling failed for {service_id}: {type(exc).__name__}"
            ) from None
        if get_prop(observed, "executionStatus") != "completed":
            raise RemoteRunError(
                f"Remote service {service_id} finished with Orion status "
                f"{get_prop(observed, 'executionStatus')}"
            )
        return RemoteRun(
            execution_id=execution_id, service_id=service_id, placement=placement,
            observed_wall_time_s=time.perf_counter() - t0, entity=observed,
        )

    def preview_iot_yaml(
        self, *, placement: str, records: int = 500_000, window: int = 500,
        seed: int = 42, threshold: int = 9500, work_factor: int = 45,
    ) -> str:
        if placement not in self.node_ids:
            raise ValueError("Remote placement must be edge or cloud")
        if not self.iot_image:
            raise ValueError("Remote IoT image not configured")
        return make_iot_job_yaml(
            node_id=self.node_ids[placement], image=self.iot_image,
            execution_id="urn:ngsi-ld:CEIExecution:000000000000000000000000",
            orion_internal_url=self.orion_internal_url,
            records=records, window=window, seed=seed,
            threshold=threshold, work_factor=work_factor,
        )

    def run_iot(
        self, *, group_id: str, placement: str, repetition: int,
        records: int = 500_000, window: int = 500, seed: int = 42,
        threshold: int = 9500, work_factor: int = 45,
        timeout_s: float = 180.0, poll_interval_s: float = 1.5,
    ) -> RemoteRun:
        if placement not in self.node_ids:
            raise ValueError("Remote placement must be edge or cloud")
        if not self.iot_image:
            raise ValueError("Remote IoT image not configured")
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("timeout/interval must be positive")
        execution_id, service_id = new_execution_id(), new_service_id()
        yaml_body = make_iot_job_yaml(
            node_id=self.node_ids[placement], image=self.iot_image,
            execution_id=execution_id, orion_internal_url=self.orion_internal_url,
            records=records, window=window, seed=seed,
            threshold=threshold, work_factor=work_factor,
        )
        params = {"records": records, "window": window, "seed": seed,
                  "threshold": threshold, "workFactor": work_factor}
        entity = create_execution_entity(
            execution_id=execution_id, group_id=group_id,
            workload="iot_aggregation", placement=placement,
            repetition=repetition, parameters=params,
            aerios_service_id=service_id, workload_version="0.3.0",
        )
        self.orion.create_entity(entity)
        self.orion.patch_entity(execution_id, status_patch("submitted"))
        self.session.record_submission_attempt(
            service_id=service_id, execution_id=execution_id, placement=placement,
        )
        t0 = time.perf_counter()
        try:
            self.aerios.post_yaml(service_id=service_id, yaml_body=yaml_body)
            self.session.set_submission(service_id, "accepted")
        except Exception as exc:
            self.session.set_submission(service_id, "unknown")
            self._mark_failure_if_pending(execution_id, f"aeriOS submission: {type(exc).__name__}")
            raise RemoteRunError(
                f"aeriOS IoT submission failed for {service_id}; tracked for Clear all"
            ) from None
        try:
            observed = self.orion.wait_for_terminal(
                execution_id, timeout_s=timeout_s, interval_s=poll_interval_s,
            )
        except TimeoutError:
            self._mark_failure_if_pending(execution_id, "Orion result timeout", status="timed_out")
            raise RemoteRunError(f"IoT result timeout for {service_id}; tracked for Clear all") from None
        except Exception as exc:
            raise RemoteRunError(f"Orion IoT polling failed for {service_id}: {type(exc).__name__}") from None
        if get_prop(observed, "executionStatus") != "completed":
            raise RemoteRunError(
                f"Remote IoT service {service_id} reported "
                f"{get_prop(observed, 'executionStatus')}"
            )
        return RemoteRun(
            execution_id=execution_id, service_id=service_id, placement=placement,
            observed_wall_time_s=time.perf_counter() - t0, entity=observed,
        )

    def _mark_failure_if_pending(self, entity_id: str, message: str, *, status: str = "failed") -> None:
        try:
            observed = self.orion.get_entity(entity_id)
            if get_prop(observed, "executionStatus") not in {"completed", "failed", "timed_out"}:
                self.orion.patch_entity(entity_id, status_patch(status, error=message))
        except Exception:
            pass
