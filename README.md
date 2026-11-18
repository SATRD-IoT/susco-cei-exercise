# Sustainable Computing in the Cloud-Edge-IoT Continuum

**Student laboratory v4.1 / 90 minutes / Jupyter + Docker + Eclipse aeriOS + Orion-LD**

The lab runs **two workloads** (CPU computation and IoT aggregation) twice in each of **three locations**: your laptop, an edge VM and a cloud VM. You will compare real computation times and explore **modeled**, not physically measured, operational energy and network delays. No assignments or uploads are required.

## 1. Before class

Your instructor will tell you the exact URLs, edge/cloud node IDs, Docker Hub image tags, and a *temporary* aeriOS/Orion access token. Do not include these tokens in screenshots, chat messages, a shared ZIP, or the notebook itself.

Install and check:

- Python **3.10+**, with `python3 -m venv` available.
- Docker Desktop and a running Docker engine. On Windows, use **WSL 2** with Docker Desktop's WSL integration enabled; open a WSL terminal. On Linux/macOS, use your normal terminal with Docker installed.
- Internet/network access to the instructor-provided **external** aeriOS and Orion-LD gateways, including outbound access from laptop Docker containers to the **external Orion** gateway. Only remote aeriOS nodes need connectivity to the direct internal Orion address.

Extract **the entire ZIP**, not only its notebook. Its Python library (`src/cei_lab`), Dockerfiles and modeled scenario YAML are needed. **If updating an earlier lab version**, restart the Jupyter kernel after installing this release, and leave `BUILD_LOCAL_IMAGES = True` so the laptop images are rebuilt with the correct gateway configuration.

## 2. First-time installation

Open a terminal **inside the extracted `cei_sustainable_computing_lab/` directory**:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[notebook]'
```

For Windows, execute these commands in **WSL 2**, not Windows PowerShell. Do not move the individual notebook outside the project folder.

## 3. Configure the classroom endpoints

From the same project directory:

```bash
cp .env.example .env
nano .env
```

Replace **every** `REPLACE_...` placeholder with values provided by your instructor. In particular, give your group a distinct `CEI_GROUP_ID` so that the cleanup registry is isolated to your group.

Activate these settings **in the same terminal** that will start Jupyter:

```bash
chmod 600 .env
set -a
source .env
set +a
```

The `.env` file stays only on your laptop; it is excluded from Docker image build contexts. It is not included in this ZIP. **Laptop Docker workloads inherit the external Orion JWT at run time** (not at image build time) so they can PATCH their results. Do not share the JWT or container inspection output; use short-lived instructor-supplied tokens.

## 4. Start the lab

Keep Docker Desktop running, then launch:

```bash
python -m jupyter lab notebooks/sustainable_cei_lab.ipynb
```

Select the Python kernel from the `.venv` used above. **Execute cells from top to bottom.** The notebook automatically builds the two *local* workload images. The *remote* workload images are already published by your instructor.

The first checks should display the Docker engine version and show that the environment configuration is present. The notebook never prints token values. If Jupyter cannot import `cei_lab`, reinstall the editable package in the exact Python environment used by the active notebook kernel.

## 5. During the exercise

1. Predict which placement will be fastest / least energy-intensive for each workload.
2. Run the **CPU workload** with default `CPU_LIMIT = 2_000_000`, `CPU_WORKERS = 1` and **two repetitions** in laptop, edge and cloud. The expected mathematical output is **148,933 primes**. A 12-vCPU cloud does not necessarily outperform a 4-vCPU edge for one worker.
3. Run the deterministic **IoT filtering and aggregation** workload with the default settings in the notebook. It recreates the same dataset in every location. The communication volumes are **logical/model inputs**, not actual dataset uploads during job execution.
4. Inspect the notebook's `scenarios/baseline.yaml` tables, modeled joules and times, comparison charts, and interactive deadline/network sensitivity controls.
5. Discuss your preferred placement and **why a different deadline or network might reverse it**.

`computeTimeMs` is measured inside a container. `jobWallTimeS` additionally includes job startup, orchestration, result reporting and, for remote runs, Orion polling. **Neither is a direct electrical-energy measurement.** Modeled application time uses measured compute duration plus estimated application-data transfer time, not job wall time.

## 6. Finish: Clear all

At the end, scroll to **Section 8**, type `CLEAR` and press **Clear all tracked services + purge**. Only the service IDs in your group's persisted `cei_lab_state/` registry are eligible.

Cleanup first sends an aeriOS **DELETE** request, then **polls the corresponding ServiceComponent entity in Orion-LD** until deallocation is confirmed, and **only then** sends the service-scoped **purge** request. A completed one-shot job may show `Finished` *before* deletion; that stale status alone does not authorize purge. If the status check times out, the notebook **does not purge** that service. The operation may be retried without repeating a successful DELETE. Execution-result entities (`CEIExecution`) stay in Orion-LD.

Do not close Jupyter while cleanup is running. If some rows remain pending, tell the instructor; do not manually delete or purge any unrelated services.

## 7. Quick troubleshooting

| Symptom | What to check |
| --- | --- |
| `No module named 'cei_lab'` | Correct `.venv` kernel; reinstall with `python -m pip install -e '.[notebook]'` |
| `docker` unavailable | Docker Desktop started, WSL 2 integration enabled, `docker info` succeeds |
| Missing configuration error | `.env` values filled and sourced in the Jupyter-starting terminal |
| JWT / HTTP 401 or 403 | Check both Jupyter and laptop-container access to the external Orion gateway; ask instructor for valid, limited-time credentials |
| Remote workload timeout | Check instructor's remote image, network reachability and node availability |
| Purge skipped after timeout | aeriOS deletion status not confirmed; tell instructor; retry cleanup once the backend is healthy |

For advanced experiments, the separate **CEI Sustainability Explorer** HTML page and its PDF student guide may be shared by the instructor as independent optional resources.
