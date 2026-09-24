"""Run only inside the untrusted agent container; never receives provider/AWS keys."""

import argparse
import json
import os
import signal
import threading
from pathlib import Path

from deepseek_harness import DeepSeekHarness  # type: ignore[import-untyped]

from oncall.http_boundary import secret_file


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
        model=os.environ.get("ONCALL_MODEL", "gpt-4.1-mini"),
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
            result = harness.run(args.symptom, session_id=args.session_id)
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "finish_reason": result.finish_reason,
                        "final_response": result.final_response,
                    }
                )
            )
    finally:
        timer.cancel()
        harness.close()
    if cancelled.is_set():
        raise SystemExit(124)


if __name__ == "__main__":
    main()
