"""Exercise the remote Day 2 boundary without displaying credentials."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

from oncall.aws_ssm import AwsCli, SsmTunnel

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/aws"


def run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=240,
    )


def admin_client() -> httpx.Client:
    token = (ROOT / ".local/lab/secrets/admin_token").read_text().strip()
    return httpx.Client(
        base_url="http://127.0.0.1:8787",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=15,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    aws = AwsCli(inventory["region"])
    instance = inventory["instance_id"]
    aws.wait_managed_node(instance)
    readiness_commands = [
        "set -e",
        "test -f /var/lib/oncall/ready",
        "systemctl is-active --quiet oncall-target.service",
        "mountpoint -q /var/lib/oncall-lab/data",
        "test $(stat -c %U /var/lib/oncall-lab/data) = oncall",
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
            raise TimeoutError("Remote application did not become ready")
        time.sleep(15)
    aws.enroll(inventory["parameter_name"], LOCAL / "target_token")
    certificate = aws.send_command(instance, ["cat /etc/oncall/target-ca.pem"]).stdout
    if "BEGIN CERTIFICATE" not in certificate:
        raise RuntimeError("Target certificate enrollment failed")
    (LOCAL / "target_ca.pem").write_text(certificate)
    (LOCAL / "target_ca.pem").chmod(0o600)

    report_path = None
    with SsmTunnel(
        instance,
        inventory["session_document"],
        inventory["region"],
        LOCAL / "tunnel.log",
    ):
        token = (LOCAL / "target_token").read_text().strip()
        with httpx.Client(
            base_url="https://127.0.0.1:18765",
            verify=str(LOCAL / "target_ca.pem"),
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=10,
        ) as target:
            request_id = "a" * 32
            sample = {"name": "sample_cpu_pressure", "duration_seconds": 1, "limit": 5}
            first = target.post("/v1/probe", json=sample, headers={"Idempotency-Key": request_id})
            first.raise_for_status()
            replay = target.post("/v1/probe", json=sample, headers={"Idempotency-Key": request_id})
            replay.raise_for_status()
            if replay.json() != first.json():
                raise RuntimeError("Target did not replay the admitted request")
            conflict = target.post(
                "/v1/probe",
                json={"name": "rank_processes", "duration_seconds": 1, "limit": 5},
                headers={"Idempotency-Key": request_id},
            )
            if conflict.status_code != 409:
                raise RuntimeError("Target accepted a reused request ID with different arguments")
            excessive = target.post(
                "/v1/probe",
                json={"name": "sample_cpu_pressure", "duration_seconds": 60, "limit": 5},
                headers={"Idempotency-Key": "b" * 32},
            )
            if excessive.status_code != 422:
                raise RuntimeError("Target accepted an excessive probe duration")

        run(
            [
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "-f",
                "compose.aws.yaml",
                "up",
                "-d",
                "--wait",
                "--force-recreate",
                "broker",
            ]
        )
        doctor = run([".venv/bin/oncall", "doctor"])
        readiness = json.loads(doctor.stdout)
        if readiness["target"].get("target_id") != instance:
            raise RuntimeError("Broker is not connected to the expected EC2 target")

        aws.send_command(
            instance,
            [
                "systemctl stop oncall-cpu-fault.service >/dev/null 2>&1 || true",
                "systemd-run --unit=oncall-cpu-fault --property=RuntimeMaxSec=35 "
                "/opt/oncall/bin/python -m oncall.lab_fault --seconds 30 --workers 2",
            ],
        )
        investigation = run(
            [
                ".venv/bin/oncall",
                "investigate",
                "--symptom",
                "Investigate the remote EC2 CPU pressure and cite target evidence.",
            ]
        )
        match = re.search(r"Report: (.+)$", investigation.stdout, re.MULTILINE)
        if not match:
            raise RuntimeError("Investigation did not export a report")
        report_path = Path(match.group(1))
        state = json.loads((report_path.parent / "report.json").read_text())
        evidence = state["evidence"]
        if {item["target_id"] for item in evidence} != {instance}:
            raise RuntimeError("Report contains evidence from the wrong target")
        process_facts = [
            fact
            for item in evidence
            if (fact := item["facts"]) is not None and fact["kind"] == "processes"
        ]
        if not process_facts or not any(
            process["name"] == "python" and process["cpu_pct_one_core"] > 10
            for process in process_facts[0]["processes"]
        ):
            raise RuntimeError("Remote CPU fault was not attributed")
        aws.send_command(instance, ["systemctl stop oncall-cpu-fault.service || true"])

        with admin_client() as client:
            cancel_run = client.post("/admin/start").raise_for_status().json()["investigation_id"]
            client.post("/admin/cancel").raise_for_status()
            status = client.get("/admin/state").raise_for_status().json()["status"]
            if status != "cancelled":
                raise RuntimeError("Remote-mode cancellation did not persist")

    # Recreate the remote broker so no pooled HTTP connection can mask tunnel loss.
    run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.aws.yaml",
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "broker",
        ]
    )
    unavailable = run([".venv/bin/oncall", "doctor"], check=False)
    if unavailable.returncode == 0:
        raise RuntimeError("Doctor did not fail after the SSM tunnel closed")

    invalid = run(
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            instance,
            "--document-name",
            inventory["session_document"],
            "--parameters",
            json.dumps({"portNumber": ["22"], "localPortNumber": ["18765"]}),
            "--region",
            inventory["region"],
        ],
        check=False,
    )
    if invalid.returncode == 0:
        raise RuntimeError("Fixed-port SSM document accepted port 22")

    run(["docker", "compose", "up", "-d", "--wait", "--force-recreate", "broker"])
    result = {
        "instance_id": instance,
        "report": str(report_path),
        "remote_readiness": "passed",
        "target_deduplication": "passed",
        "target_hard_limits": "passed",
        "cpu_attribution": "passed",
        "cancellation": "passed",
        "cancelled_run": cancel_run,
        "closed_tunnel_failure": "passed",
        "fixed_port_rejection": "passed",
    }
    (LOCAL / f"acceptance-{instance}.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
