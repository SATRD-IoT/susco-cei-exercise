"""Orion-LD client for CEI lab.

External gateway: <external-url>/entities (the gateway hides /ngsi-ld/v1).
Internal direct endpoint: <internal-url>/ngsi-ld/v1/entities.
Entity updates use user-specific PATCH /entities/{id}, NOT /attrs.
No explicit JSON-LD @context in bodies.
"""
from __future__ import annotations

import os
import time
from typing import Any, Mapping
from urllib.parse import quote

import requests

from .entities import TERMINAL_STATUSES, get_prop


class OrionError(RuntimeError):
    """Non-sensitive summary of broker failure with optional HTTP code."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OrionClient:
    def __init__(
        self,
        base_url: str,
        *,
        mode: str = "external",
        jwt: str | None = None,
        timeout_s: float = 12.0,
    ) -> None:
        if mode not in {"external", "internal"}:
            raise ValueError("Orion mode must be external or internal")
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ValueError("Orion base_url must be an http(s) URL")
        if mode == "external" and not (jwt or "").strip():
            raise ValueError("JWT Bearer token required for external Orion-LD")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.timeout_s = float(timeout_s)
        self._jwt = jwt.strip() if jwt else None
        if self.base_url.endswith("/entities"):
            self.entities_url = self.base_url
        elif self.base_url.endswith("/ngsi-ld/v1"):
            self.entities_url = f"{self.base_url}/entities"
        elif mode == "external":
            self.entities_url = f"{self.base_url}/entities"
        else:
            self.entities_url = f"{self.base_url}/ngsi-ld/v1/entities"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if mode == "external":
            self.session.headers["Authorization"] = f"Bearer {self._jwt}"

    @classmethod
    def from_env(cls) -> "OrionClient":
        mode = os.getenv("CEI_ORION_MODE", "external").strip().lower()
        url = os.getenv("CEI_ORION_URL", "").strip()
        if not url:
            url = os.getenv("CEI_ORION_EXTERNAL_URL" if mode == "external" else "CEI_ORION_INTERNAL_URL", "")
        jwt = os.getenv("CEI_ORION_JWT") if mode == "external" else None
        return cls(url, mode=mode, jwt=jwt)

    def external_workload_env(self, execution_id: str) -> dict[str, str]:
        """JWT-authenticated gateway settings for a trusted laptop Docker job.

        Use env-var names in ``docker run -e KEY`` rather than embedding the
        JWT in command-line arguments or in the Docker image. Remote aeriOS
        jobs use their separate, internal Orion endpoint and no JWT.
        """
        if self.mode != "external" or not self._jwt:
            raise ValueError("Laptop containers require external Orion gateway access with JWT")
        return {
            "CEI_EXECUTION_ID": execution_id,
            "CEI_ORION_URL": self.base_url,
            "CEI_ORION_MODE": "external",
            "CEI_ORION_JWT": self._jwt,
        }

    def _entity_url(self, entity_id: str) -> str:
        return f"{self.entities_url}/{quote(entity_id, safe=':')}"

    def _request(self, method: str, url: str, *, body: Mapping[str, Any] | None = None) -> Any:
        try:
            response = self.session.request(
                method, url, json=body if body is not None else None,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise OrionError(f"Orion-LD {method} request failed ({type(exc).__name__})") from None
        if not response.ok:
            raise OrionError(f"Orion-LD {method} failed: HTTP {response.status_code}", status_code=response.status_code)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def create_entity(self, entity: Mapping[str, Any]) -> None:
        if "@context" in entity:
            raise ValueError("CEIExecution entities must not include @context")
        self._request("POST", self.entities_url, body=entity)

    def get_entity(self, execution_id: str) -> dict[str, Any]:
        entity = self._request("GET", self._entity_url(execution_id))
        if not isinstance(entity, dict):
            raise OrionError("Orion-LD GET returned no JSON entity")
        return entity

    def get_entity_if_present(self, entity_id: str) -> dict[str, Any] | None:
        """GET a known entity, returning None only for an explicit HTTP 404.

        Transient HTTP/network errors are propagated, never mistaken for
        deletion or for a successful deallocation.
        """
        try:
            return self.get_entity(entity_id)
        except OrionError as exc:
            if exc.status_code == 404:
                return None
            raise

    def patch_entity(self, execution_id: str, attrs: Mapping[str, Any]) -> None:
        if "@context" in attrs:
            raise ValueError("CEIExecution PATCH must not include @context")
        self._request("PATCH", self._entity_url(execution_id), body=attrs)

    def wait_for_terminal(
        self, execution_id: str, *, timeout_s: float = 180.0,
        interval_s: float = 1.5,
    ) -> dict[str, Any]:
        if timeout_s <= 0 or interval_s <= 0:
            raise ValueError("poll timeout and interval must be positive")
        deadline = time.monotonic() + timeout_s
        while True:
            entity = self.get_entity(execution_id)
            if get_prop(entity, "executionStatus") in TERMINAL_STATUSES:
                return entity
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Orion-LD execution result was not reported before the deadline")
            time.sleep(min(interval_s, remaining))
