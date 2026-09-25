"""Run only inside the untrusted agent container; never receives provider/AWS keys."""

import argparse
import json
import os
import signal
import threading
from pathlib import Path

from deepseek_harness import DeepSeekHarness  # type: ignore[import-untyped]

from oncall.harness_progress import PROGRESS_PROTOCOL, HarnessProgressAdapter
from oncall.http_boundary import secret_file


def emit(value: dict[str, object]) -> None:
    """Write one flushed protocol record for the parent CLI."""
    print(json.dumps(value, separators=(",", ":")), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument(
        "--symptom", default="Investigate the target CPU activity and cite evidence."
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 180:
        parser.error("timeout must be 1..180 seconds")
    Path("/workspace/dsh").mkdir(exist_ok=True)
    token = secret_file("agent_token")
    harness = DeepSeekHarness(
        provider="oncall-openai",
        model=os.environ.get("ONCALL_MODEL", "gpt-5.6-terra"),
        max_tokens=2048,
        profile="sdk-minimal",
        patches=("/app/harness/oncall.patch.yml",),
        cwd="/workspace",
        dsh_home="/workspace/dsh",
        env={"ONCALL_RELAY_TOKEN": token},
        initialize_timeout_seconds=45,
        shutdown_timeout_seconds=2,
    )
    cancelled = threading.Event()

    def stop(*_: object) -> None:
        cancelled.set()
        harness.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    timer = threading.Timer(args.timeout, stop)
    timer.daemon = True
    timer.start()
    try:
        with harness:
            emit({"protocol": PROGRESS_PROTOCOL, "kind": "harness_started"})
            progress = HarnessProgressAdapter(emit)
            result = harness.run(
                args.symptom,
                session_id=args.session_id,
                on_notification=lambda item: progress.notification(item.method, item.payload),
            )
            emit(
                {
                    "protocol": PROGRESS_PROTOCOL,
                    "kind": "result",
                    "session_id": result.session_id,
                    "finish_reason": result.finish_reason,
                }
            )
    except Exception:
        emit({"protocol": PROGRESS_PROTOCOL, "kind": "harness_failed"})
        raise
    finally:
        timer.cancel()
        harness.close()
    if cancelled.is_set():
        raise SystemExit(124)


if __name__ == "__main__":
    main()
