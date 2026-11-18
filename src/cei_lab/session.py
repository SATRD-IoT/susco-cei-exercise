"""Session-scoped aeriOS cleanup with verified deallocation before purge.

One aeriOS service is composed of the single component named ``man``.
The service/component *entities* remain in Orion after deallocation, because
redeployment is allowed. The workload's CEIExecution entity is unrelated to
that lifecycle, so its 'completed' status is NOT evidence of deallocation.

DELETE /services/{id} only *starts* asynchronous deallocation. We poll the
corresponding ServiceComponent entity via authenticated Orion GET. Crucially,
'Finished' may already be the state of a completed Kubernetes Job *before*
DELETE. We therefore require a post-DELETE lifecycle signal, rather than
mistaking a stale 'Finished' for deallocation completion.

Only IDs in this durable session registry can be deleted or purged. A failed
verification never triggers purge. A later Clear all retries only unfinished
phases, never resending a successful DELETE or purge.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from .aerios import AeriOSHttpClient
from .orion import OrionClient, OrionError

# These statuses are normalized from the NGSI-LD Relationship object, e.g.
# urn:ngsi-ld:ServiceComponentStatus:Finished.
_DEALLOCATED = frozenset({"removed", "deleted", "deallocated", "terminated"})


def component_id(service_id: str) -> str:
    """aeriOS component URN for the single TOSCA node_template named 'man'."""
    if not service_id.startswith("urn:ngsi-ld:Service:"):
        raise ValueError("Unexpected service ID")
    return f"{service_id}:Component:man"


def _snapshot(orion: OrionClient, service_id: str) -> dict[str, Any]:
    """Read the lifecycle status, including its revision marker if available.

    `modifiedAt` / `observedAt` may be present in either the attribute or
    entity. They help disambiguate Finished-before-DELETE from Finished-after-
    deallocation, even if Removing was too brief to observe.
    """
    entity = orion.get_entity_if_present(component_id(service_id))
    if entity is None:
        return {"present": False, "status": None, "marker": None}
    entity_type = str(entity.get("type", ""))
    if not entity_type.endswith("ServiceComponent"):
        raise ValueError("Orion entity is not a ServiceComponent")
    attr = entity.get("serviceComponentStatus")
    raw: Any = None
    marker: Any = entity.get("modifiedAt")
    if isinstance(attr, dict):
        raw = attr.get("object", attr.get("value"))
        marker = attr.get("observedAt") or attr.get("modifiedAt") or marker
    elif isinstance(attr, str):
        raw = attr
    status = str(raw).rsplit(":", 1)[-1].lower() if raw else None
    return {"present": True, "status": status, "marker": str(marker) if marker else None}


def _deallocation_confirmed(before: dict[str, Any] | None, after: dict[str, Any], *, seen_removing: bool) -> bool:
    if not after["present"]:
        # The component actually disappeared, which is sufficient IF it was
        # observed in the broker before the delete. Not merely an unknown ID.
        return bool(before and before.get("present"))
    status = after.get("status")
    if status in _DEALLOCATED:
        return True
    return status == "finished"

class SessionRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if self._data.get("schema") != 1 or not isinstance(self._data.get("services"), list):
                raise ValueError("Invalid CEI session registry")
            # Upgrade v2/v3 registries in memory, retaining all tracked IDs.
            for row in self._data["services"]:
                row.setdefault("purge", "not_requested")
                row.setdefault("deallocation", "legacy_not_verified" if row["purge"] == "accepted" else "not_checked")
                row.setdefault("pre_delete_component", None)
                row.setdefault("seen_removing", False)
        else:
            self._data = {"schema": 1, "session": secrets.token_hex(6), "services": []}
            self._save()

    @property
    def session_id(self) -> str:
        return self._data["session"]

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._data["services"]]

    def record_submission_attempt(self, *, service_id: str, execution_id: str, placement: str) -> None:
        if any(x["service_id"] == service_id for x in self._data["services"]):
            raise ValueError("Service ID already tracked in this session")
        self._data["services"].append({
            "service_id": service_id,
            "execution_id": execution_id,
            "placement": placement,
            "submission": "attempted",
            "deletion": "not_requested",
            "deallocation": "not_checked",
            "pre_delete_component": None,
            "seen_removing": False,
            "purge": "not_requested",
        })
        self._save()

    def set_submission(self, service_id: str, state: str) -> None:
        self._row(service_id)["submission"] = state
        self._save()

    def clear_all(
        self, client: AeriOSHttpClient, orion: OrionClient, *,
        deallocation_timeout_s: float = 90.0,
        poll_interval_s: float = 1.5,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """DELETE all tracked services, poll Orion for deallocation, then purge.

        All DELETE requests are submitted first; polling runs as a batch so
        total waiting time is bounded by one timeout, not number_of_services
        * timeout. Confirmation is persisted before purge, allowing retries
        after interruptions. The HTTP 2xx itself never authorizes purge.
        """
        if deallocation_timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("deallocation timeout and polling interval must be positive")

        def say(message: str) -> None:
            if on_progress:
                on_progress(message)

        active: list[dict[str, Any]] = []
        reports: dict[str, dict[str, Any]] = {}
        for item in self._data["services"]:
            sid = item["service_id"]
            if item.get("purge") == "accepted":
                continue
            report: dict[str, Any] = {"service_id": sid}
            reports[sid] = report
            if item["deletion"] not in {"accepted", "already_absent"}:
                # Probe before deletion so an already-Finished job does not
                # cause an unsafe immediate purge. A missing / failed probe
                # does not stop us from attempting to deallocate the service.
                try:
                    item["pre_delete_component"] = _snapshot(orion, sid)
                except Exception as exc:
                    item["pre_delete_component"] = None
                    report["before_delete_probe_error"] = type(exc).__name__
                self._save()
                try:
                    say(f"DELETE {sid}")
                    code = client.delete(service_id=sid)
                    item["deletion"] = "already_absent" if code == 404 else "accepted"
                    item["deallocation"] = "awaiting_confirmation"
                    report["delete_http"] = code
                except Exception as exc:
                    item["deletion"] = "failed"
                    report["delete_error"] = type(exc).__name__
                self._save()
            if item["deletion"] in {"accepted", "already_absent"}:
                if item.get("deallocation") != "confirmed":
                    active.append(item)
            report["deletion"] = item["deletion"]

        # Batch-poll all outstanding services (including delete-accepted rows
        # recovered after a kernel restart). Never repeatedly DELETE them.
        deadline = time.monotonic() + deallocation_timeout_s
        if active:
            say(f"Checking deallocation for {len(active)} service(s) in Orion-LD...")
        while active:
            for item in list(active):
                sid = item["service_id"]
                report = reports[sid]
                try:
                    now = _snapshot(orion, sid)
                    report["last_component_status"] = now["status"] if now["present"] else "not_found"
                    if now.get("status") == "removing":
                        item["seen_removing"] = True
                        self._save()
                    confirmed = _deallocation_confirmed(
                        item.get("pre_delete_component"), now,
                        seen_removing=bool(item.get("seen_removing")),
                    )
                    if confirmed or (item["deletion"] == "already_absent" and not now["present"]):
                        item["deallocation"] = "confirmed"
                        report["deallocation"] = "confirmed"
                        self._save()
                        active.remove(item)
                        say(f"Deallocation confirmed: {sid}")
                except (OrionError, ValueError) as exc:
                    # Temporary 5xx/connectivity problems are retried until
                    # the deadline. A schema discrepancy cannot authorize purge.
                    report["poll_error"] = type(exc).__name__
            if not active:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for item in active:
                    item["deallocation"] = "timed_out"
                    reports[item["service_id"]]["deallocation"] = "timed_out"
                    say(f"Deallocation NOT confirmed: {item['service_id']} (purge skipped)")
                self._save()
                break
            time.sleep(min(poll_interval_s, remaining))

        # Purge only services with explicit, persisted confirmation.
        for item in self._data["services"]:
            sid = item["service_id"]
            if sid not in reports:
                continue
            report = reports[sid]
            report["deallocation"] = item.get("deallocation", "not_checked")
            if item.get("deallocation") != "confirmed":
                report["purge"] = "skipped_until_deallocated"
                continue
            if item.get("purge") != "accepted":
                try:
                    say(f"PURGE {sid}")
                    report["purge_http"] = client.purge(service_id=sid)
                    item["purge"] = "accepted"
                except Exception as exc:
                    item["purge"] = "failed"
                    report["purge_error"] = type(exc).__name__
                self._save()
            report["purge"] = item["purge"]

        return list(reports.values())

    def _row(self, service_id: str) -> dict[str, Any]:
        for item in self._data["services"]:
            if item["service_id"] == service_id:
                return item
        raise KeyError("Service is not part of the current notebook session")

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            os.chmod(tmp, 0o600)
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
