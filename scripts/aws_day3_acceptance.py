"""Exercise Day 3 memory/OOM, filesystem, journal, and cleanup behavior on AWS."""

import argparse
import concurrent.futures
import json
import time
import uuid
from pathlib import Path

import httpx

from oncall.aws_ssm import AwsCli, SsmTunnel
from oncall.faults import SCENARIOS, FaultController, SsmOperatorExecutor

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/aws"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    if inventory.get("disposable") is not True:
        raise RuntimeError("Acceptance requires an explicitly disposable target")
    instance = inventory["instance_id"]
    region = inventory["region"]
    aws = AwsCli(region)
    aws.wait_managed_node(instance)
    readiness_commands = [
        "set -e",
        "test -f /var/lib/oncall/ready",
        "systemctl is-active --quiet oncall-target.service",
        "mountpoint -q /var/lib/oncall-lab/data",
        "grep -qx linux-oncall-dedicated-lab /var/lib/oncall-lab/data/.oncall-lab-volume",
        "test -f /sys/fs/cgroup/cgroup.controllers",
        "echo ready",
    ]
    deadline = time.monotonic() + 600
    while True:
        try:
            ready = aws.send_command(instance, readiness_commands, timeout=90)
            if ready.stdout.strip() == "ready":
                break
        except RuntimeError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("Day 3 target did not become ready")
        time.sleep(15)
    aws.enroll(inventory["parameter_name"], LOCAL / "target_token")
    certificate = aws.send_command(instance, ["cat /etc/oncall/target-ca.pem"]).stdout
    (LOCAL / "target_ca.pem").write_text(certificate)
    (LOCAL / "target_ca.pem").chmod(0o600)
    executor = SsmOperatorExecutor(region, instance)
    controller = FaultController(executor, LOCAL / f"fault-{instance}.json", instance)
    if controller.current():
        controller.stop()
    result: dict[str, object] = {"instance_id": instance}

    with SsmTunnel(
        instance,
        inventory["session_document"],
        region,
        LOCAL / "day3-tunnel.log",
    ):
        token = (LOCAL / "target_token").read_text().strip()
        with httpx.Client(
            base_url="https://127.0.0.1:18765",
            verify=str(LOCAL / "target_ca.pem"),
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
            trust_env=False,
            timeout=15,
        ) as target:

            def probe(payload: dict) -> dict:
                response = target.post(
                    "/v1/probe",
                    json=payload,
                    headers={"Idempotency-Key": uuid.uuid4().hex},
                )
                response.raise_for_status()
                return response.json()

            health = target.get("/health").raise_for_status().json()
            if health["protocol"] != 3 or len(health["capabilities"]) != 6:
                raise RuntimeError("Day 3 capability negotiation failed")
            memory = probe({"name": "inspect_memory_pressure"})
            baseline_fs = probe({"name": "inspect_filesystem", "mount_id": "lab"})
            if memory["status"] != "ok" or baseline_fs["status"] != "ok":
                raise RuntimeError("Day 3 baseline probes failed")

            memory_plan = SCENARIOS["memory"].plan(120)
            executor.execute(memory_plan.cleanup)
            executor.execute(
                (
                    "set -e",
                    "systemd-run --unit=oncall-lab-sentinel --slice=oncall-lab.slice "
                    "--property=RuntimeMaxSec=120 /usr/bin/sleep 115",
                    "systemctl set-property oncall-lab.slice MemoryMax=48M",
                )
            )
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    sample = pool.submit(
                        probe,
                        {
                            "name": "inspect_cgroup_memory",
                            "scope_id": "lab",
                            "duration_seconds": 5,
                        },
                    )
                    time.sleep(0.75)
                    trigger = pool.submit(
                        executor.execute,
                        (
                            "set -e",
                            "systemd-run --unit=oncall-lab-workload --slice=oncall-lab.slice "
                            "--property=RuntimeMaxSec=110 /opt/oncall/bin/python "
                            "-m oncall.lab_fault memory --seconds 100 --mebibytes 128",
                        ),
                    )
                    cgroup = sample.result(timeout=15)
                    trigger.result(timeout=30)
                facts = cgroup["facts"]
                if cgroup["status"] != "ok" or facts["oom_kill_delta"] < 1:
                    raise RuntimeError(
                        "No new cgroup OOM kill was captured during the interval: "
                        + json.dumps(facts, sort_keys=True)
                    )
                journal = probe(
                    {
                        "name": "query_service_journal",
                        "unit": "oncall-lab-workload.service",
                        "since_seconds": 120,
                        "limit": 100,
                    }
                )
                if journal["status"] != "ok" or journal["facts"]["entries"] < 1:
                    raise RuntimeError("OOM workload journal was not captured")
                result["cgroup_oom_delta"] = facts["oom_delta"]
                result["cgroup_oom_kill_delta"] = facts["oom_kill_delta"]
                result["oom_journal_entries"] = journal["facts"]["entries"]
            finally:
                executor.execute(memory_plan.cleanup)
                executor.execute(memory_plan.verify_clean)

            controller.start("filesystem", 120)
            try:
                constrained = probe({"name": "inspect_filesystem", "mount_id": "lab"})
                facts = constrained["facts"]
                if constrained["status"] != "ok" or facts["available_bytes"] > 32 * 1024 * 1024:
                    raise RuntimeError("Dedicated lab filesystem did not become constrained")
                journal = probe(
                    {
                        "name": "query_service_journal",
                        "unit": "oncall-lab-workload.service",
                        "since_seconds": 120,
                        "limit": 100,
                    }
                )
                if "controlled_write_errno=28" not in journal["raw"]:
                    raise RuntimeError("Controlled ENOSPC was not present in the workload journal")
                result["filesystem_available_bytes"] = facts["available_bytes"]
                result["filesystem_available_inodes"] = facts["available_inodes"]
                result["filesystem_write_errno"] = 28
            finally:
                controller.stop()
            recovered = probe({"name": "inspect_filesystem", "mount_id": "lab"})
            if (
                recovered["facts"]["available_bytes"]
                <= baseline_fs["facts"]["available_bytes"] // 2
            ):
                raise RuntimeError("Filesystem cleanup did not recover capacity")
            result["healthy_memory_status"] = memory["status"]
            result["cleanup"] = "passed"
            result["gzip"] = "passed"

    output = LOCAL / f"day3-acceptance-{instance}.json"
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
