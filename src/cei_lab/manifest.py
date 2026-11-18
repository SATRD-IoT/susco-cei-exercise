"""Build aeriOS YAML with the exact TOSCA shape supplied by the instructor.

The environment variable encoding uses a list of one-key mappings. Confirm that
this matches the exact HLO-FE version before deploying live student jobs.
"""
from __future__ import annotations

import re

import yaml

_INFRA = re.compile(r"urn:ngsi-ld:InfrastructureElement:[^\s]+\Z")


def make_cpu_job_yaml(
    *,
    node_id: str,
    image: str,
    execution_id: str,
    orion_internal_url: str,
    limit: int,
    workers: int,
) -> str:
    if not _INFRA.fullmatch(node_id):
        raise ValueError("Supply the actual urn:ngsi-ld:InfrastructureElement:... node ID")
    if not image or image.startswith("REPLACE_") or image.endswith(":latest"):
        raise ValueError("Supply a published, version-tagged Docker Hub image")
    if not execution_id.startswith("urn:ngsi-ld:CEIExecution:"):
        raise ValueError("Invalid execution ID")
    if not orion_internal_url.startswith(("http://", "https://")):
        raise ValueError("Set a reachable internal Orion-LD URL")
    if limit < 2 or not 1 <= workers <= 8:
        raise ValueError("Invalid CPU workload arguments")
    envs = {
        "CEI_EXECUTION_ID": execution_id,
        "CEI_ORION_URL": orion_internal_url,
        "CEI_ORION_MODE": "internal",
        "CEI_CPU_LIMIT": str(limit),
        "CEI_CPU_WORKERS": str(workers),
    }
    manifest = {
        "description": "CEI teaching lab CPU one-shot job",
        "node_templates": {
            "man": {
                "artifacts": {
                    "application_image": {
                        "file": image,
                        "isPrivate": False,
                        "repository": "docker_hub",
                        "type": "tosca.artifacts.Deployment.Image.Container.Docker",
                    }
                },
                "interfaces": {
                    "Standard": {
                        "create": {
                            "implementation": "application_image",
                            "inputs": {
                                "cliArgs": [],
                                "envVars": [{k: v} for k, v in envs.items()],
                            },
                        },
                    },
                },
                "isJob": True,
                "requirements": [
                    {"host": {"node_filter": {"capabilities": None,
                          "properties": {"id": [node_id]}}}},
                ],
                "type": "tosca.nodes.Container.Application",
            },
        },
        "serviceOverlay": False,
        "tosca_definitions_version": "tosca_simple_yaml_1_3",
    }
    return yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)


def make_iot_job_yaml(
    *,
    node_id: str,
    image: str,
    execution_id: str,
    orion_internal_url: str,
    records: int,
    window: int,
    seed: int,
    threshold: int,
    work_factor: int,
) -> str:
    """Single-component aeriOS job matching the instructor's proven CPU TOSCA shape."""
    if not 100 <= records <= 2_000_000 or not 1 <= window <= records:
        raise ValueError("Invalid IoT dataset/window")
    if not 0 <= seed <= 2**32 - 1 or not 0 <= threshold <= 10_000:
        raise ValueError("Invalid IoT seed/threshold")
    if not 1 <= work_factor <= 200:
        raise ValueError("Invalid IoT work factor")
    # Reuse the known aeriOS YAML structure, replacing only workload-specific
    # environment variables. The checked CPU args below never reach aeriOS.
    manifest = yaml.safe_load(make_cpu_job_yaml(
        node_id=node_id,
        image=image,
        execution_id=execution_id,
        orion_internal_url=orion_internal_url,
        limit=2,
        workers=1,
    ))
    manifest["description"] = "CEI teaching lab IoT aggregation single-shot job"
    envs = {
        "CEI_EXECUTION_ID": execution_id,
        "CEI_ORION_URL": orion_internal_url,
        "CEI_ORION_MODE": "internal",
        "CEI_IOT_RECORDS": str(records),
        "CEI_IOT_WINDOW": str(window),
        "CEI_IOT_SEED": str(seed),
        "CEI_IOT_THRESHOLD": str(threshold),
        "CEI_IOT_WORK_FACTOR": str(work_factor),
    }
    manifest["node_templates"]["man"]["interfaces"]["Standard"]["create"]["inputs"]["envVars"] = [
        {key: value} for key, value in envs.items()
    ]
    return yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
