"""Operator-only fault lifecycle with leases, readiness, and verified cleanup."""

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from oncall.aws_ssm import AwsCli

ScenarioName = Literal["cpu", "memory", "filesystem"]


class OperatorExecutor(ABC):
    @abstractmethod
    def execute(self, commands: tuple[str, ...], timeout: int = 180) -> str: ...


class SsmOperatorExecutor(OperatorExecutor):
    def __init__(self, region: str, instance_id: str) -> None:
        self.aws = AwsCli(region)
        self.instance_id = instance_id

    def execute(self, commands: tuple[str, ...], timeout: int = 180) -> str:
        return self.aws.send_command(self.instance_id, list(commands), timeout=timeout).stdout


@dataclass(frozen=True)
class FaultPlan:
    start: tuple[str, ...]
    ready: tuple[str, ...]
    cleanup: tuple[str, ...]
    verify_clean: tuple[str, ...]


class FaultScenario(ABC):
    name: ScenarioName

    @abstractmethod
    def plan(self, ttl_seconds: int) -> FaultPlan: ...

    @staticmethod
    def common_cleanup() -> tuple[str, ...]:
        return (
            "systemctl stop oncall-lab-workload.service oncall-lab-sentinel.service "
            ">/dev/null 2>&1 || true",
            "systemctl reset-failed oncall-lab-workload.service oncall-lab-sentinel.service "
            ">/dev/null 2>&1 || true",
            "rm -f /var/lib/oncall-lab/data/.oncall-fault.bin "
            "/var/lib/oncall-lab/data/.oncall-fault-ready "
            "/var/lib/oncall-lab/data/.oncall-write-probe "
            "/var/lib/oncall-lab/data/.oncall-oom-before",
        )


class CpuScenario(FaultScenario):
    name: ScenarioName = "cpu"

    def plan(self, ttl_seconds: int) -> FaultPlan:
        return FaultPlan(
            start=(
                "set -e",
                *self.common_cleanup(),
                f"systemd-run --unit=oncall-lab-workload --property=RuntimeMaxSec={ttl_seconds} "
                "/opt/oncall/bin/python -m oncall.lab_fault cpu --seconds "
                f"{ttl_seconds - 5} --workers 2",
            ),
            ready=(
                "set -e",
                "sleep 2",
                "systemctl is-active --quiet oncall-lab-workload.service",
                "echo ready",
            ),
            cleanup=self.common_cleanup(),
            verify_clean=(
                "set -e",
                "! systemctl is-active --quiet oncall-lab-workload.service",
                "test ! -e /var/lib/oncall-lab/data/.oncall-fault.bin",
                "echo clean",
            ),
        )


class MemoryScenario(FaultScenario):
    name: ScenarioName = "memory"

    def plan(self, ttl_seconds: int) -> FaultPlan:
        cleanup = (
            "systemctl set-property oncall-lab.slice MemoryMax=infinity >/dev/null 2>&1 || true",
            *self.common_cleanup(),
        )
        return FaultPlan(
            start=(
                "set -e",
                *self.common_cleanup(),
                f"systemd-run --unit=oncall-lab-sentinel --slice=oncall-lab.slice "
                f"--property=RuntimeMaxSec={ttl_seconds} /usr/bin/sleep {ttl_seconds - 5}",
                "awk '$1 == \"oom_kill\" {print $2}' "
                "/sys/fs/cgroup/oncall.slice/oncall-lab.slice/memory.events "
                "> /var/lib/oncall-lab/data/.oncall-oom-before",
                "systemctl set-property oncall-lab.slice MemoryMax=48M",
                f"systemd-run --unit=oncall-lab-workload --slice=oncall-lab.slice "
                f"--property=RuntimeMaxSec={ttl_seconds} /opt/oncall/bin/python "
                f"-m oncall.lab_fault memory --seconds {ttl_seconds - 5} --mebibytes 128",
            ),
            ready=(
                "set -e",
                "sleep 4",
                "before=$(cat /var/lib/oncall-lab/data/.oncall-oom-before); "
                "after=$(awk '$1 == \"oom_kill\" {print $2}' "
                "/sys/fs/cgroup/oncall.slice/oncall-lab.slice/memory.events); "
                'test "$after" -gt "$before"',
                "systemctl is-active --quiet oncall-lab-sentinel.service",
                "echo ready",
            ),
            cleanup=cleanup,
            verify_clean=(
                "set -e",
                "! systemctl is-active --quiet oncall-lab-sentinel.service",
                "! systemctl is-active --quiet oncall-lab-workload.service",
                "test ! -e /sys/fs/cgroup/oncall.slice/oncall-lab.slice/memory.max || "
                "grep -qx max /sys/fs/cgroup/oncall.slice/oncall-lab.slice/memory.max",
                "test ! -e /var/lib/oncall-lab/data/.oncall-oom-before",
                "echo clean",
            ),
        )


