"""AeriOS HLO-FE transport using the supplied service URL and YAML shape."""
from __future__ import annotations

import os
import re
from urllib.parse import quote

import requests

_SERVICE = re.compile(r"urn:ngsi-ld:Service:[0-9a-fA-F]{12}\Z")


class AeriOSError(RuntimeError):
    pass


class AeriOSHttpClient:
    def __init__(self, *, base_url: str, jwt: str, timeout_s: float = 20.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("aeriOS base URL must use http(s)")
        if not (jwt or "").strip():
            raise ValueError("aeriOS Bearer token required")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {jwt.strip()}"})

    @classmethod
    def from_env(cls) -> "AeriOSHttpClient":
        return cls(
            base_url=os.environ.get("CEI_AERIOS_URL", ""),
            jwt=os.environ.get("CEI_AERIOS_JWT", ""),
        )

    def service_url(self, service_id: str) -> str:
        if not _SERVICE.fullmatch(service_id):
            raise ValueError("aeriOS service ID must have exactly 12 hexadecimal digits")
        return f"{self.base_url}/services/{quote(service_id, safe=':')}"

    def post_yaml(self, *, service_id: str, yaml_body: str) -> int:
        if not yaml_body.strip():
            raise ValueError("aeriOS YAML body is empty")
        try:
            response = self.session.post(
                self.service_url(service_id),
                data=yaml_body.encode("utf-8"),
                headers={"Content-Type": "text/yaml", "Accept": "application/json"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise AeriOSError(f"aeriOS POST did not complete ({type(exc).__name__})") from None
        if not response.ok:
            raise AeriOSError(f"aeriOS POST rejected: HTTP {response.status_code}")
        return response.status_code  # 2xx acknowledges submission; NOT job completion

    def delete(self, *, service_id: str) -> int:
        try:
            response = self.session.delete(self.service_url(service_id), timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise AeriOSError(f"aeriOS DELETE did not complete ({type(exc).__name__})") from None
        if response.status_code == 404:
            return 404  # already absent; useful for idempotent session cleanup
        if not response.ok:
            raise AeriOSError(f"aeriOS DELETE rejected: HTTP {response.status_code}")
        return response.status_code  # accepted; not proof of physical removal

    def purge(self, *, service_id: str) -> int:
        """Purge one known service and its aeriOS component entity records.

        Assumed instructor-specific route: DELETE /hlo_fe/services/{id}/purge.
        This is not a standard NGSI-LD operation; live confirmation of the
        route and asynchronous lifecycle is required before classroom use.
        """
        url = self.service_url(service_id) + "/purge"
        try:
            response = self.session.delete(url, timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise AeriOSError(f"aeriOS PURGE did not complete ({type(exc).__name__})") from None
        if response.status_code == 404:
            # Custom route is not yet live-verified: a 404 could mean a missing
            # endpoint, not just an already-purged service. Never mark clean.
            raise AeriOSError("aeriOS PURGE returned HTTP 404 (route or service not found)")
        if not response.ok:
            raise AeriOSError(f"aeriOS PURGE rejected: HTTP {response.status_code}")
        return response.status_code
