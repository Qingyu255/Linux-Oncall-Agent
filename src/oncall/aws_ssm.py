"""Trusted operator-side AWS CLI and Session Manager lifecycle adapter."""

import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self

INSTANCE = re.compile(r"^i-[a-f0-9]{17}$")
DOCUMENT = re.compile(r"^[A-Za-z0-9_.-]{3,128}$")
REGION = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]$")
SESSION = re.compile(r"^[A-Za-z0-9-]{1,96}$")
SESSION_LOG = re.compile(r"Starting session with SessionId: ([A-Za-z0-9-]{1,96})")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    stdout: str


class AwsCli:
    def __init__(self, region: str) -> None:
        if not REGION.fullmatch(region):
            raise ValueError("Invalid AWS region")
        self.region = region

    def _run(self, arguments: list[str], *, timeout: int = 60) -> str:
        result = subprocess.run(
            ["aws", *arguments, "--region", self.region],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()

    def wait_managed_node(self, instance_id: str, timeout: int = 600) -> None:
        self._instance(instance_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self._run(
                [
                    "ssm",
                    "describe-instance-information",
                    "--filters",
                    f"Key=InstanceIds,Values={instance_id}",
                    "--query",
                    "InstanceInformationList[0].PingStatus",
                    "--output",
                    "text",
                ]
            )
            if output == "Online":
                return
            time.sleep(10)
        raise TimeoutError("SSM managed node did not become online")

    def send_command(
        self, instance_id: str, commands: list[str], *, timeout: int = 600
    ) -> CommandResult:
        self._instance(instance_id)
        command_id = self._run(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--parameters",
                json.dumps({"commands": commands}),
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ]
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._run(
                    [
                        "ssm",
                        "get-command-invocation",
                        "--command-id",
                        command_id,
                        "--instance-id",
                        instance_id,
                        "--output",
                        "json",
                    ]
                )
            except subprocess.CalledProcessError as error:
                if "InvocationDoesNotExist" not in (error.stderr or ""):
                    raise
                time.sleep(2)
                continue
            state = json.loads(raw)
            status = state["Status"]
            if status == "Success":
                return CommandResult(command_id, state.get("StandardOutputContent", ""))
            if status in {"Cancelled", "Failed", "TimedOut", "Cancelling"}:
                raise RuntimeError(f"SSM command {command_id} ended as {status}")
            time.sleep(5)
        raise TimeoutError(f"SSM command {command_id} did not finish")

    def enroll(self, parameter_name: str, output: Path) -> None:
        if not re.fullmatch(r"/linux-oncall/i-[a-f0-9]{17}/target-token", parameter_name):
            raise ValueError("Invalid enrollment parameter name")
        value = self._run(
            [
                "ssm",
                "get-parameter",
                "--name",
                parameter_name,
                "--with-decryption",
                "--query",
                "Parameter.Value",
                "--output",
                "text",
            ]
        )
        if len(value) < 24:
            raise RuntimeError("Enrollment token is missing or short")
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)

    def delete_parameter(self, parameter_name: str) -> None:
        self._run(["ssm", "delete-parameter", "--name", parameter_name])

    def terminate_session(self, session_id: str) -> None:
        if not SESSION.fullmatch(session_id):
            raise ValueError("Invalid SSM session ID")
        self._run(["ssm", "terminate-session", "--session-id", session_id])

    @staticmethod
    def _instance(instance_id: str) -> None:
        if not INSTANCE.fullmatch(instance_id):
            raise ValueError("Invalid EC2 instance ID")


class SsmTunnel:
    def __init__(
        self,
        instance_id: str,
        document: str,
        region: str,
        log_path: Path,
        local_port: int = 18765,
    ) -> None:
        AwsCli._instance(instance_id)
        if not DOCUMENT.fullmatch(document):
            raise ValueError("Invalid SSM document name")
        if local_port != 18765:
            raise ValueError("Only the fixed local port is supported")
        self.instance_id = instance_id
        self.document = document
        self.aws = AwsCli(region)
        self.region = region
        self.log_path = log_path
        self.local_port = local_port
        self.process: subprocess.Popen[bytes] | None = None
        self.log: BinaryIO | None = None
        self.session_id: str | None = None

    def __enter__(self) -> Self:
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [
                "aws",
                "ssm",
                "start-session",
                "--target",
                self.instance_id,
                "--document-name",
                self.document,
                "--parameters",
                json.dumps({"portNumber": ["8765"], "localPortNumber": ["18765"]}),
                "--region",
                self.region,
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.close()
                raise RuntimeError("SSM tunnel exited during startup")
            self._capture_session_id()
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                    if not self.session_id:
                        time.sleep(0.2)
                        self._capture_session_id()
                    if self.session_id:
                        return self
            except OSError:
                time.sleep(1)
        self.close()
        raise TimeoutError("SSM tunnel did not open the fixed local port")

    def close(self) -> None:
        if self.process:
            remote_terminated = False
            if self.session_id:
                try:
                    self.aws.terminate_session(self.session_id)
                    remote_terminated = True
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    # The service may have already closed the session. The local process
                    # group is still reaped below so cleanup remains bounded.
                    pass
            if not remote_terminated:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                self.process.wait(timeout=2)
        if self.log:
            self.log.close()
        self.process = None
        self.log = None
        self.session_id = None

    def _capture_session_id(self) -> None:
        if self.session_id or not self.log_path.exists():
            return
        match = SESSION_LOG.search(self.log_path.read_text(errors="replace"))
        if match:
            self.session_id = match.group(1)

    def __exit__(self, *_: object) -> None:
        self.close()