class FilesystemScenario(FaultScenario):
    name: ScenarioName = "filesystem"

    def plan(self, ttl_seconds: int) -> FaultPlan:
        return FaultPlan(
            start=(
                "set -e",
                *self.common_cleanup(),
                f"systemd-run --unit=oncall-lab-workload --uid=oncall "
                f"--property=RuntimeMaxSec={ttl_seconds} /opt/oncall/bin/python "
                f"-m oncall.lab_fault filesystem --seconds {ttl_seconds - 5} "
                "--reserve-mib 4 --maximum-mib 1024",
            ),
            ready=(
                "set -e",
                "for attempt in $(seq 1 30); do test -f "
                "/var/lib/oncall-lab/data/.oncall-fault-ready && break; sleep 1; done",
                "test -f /var/lib/oncall-lab/data/.oncall-fault-ready",
                "grep -q '\"write_errno\": 28' /var/lib/oncall-lab/data/.oncall-fault-ready",
                "echo ready",
            ),
            cleanup=self.common_cleanup(),
            verify_clean=(
                "set -e",
                "test ! -e /var/lib/oncall-lab/data/.oncall-fault.bin",
                "test ! -e /var/lib/oncall-lab/data/.oncall-fault-ready",
                "test ! -e /var/lib/oncall-lab/data/.oncall-write-probe",
                "blocks=$(stat -f -c %a /var/lib/oncall-lab/data); "
                "size=$(stat -f -c %S /var/lib/oncall-lab/data); "
                "test $((blocks * size)) -gt 33554432",
                "echo clean",
            ),
        )


SCENARIOS: dict[ScenarioName, FaultScenario] = {
    "cpu": CpuScenario(),
    "memory": MemoryScenario(),
    "filesystem": FilesystemScenario(),
}


@dataclass(frozen=True)
class FaultLease:
    lease_id: str
    scenario: ScenarioName
    instance_id: str
    expires_at: float
    status: Literal["injecting", "ready", "dirty"]


class FaultController:
    """Facade over scenario strategies; only the trusted CLI constructs it."""

    def __init__(self, executor: OperatorExecutor, state_path: Path, instance_id: str) -> None:
        self.executor = executor
        self.state_path = state_path
        self.instance_id = instance_id

    def current(self) -> FaultLease | None:
        if not self.state_path.exists():
            return None
        return FaultLease(**json.loads(self.state_path.read_text()))

    def start(self, scenario_name: ScenarioName, ttl_seconds: int = 120) -> FaultLease:
        if not 30 <= ttl_seconds <= 120:
            raise ValueError("Fault TTL must be 30..120 seconds")
        current = self.current()
        if current and current.status != "dirty" and current.expires_at > time.time():
            raise RuntimeError("A fault lease is already active")
        if current:
            self.stop()
        scenario = SCENARIOS[scenario_name]
        lease = FaultLease(
            uuid.uuid4().hex,
            scenario_name,
            self.instance_id,
            time.time() + ttl_seconds,
            "injecting",
        )
        self._write(lease)
        plan = scenario.plan(ttl_seconds)
        try:
            self.executor.execute(plan.start, ttl_seconds + 30)
            if self.executor.execute(plan.ready, 60).strip() != "ready":
                raise RuntimeError("Fault readiness check failed")
            lease = FaultLease(**{**asdict(lease), "status": "ready"})
            self._write(lease)
            return lease
        except BaseException:
            try:
                self.executor.execute(plan.cleanup)
                self.executor.execute(plan.verify_clean)
                self.state_path.unlink(missing_ok=True)
            except BaseException:
                self._write(FaultLease(**{**asdict(lease), "status": "dirty"}))
            raise

    def stop(self) -> None:
        lease = self.current()
        if lease is None:
            return
        if lease.instance_id != self.instance_id:
            raise RuntimeError("Fault lease belongs to another target")
        plan = SCENARIOS[lease.scenario].plan(120)
        try:
            self.executor.execute(plan.cleanup)
            if self.executor.execute(plan.verify_clean).strip() != "clean":
                raise RuntimeError("Fault cleanup verification failed")
        except BaseException:
            self._write(FaultLease(**{**asdict(lease), "status": "dirty"}))
            raise
        self.state_path.unlink(missing_ok=True)

    def _write(self, lease: FaultLease) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(lease), indent=2))
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)
