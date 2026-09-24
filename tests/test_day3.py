import json
from dataclasses import dataclass

import pytest

from oncall.domain import CpuFacts, Hypothesis, Observation, ProbeRequest, utcnow
from oncall.faults import FaultController, OperatorExecutor
from oncall.parsers import (
    parse_meminfo,
    parse_memory_events,
    parse_mountinfo,
    parse_pressure,
    parse_vmstat,
)
from oncall.probes import CommandCapture, JournalProbe, JournalReader, RegexSanitizer
from oncall.storage import EvidenceStore


def proc_fixture(tmp_path):
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-3\n")
    return proc


def test_memory_and_mount_parsers_preserve_scope_and_missing_values():
    memory = parse_meminfo(
        "MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    assert memory["MemAvailable"] == 256000
    vmstat = parse_vmstat("pgfault 10\npgmajfault 2\noom_kill 1\n")
    assert vmstat.pgmajfault == 2 and vmstat.pswpin is None
    pressure = parse_pressure("some avg10=1.00 avg60=2.00 avg300=3.00 total=42\n")
    assert pressure["some"].total_usec == 42
    events = parse_memory_events("low 0\nhigh 2\nmax 3\noom 1\noom_kill 1\n")
    assert events.oom_kill == 1
    mount = parse_mountinfo(
        "1 0 0:1 / / rw - ext4 /dev/root rw\n2 1 0:2 / /lab ro - xfs /dev/xvdf ro\n",
        "/lab/data",
    )
    assert mount.filesystem_type == "xfs" and "ro" in mount.options


class FixtureJournal(JournalReader):
    async def read(self, unit, since_seconds, limit):
        fake_access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        record = {
            "__REALTIME_TIMESTAMP": "123",
            "PRIORITY": "3",
            "_SYSTEMD_UNIT": unit,
            "MESSAGE": f"token=hunter2 Authorization: Bearer abc {fake_access_key}",
        }
        return CommandCapture((json.dumps(record) + "\n").encode(), b"", False)


async def test_journal_is_sanitized_before_it_becomes_an_artifact(tmp_path):
    probe = JournalProbe("target", proc_fixture(tmp_path), FixtureJournal(), RegexSanitizer())
    result = await probe.collect(
        ProbeRequest(name="query_service_journal", unit="oncall-target.service")
    )
    fake_access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert result.status == "ok"
    assert "hunter2" not in result.raw and fake_access_key not in result.raw
    assert result.facts is not None and result.facts.kind == "journal"


def observation(raw="é\nsecond line"):
    request = ProbeRequest(name="sample_cpu_pressure")
    return Observation(
        target_id="target",
        boot_id="boot",
        request=request,
        started_at=utcnow(),
        completed_at=utcnow(),
        duration_ms=1,
        facts=CpuFacts(
            logical_cpus=1,
            host_busy_pct=1,
            host_iowait_pct=0,
            host_steal_pct=0,
            probe_cgroup_cpu_cores_used=0,
            probe_cgroup_quota_cores=1,
            probe_cgroup_throttled_usec_delta=0,
            load1=0,
        ),
        raw=raw,
        raw_bytes=len(raw.encode()),
    )


def test_artifact_pages_are_lossless_and_hypotheses_are_versioned(tmp_path):
    store = EvidenceStore(tmp_path)
    run = store.create_run("test")
    evidence = store.add(run, observation())
    first = store.artifact_page(run, evidence.artifact_id, 0, 1)
    second = store.artifact_page(run, evidence.artifact_id, first["next_offset"], 100)
    assert first["text"] + second["text"] == "é\nsecond line"
    initial = store.update_hypothesis(
        run,
        Hypothesis(
            hypothesis_id="memory_pressure",
            claim="Memory may be constrained",
            status="open",
            supporting_evidence_ids=(evidence.evidence_id,),
        ),
    )
    revised = store.update_hypothesis(run, initial.model_copy(update={"status": "weakened"}))
    assert revised.version == 2
    assert store.hypotheses(run) == [revised]
    store.close()


@dataclass
class FixtureExecutor(OperatorExecutor):
    outputs: list[str]

    def execute(self, commands, timeout=180):
        return self.outputs.pop(0)


def test_fault_controller_persists_lease_and_verifies_cleanup(tmp_path):
    executor = FixtureExecutor(["", "ready", "", "clean"])
    controller = FaultController(executor, tmp_path / "lease.json", "i-0123456789abcdef0")
    lease = controller.start("cpu", 30)
    assert lease.status == "ready" and controller.current() == lease
    controller.stop()
    assert controller.current() is None


def test_probe_requests_reject_authority_for_the_wrong_capability():
    with pytest.raises(ValueError):
        ProbeRequest(name="inspect_filesystem", mount_id="root", scope_id="self")


def test_fault_plans_require_new_oom_and_verify_resource_reset():
    from oncall.faults import SCENARIOS

    memory = SCENARIOS["memory"].plan(60)
    assert any(".oncall-oom-before" in command for command in memory.start)
    assert any('test "$after" -gt "$before"' in command for command in memory.ready)
    assert any("MemoryMax=infinity" in command for command in memory.cleanup)
    assert any("memory.max" in command for command in memory.verify_clean)
    filesystem = SCENARIOS["filesystem"].plan(60)
    assert any("blocks * size" in command for command in filesystem.verify_clean)


def test_hypothesis_cannot_use_one_observation_as_support_and_contradiction():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="both support and contradict"):
        Hypothesis(
            hypothesis_id="conflicted_claim",
            claim="A condition exists",
            status="open",
            supporting_evidence_ids=("a" * 32,),
            contradicting_evidence_ids=("a" * 32,),
        )
