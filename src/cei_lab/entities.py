"""Execution record schema; JSON without an explicit @context."""

from __future__ import annotations

import re
import secrets
from typing import Any, Mapping


STATUSES = frozenset({"pending", "submitted", "running", "completed", "failed", "timed_out"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out"})
WORKLOADS = frozenset({"cpu_compute", "iot_aggregation"})
PLACEMENTS = frozenset({"laptop", "edge", "cloud"})


def prop(value: Any) -> dict[str, Any]:
    return {"type": "Property", "value": value}


def new_execution_id() -> str:
    """Unique one-run identifier, kept independent of the aeriOS service ID."""
    return f"urn:ngsi-ld:CEIExecution:{secrets.token_hex(12)}"


def new_service_id() -> str:
    """12 hex digits as required by this exercise's aeriOS service IDs."""
    return f"urn:ngsi-ld:Service:{secrets.token_hex(6)}"


def create_execution_entity(
    *,
    execution_id: str,
    group_id: str,
    workload: str,
    placement: str,
    repetition: int,
    parameters: Mapping[str, Any],
    workload_version: str = "0.1.0",
    aerios_service_id: str | None = None,
) -> dict[str, Any]:
    if not execution_id.startswith("urn:ngsi-ld:CEIExecution:"):
        raise ValueError("execution_id must be a CEIExecution URN")
    if workload not in WORKLOADS:
        raise ValueError(f"unsupported workload: {workload}")
    if placement not in PLACEMENTS:
        raise ValueError(f"unsupported placement: {placement}")
    if repetition < 1:
        raise ValueError("repetition must be >= 1")
    if not group_id.strip():
        raise ValueError("group_id must be non-empty")
    if placement == "laptop" and aerios_service_id is not None:
        raise ValueError("local laptop runs must not have an aeriOS service ID")
    if placement in {"edge", "cloud"} and not aerios_service_id:
        raise ValueError("remote aeriOS runs require an explicit service ID")
    if aerios_service_id and not re.fullmatch(r"urn:ngsi-ld:Service:[0-9a-fA-F]{12}", aerios_service_id):
        raise ValueError("aeriOS service ID must contain 12 hexadecimal digits")

    entity: dict[str, Any] = {
        "id": execution_id,
        "type": "CEIExecution",
        "groupId": prop(group_id),
        "workload": prop(workload),
        "workloadVersion": prop(workload_version),
        "placement": prop(placement),
        "repetition": prop(repetition),
        "executionStatus": prop("pending"),
        "parameters": prop(dict(parameters)),
        "result": prop({}),
        "errorMessage": prop(""),
    }
    if aerios_service_id:
        entity["aeriosServiceId"] = prop(aerios_service_id)
    return entity


def status_patch(status: str, *, result: Mapping[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    patch = {"executionStatus": prop(status)}
    if result is not None:
        patch["result"] = prop(dict(result))
    if error is not None:
        patch["errorMessage"] = prop(str(error)[:240])
    return patch


def get_prop(entity: Mapping[str, Any], name: str) -> Any:
    """Read a regular Property (also tolerates Orion-LD keyValues response)."""
    attr = entity.get(name)
    return attr.get("value") if isinstance(attr, dict) and attr.get("type") == "Property" else attr
