from dataclasses import dataclass

from oncall.lab.faults import SCENARIOS, FaultController, OperatorExecutor


@dataclass
class FixtureExecutor(OperatorExecutor):
    outputs: list[str]

    def execute(self, commands: tuple[str, ...], timeout: int = 180) -> str:
        return self.outputs.pop(0)


def test_fault_controller_persists_lease_and_verifies_cleanup(tmp_path):
    executor = FixtureExecutor(["", "ready", "", "clean"])
    controller = FaultController(executor, tmp_path / "lease.json", "i-0123456789abcdef0")
    lease = controller.start("cpu", 30)
    assert lease.status == "ready" and controller.current() == lease
    controller.stop()
    assert controller.current() is None


def test_fault_plans_require_new_oom_and_verify_resource_reset():
    memory = SCENARIOS["memory"].plan(60)
    watchdog = next(
        command
        for command in memory.start
        if command.startswith("systemd-run --unit=oncall-lab-watchdog")
    )
    assert "sleep 60" in watchdog
    assert "MemoryMax=infinity" in watchdog
    assert ".oncall-fault.bin" in watchdog
    assert any(".oncall-oom-before" in command for command in memory.start)
    assert any('test "$after" -gt "$before"' in command for command in memory.ready)
    assert any("MemoryMax=infinity" in command for command in memory.cleanup)
    assert any("memory.max" in command for command in memory.verify_clean)
    filesystem = SCENARIOS["filesystem"].plan(60)
    assert any("oncall-lab-watchdog" in command for command in filesystem.start)
    assert any("blocks * size" in command for command in filesystem.verify_clean)
