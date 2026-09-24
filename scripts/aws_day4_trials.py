"""Run repeated Day 4 substrate trials without claiming model diagnostic quality."""

import argparse
import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from oncall.aws_ssm import AwsCli, SsmTunnel
from oncall.evaluation import source_revision
from oncall.faults import FaultController, SsmOperatorExecutor

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/aws"


def now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--release-sha256", required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    if inventory.get("disposable") is not True:
        raise RuntimeError("Day 4 trials require an explicitly disposable target")
    instance, region = inventory["instance_id"], inventory["region"]
    aws = AwsCli(region)
    aws.wait_managed_node(instance)
    deadline = time.monotonic() + 600
    while True:
        try:
            readiness = aws.send_command(
                instance,
                [
                    "set -e",
                    "test -f /var/lib/oncall/ready",
                    "systemctl is-active --quiet oncall-target.service",
                    "mountpoint -q /var/lib/oncall-lab/data",
                    "echo ready",
                ],
                timeout=90,
            )
            if readiness.stdout.strip() == "ready":
                break
        except RuntimeError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("Day 4 target did not become ready")
        time.sleep(15)
    aws.enroll(inventory["parameter_name"], LOCAL / "target_token")
    certificate = aws.send_command(instance, ["cat /etc/oncall/target-ca.pem"]).stdout
    (LOCAL / "target_ca.pem").write_text(certificate)
    (LOCAL / "target_ca.pem").chmod(0o600)
    executor = SsmOperatorExecutor(region, instance)
    controller = FaultController(executor, LOCAL / f"fault-{instance}.json", instance)
    if controller.current():
        controller.stop()
    trials: list[dict[str, Any]] = []
    overhead_samples: list[float] = []

    with SsmTunnel(instance, inventory["session_document"], region, LOCAL / "day4-tunnel.log"):
        token = (LOCAL / "target_token").read_text().strip()
        with httpx.Client(
            base_url="https://127.0.0.1:18765",
            verify=str(LOCAL / "target_ca.pem"),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=20,
        ) as target:

            def probe(payload: dict[str, Any]) -> dict[str, Any]:
                response = target.post(
                    "/v1/probe",
                    json=payload,
                    headers={"Idempotency-Key": uuid.uuid4().hex},
                )
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise RuntimeError("Target returned a non-object observation")
                return value

            health = target.get("/health").raise_for_status().json()
            for repetition in range(1, args.repetitions + 1):
                started = now()
                cpu = probe({"name": "sample_cpu_pressure", "duration_seconds": 2})
                memory = probe({"name": "inspect_memory_pressure"})
                cgroup = probe(
                    {"name": "inspect_cgroup_memory", "scope_id": "self", "duration_seconds": 1}
                )
                filesystem = probe({"name": "inspect_filesystem", "mount_id": "lab"})
                if cpu.get("facts"):
                    overhead_samples.append(cpu["facts"]["probe_cgroup_cpu_cores_used"] or 0.0)
                trials.append(
                    {
                        "scenario": "healthy",
                        "repetition": repetition,
                        "started_at": started,
                        "completed_at": now(),
                        "setup_ready": True,
                        "cleanup": "not_required",
                        "passed": all(
                            observation["status"] == "ok"
                            for observation in (cpu, memory, cgroup, filesystem)
                        )
                        and cgroup["facts"]["oom_kill_delta"] == 0
                        and filesystem["facts"]["available_bytes"] > 32 * 1024 * 1024,
                        "observations": [cpu, memory, cgroup, filesystem],
                    }
                )

                started = now()
                controller.start("cpu", 45)
                try:
                    cpu = probe({"name": "sample_cpu_pressure", "duration_seconds": 2})
                    processes = probe(
                        {"name": "rank_processes", "duration_seconds": 2, "limit": 10}
                    )
                    dominant = any(
                        item["name"] == "python" and item["cpu_pct_one_core"] > 25
                        for item in processes["facts"]["processes"]
                    )
                    passed = cpu["facts"]["host_busy_pct"] > 70 and dominant
                finally:
                    controller.stop()
                trials.append(
                    {
                        "scenario": "cpu",
                        "repetition": repetition,
                        "started_at": started,
                        "completed_at": now(),
                        "setup_ready": True,
                        "cleanup": "passed",
                        "passed": passed,
                        "observations": [cpu, processes],
                    }
                )

                started = now()
                controller.start("memory", 60)
                try:
                    baseline_kills = int(
                        executor.execute(
                            ("cat /var/lib/oncall-lab/data/.oncall-oom-before",)
                        ).strip()
                    )
                    cgroup = probe(
                        {
                            "name": "inspect_cgroup_memory",
                            "scope_id": "lab",
                            "duration_seconds": 1,
                        }
                    )
                    journal = probe(
                        {
                            "name": "query_service_journal",
                            "unit": "oncall-lab-workload.service",
                            "since_seconds": 120,
                            "limit": 100,
                        }
                    )
                    current_kills = cgroup["facts"]["events_after"]["oom_kill"] or 0
                    passed = (
                        current_kills > baseline_kills
                        and cgroup["facts"]["max_bytes"] == 48 * 1024 * 1024
                        and journal["status"] == "ok"
                        and journal["facts"]["entries"] > 0
                    )
                finally:
                    controller.stop()
                trials.append(
                    {
                        "scenario": "memory",
                        "repetition": repetition,
                        "started_at": started,
                        "completed_at": now(),
                        "setup_ready": True,
                        "cleanup": "passed",
                        "passed": passed,
                        "baseline_oom_kill": baseline_kills,
                        "observations": [cgroup, journal],
                    }
                )

                baseline = probe({"name": "inspect_filesystem", "mount_id": "lab"})
                started = now()
                controller.start("filesystem", 90)
                try:
                    filesystem = probe({"name": "inspect_filesystem", "mount_id": "lab"})
                    journal = probe(
                        {
                            "name": "query_service_journal",
                            "unit": "oncall-lab-workload.service",
                            "since_seconds": 120,
                            "limit": 100,
                        }
                    )
                    constrained = filesystem["facts"]["available_bytes"] <= 32 * 1024 * 1024
                    failed_write = "controlled_write_errno=28" in journal["raw"]
                finally:
                    controller.stop()
                recovered = probe({"name": "inspect_filesystem", "mount_id": "lab"})
                passed = (
                    constrained
                    and failed_write
                    and recovered["facts"]["available_bytes"]
                    > baseline["facts"]["available_bytes"] // 2
                )
                trials.append(
                    {
                        "scenario": "filesystem",
                        "repetition": repetition,
                        "started_at": started,
                        "completed_at": now(),
                        "setup_ready": True,
                        "cleanup": "passed",
                        "passed": passed,
                        "observations": [filesystem, journal, recovered],
                    }
                )

    # The tunnel is closed here. Keep failed connection attempts in the denominator.
    token = (LOCAL / "target_token").read_text().strip()
    for repetition in range(1, args.repetitions + 1):
        started = now()
        unavailable = False
        error_type = None
        try:
            with httpx.Client(
                base_url="https://127.0.0.1:18765",
                verify=str(LOCAL / "target_ca.pem"),
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                timeout=1,
            ) as closed:
                closed.get("/health").raise_for_status()
        except httpx.HTTPError as error:
            unavailable, error_type = True, type(error).__name__
        trials.append(
            {
                "scenario": "unavailable",
                "repetition": repetition,
                "started_at": started,
                "completed_at": now(),
                "setup_ready": True,
                "cleanup": "not_required",
                "passed": unavailable,
                "error_type": error_type,
                "observations": [],
            }
        )

    counts = {
        scenario: {
            "passed": sum(trial["passed"] for trial in trials if trial["scenario"] == scenario),
            "total": sum(trial["scenario"] == scenario for trial in trials),
        }
        for scenario in ("cpu", "memory", "filesystem", "healthy", "unavailable")
    }
    result = {
        "schema_version": 1,
        "kind": "substrate_reliability_not_model_quality",
        "instance_id": instance,
        "target_health": health,
        "source_revision": source_revision(ROOT),
        "release_sha256": args.release_sha256,
        "created_at": now(),
        "counts": counts,
        "probe_overhead": {
            "metric": "probe cgroup CPU cores used during healthy CPU sampling",
            "samples": overhead_samples,
            "median": statistics.median(overhead_samples),
            "range": [min(overhead_samples), max(overhead_samples)],
        },
        "trials": trials,
    }
    output = LOCAL / f"day4-substrate-{instance}.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(output), "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
