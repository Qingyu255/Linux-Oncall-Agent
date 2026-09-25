"""Operator CLI. Docker authority is here, never exposed as an agent tool."""

import json
import selectors
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from oncall.faults import SCENARIOS, FaultController, SsmOperatorExecutor
from oncall.harness_progress import PROGRESS_PROTOCOL, progress_message
from oncall.operator_view import operator_progress_message
from oncall.progress_projection import bounded_assistant_response

app = typer.Typer(
    help="Evidence-driven Linux incident investigation",
    invoke_without_command=True,
    no_args_is_help=False,
)
ROOT = Path(__file__).resolve().parents[2]
console = Console()

SESSION_COMMAND_SLASHES = str.maketrans({"／": "/", "⁄": "/"})
DEFAULT_ACTIVITY_MESSAGE = "[cyan]Investigating…[/]"


@dataclass(frozen=True)
class HarnessRunOutcome:
    return_code: int
    assistant_response: str | None
    tool_calls: int


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def session_command(message: str) -> str | None:
    """Normalize a slash command, including bracketed-paste and Unicode slash forms."""
    cleaned = message.replace("\x1b[200~", "").replace("\x1b[201~", "")
    cleaned = cleaned.strip().translate(SESSION_COMMAND_SLASHES)
    if not cleaned.startswith("/"):
        return None
    return "/" + cleaned[1:].strip().casefold()


def failure_detail(state: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for event in state.get("events", []):
        if event.get("kind") != "probe_failed":
            continue
        failure = str(event.get("payload", {}).get("type", "unknown"))
        counts[failure] = counts.get(failure, 0) + 1
    if not counts:
        return ""
    summary = ", ".join(f"{name} × {count}" for name, count in sorted(counts.items()))
    return f"\nProbe failures: {summary}"


def permits_conversational_response(state: dict[str, Any], outcome: HarnessRunOutcome) -> bool:
    """Allow direct model text only when no diagnostic action was attempted."""
    events = state.get("events")
    allowed_events = {"started", "continued_from", "model_request"}
    return (
        state.get("status") == "running"
        and state.get("report") is None
        and state.get("probe_calls") == 0
        and not state.get("evidence")
        and not state.get("hypotheses")
        and outcome.tool_calls == 0
        and outcome.assistant_response is not None
        and isinstance(events, list)
        and all(isinstance(event, dict) and event.get("kind") in allowed_events for event in events)
    )


def fault_controller(inventory_path: Path) -> FaultController:
    inventory = json.loads(inventory_path.read_text())
    if inventory.get("disposable") is not True:
        raise typer.BadParameter("Inventory must explicitly mark the target disposable")
    instance_id, region = inventory.get("instance_id"), inventory.get("region")
    if not isinstance(instance_id, str) or not isinstance(region, str):
        raise typer.BadParameter("Inventory is missing target identity or region")
    state = ROOT / ".local/faults" / f"{instance_id}.json"
    return FaultController(SsmOperatorExecutor(region, instance_id), state, instance_id)


def client() -> httpx.Client:
    token = (ROOT / ".local/lab/secrets/admin_token").read_text().strip()
    return httpx.Client(
        base_url="http://127.0.0.1:8787",
        timeout=10,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    )


def evidence_view(state: dict[str, Any]) -> list[dict[str, Any]]:
    current = [{**item, "evidence_scope": "current"} for item in state.get("evidence", [])]
    continuation = state.get("continuation")
    historical = (
        continuation.get("historical_evidence", []) if isinstance(continuation, dict) else []
    )
    return [*current, *historical]


def render(state: dict[str, Any]) -> str:
    lines = [
        "# Linux OnCall investigation",
        "",
        f"Run: `{state['investigation_id']}`",
        f"Status: **{state['status']}**",
        f"Provider mode: **{state['mode']}**",
        "",
    ]
    parent = state.get("parent_investigation_id")
    if parent:
        lines.insert(4, f"Parent run: `{parent}`")
    report = state.get("report")
    if report:
        lines += [report["summary"], "", "## Findings", ""]
        for claim in report["claims"]:
            refs = ", ".join(f"`{ref}`" for ref in claim["evidence_ids"])
            scope = claim.get("evidence_scope", "current")
            lines.append(f"- [{scope}] {claim['text']} Evidence: {refs}")
        for field in ("alternatives", "limitations", "next_steps"):
            lines += ["", f"## {field.replace('_', ' ').title()}", ""]
            lines += [f"- {item}" for item in report[field]]
    lines += ["", "## Evidence", ""]
    for evidence in evidence_view(state):
        lines += [
            f"### {evidence['evidence_id']}",
            "",
            f"Scope: `{evidence.get('evidence_scope', 'current')}`",
            f"Target: `{evidence['target_id']}`; boot: `{evidence['boot_id']}`",
            f"Interval: {evidence['started_at']} to {evidence['completed_at']}",
            f"Quality: `{evidence['status']}`; error: `{evidence['error_code'] or 'none'}`",
            f"Artifact: {evidence['artifact_bytes']} bytes; "
            f"truncated: `{evidence['artifact_truncated']}`",
            "",
            "```json",
            json.dumps(evidence["facts"], indent=2),
            "```",
            "",
            f"Artifact SHA-256: `{evidence['artifact_sha256']}`",
        ]
        if evidence["limitations"]:
            lines.append("Limitations: " + "; ".join(evidence["limitations"]))
        lines.append("")
    return "\n".join(lines)


def show_result(
    state: dict[str, Any],
    report_path: Path,
    json_path: Path,
    elapsed_seconds: float,
    *,
    verbose: bool = False,
) -> None:
    """Render the operator result while Markdown retains complete evidence."""
    report = state.get("report")
    if not isinstance(report, dict):
        raise ValueError("Completed investigation has no report")

    if verbose:
        metadata = Table.grid(padding=(0, 2))
        metadata.add_column(style="dim")
        metadata.add_column(style="bold")
        metadata.add_row("Run", str(state["investigation_id"]))
        if state.get("parent_investigation_id"):
            metadata.add_row("Parent", str(state["parent_investigation_id"]))
        metadata.add_row("Provider", str(state["mode"]))
        metadata.add_row("Outcome", str(report["outcome"]))
        metadata.add_row("Elapsed", f"{elapsed_seconds:.1f}s")
        metadata.add_row("Probe calls", str(state.get("probe_calls", "unavailable")))
        metadata.add_row("Captured", f"{int(state.get('captured_bytes', 0)):,} bytes")
        console.print(
            Panel(metadata, title="[bold green]Investigation complete[/]", border_style="green")
        )
    console.print(Panel(escape(str(report["summary"])), title="Diagnosis", border_style="cyan"))

    console.print("\n[bold]Findings[/]")
    for index, claim in enumerate(report["claims"], start=1):
        console.print(f"  [bold cyan]{index}.[/] {escape(str(claim['text']))}")
        if verbose:
            references = ", ".join(str(item)[:8] for item in claim["evidence_ids"])
            scope = str(claim.get("evidence_scope", "current"))
            console.print(f"     [dim]Evidence ({scope}): {references}[/]")

    if verbose:
        evidence_table = Table(title="Investigation evidence", header_style="bold magenta")
        evidence_table.add_column("Scope")
        evidence_table.add_column("Probe")
        evidence_table.add_column("Quality")
        evidence_table.add_column("Bytes", justify="right")
        evidence_table.add_column("Evidence ID")
        for evidence in evidence_view(state):
            evidence_table.add_row(
                str(evidence.get("evidence_scope", "current")),
                str(evidence["request"]["name"]),
                str(evidence["status"]),
                f"{int(evidence['artifact_bytes']):,}",
                str(evidence["evidence_id"])[:12],
            )
        console.print(evidence_table)

    for title, field, style in (
        ("Limitations", "limitations", "yellow"),
        ("Recommended next steps", "next_steps", "blue"),
    ):
        content = Text()
        for index, item in enumerate(report[field], start=1):
            content.append(f"{index}. ", style=f"bold {style}")
            content.append(str(item))
            if index < len(report[field]):
                content.append("\n")
        console.print(Panel(content, title=title, border_style=style))

    console.print(f"[dim]Detailed report: {display_path(report_path)}[/]")
    if verbose:
        console.print(f"[bold]JSON report:[/] {display_path(json_path)}")


def save_state(state: dict[str, Any], filename: str = "report.json") -> Path:
    run_id = state.get("investigation_id")
    if not isinstance(run_id, str):
        raise ValueError("Investigation state is missing its run ID")
    output = ROOT / ".local/reports" / run_id
    output.mkdir(parents=True, exist_ok=True)
    path = output / filename
    path.write_text(json.dumps(state, indent=2) + "\n")
    return path


def show_progress(
    event: dict[str, Any],
    started: float,
    *,
    verbose: bool = False,
    activity: Status | None = None,
) -> None:
    """Render one safe event produced by the sandbox progress adapter."""
    rendered = progress_message(event) if verbose else operator_progress_message(event)
    if rendered is None:
        return
    style, symbol, message = rendered
    if activity is not None and event.get("kind") == "tool_started":
        activity.update(f"[{style}]{escape(message)}[/]")
        return
    if activity is not None:
        activity.stop()
    prefix = f"[dim]{time.monotonic() - started:6.1f}s[/] " if verbose else ""
    console.print(f"{prefix}[{style}]{symbol}[/] {escape(message)}")
    if activity is not None:
        activity.update(DEFAULT_ACTIVITY_MESSAGE)
        activity.start()


def run_harness(
    run: str,
    symptom: str,
    started: float,
    timeout: float = 175,
    *,
    verbose: bool = False,
    activity: Status | None = None,
) -> HarnessRunOutcome:
    """Stream the runner's JSONL protocol while retaining an outer hard deadline."""
    command = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "agent",
        "--session-id",
        run,
        "--symptom",
        symptom,
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    if process.stdout is None:  # pragma: no cover - Popen guarantees it for PIPE
        process.kill()
        raise RuntimeError("Harness output pipe was not created")
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    assistant_response: str | None = None
    tool_calls = 0

    def consume(line: str) -> None:
        nonlocal assistant_response, tool_calls
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict) and event.get("protocol") == PROGRESS_PROTOCOL:
            if event.get("kind") == "tool_started":
                tool_calls += 1
        response = handle_harness_line(line, started, verbose=verbose, activity=activity)
        if response is not None:
            assistant_response = response

    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise TimeoutError(f"Harness exceeded {timeout:.0f} seconds")
            for _key, _ in selector.select(timeout=min(0.25, remaining)):
                line = process.stdout.readline()
                if line:
                    consume(line)
        for line in process.stdout:
            consume(line)
        return HarnessRunOutcome(process.wait(), assistant_response, tool_calls)
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def handle_harness_line(
    line: str,
    started: float,
    *,
    verbose: bool = False,
    activity: Status | None = None,
) -> str | None:
    """Accept only the explicit progress protocol; discard runtime diagnostics."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(event, dict) and event.get("protocol") == PROGRESS_PROTOCOL:
        if event.get("kind") == "assistant_response":
            return bounded_assistant_response(event.get("text"))
        show_progress(event, started, verbose=verbose, activity=activity)
    return None


def continuation_prompt(run_id: str, message: str) -> str:
    """Build the bounded context contract for a follow-up investigation."""
    return (
        f"This is an explicit follow-up to investigation {run_id}. "
        "First call get_investigation_state. Prior-run evidence is historical. "
        "Use evidence_scope='historical' only for retrospective claims. "
        "For any claim about current target conditions, collect fresh evidence in this child "
        "investigation and use evidence_scope='current'. "
        f"Operator follow-up: {message}"
    )


def execute_investigation(
    connection: httpx.Client,
    run: str,
    prompt: str,
    display_message: str,
    parent_id: str | None = None,
    *,
    verbose: bool = False,
    exit_on_failure: bool = True,
    allow_conversation: bool = False,
) -> dict[str, Any] | None:
    if verbose:
        heading = f"[bold]Run[/] {run}"
        if parent_id is not None:
            heading += f"\n[bold]Parent[/] {parent_id}"
        console.print(
            Panel.fit(f"{heading}\n[dim]{escape(display_message)}[/]", title="Linux OnCall")
        )
    started = time.monotonic()
    try:
        activity = None if verbose else console.status(DEFAULT_ACTIVITY_MESSAGE, spinner="dots")
        if activity is not None:
            activity.start()
        try:
            outcome = run_harness(
                run,
                prompt,
                started,
                verbose=verbose,
                activity=activity,
            )
        finally:
            if activity is not None:
                activity.stop()
        if outcome.return_code:
            raise RuntimeError(f"Harness exited with {outcome.return_code}")
        state_value = connection.get("/admin/state").raise_for_status().json()
        if not isinstance(state_value, dict):
            raise RuntimeError("Broker returned an invalid investigation state")
        state: dict[str, Any] = state_value
        if state["status"] == "running":
            if allow_conversation and permits_conversational_response(state, outcome):
                connection.post("/admin/cancel").raise_for_status()
                assert outcome.assistant_response is not None
                console.print(Text(outcome.assistant_response))
                return None
            raise RuntimeError("Harness returned without an accepted report")
        json_path = save_state(state)
        report_path = json_path.with_name("report.md")
        report_path.write_text(render(state))
        show_result(
            state,
            report_path,
            json_path,
            time.monotonic() - started,
            verbose=verbose,
        )
        return state
    except BaseException as error:
        connection.post("/admin/cancel")
        state = connection.get("/admin/state").raise_for_status().json()
        failure_path = save_state(state, "failed-state.json")
        if verbose:
            console.print(
                Panel(
                    f"{escape(str(error))}{failure_detail(state)}\n\n"
                    f"Audit state: {display_path(failure_path)}",
                    title="[bold red]Investigation failed[/]",
                    border_style="red",
                )
            )
        else:
            if str(error) == "Harness returned without an accepted report":
                message = (
                    "I couldn't complete an evidence-backed diagnosis because the model stopped "
                    "before submitting a valid report. Please retry with the Linux symptom, "
                    "affected service, and approximate time."
                )
            else:
                message = f"I couldn't complete the investigation: {error}"
            console.print(f"[red]✗[/] {escape(message)}")
            console.print(f"[dim]Failure record: {display_path(failure_path)}[/]")
        if exit_on_failure:
            raise typer.Exit(1) from error
        return None


class InteractiveSession:
    """Operator shell backed by immutable, bounded investigation runs."""

    def __init__(self, connection: httpx.Client, *, verbose: bool = False) -> None:
        self._connection = connection
        self._verbose = verbose
        self._current_run: str | None = None

    def run(self) -> None:
        console.print("[bold cyan]Linux OnCall[/]")
        console.print(
            "Describe the incident. Follow-up messages stay in this incident; "
            "use [bold]/new[/] to start another."
        )
        console.print("[dim]Commands: /new, /status, /target, /verbose, /help, /exit[/]\n")
        while True:
            try:
                message = console.input("[bold cyan]oncall>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not message:
                continue
            command = session_command(message)
            if command is not None:
                if self._command(command):
                    return
                continue
            self._investigate(message)

    def _command(self, command: str) -> bool:
        if command in {"/exit", "/quit"}:
            return True
        if command == "/new":
            self._current_run = None
            console.print("[green]New incident ready.[/]")
            return False
        if command == "/status":
            if self._current_run is None:
                console.print("[dim]No incident context yet.[/]")
                return False
            state = (
                self._connection.get(f"/admin/runs/{self._current_run}").raise_for_status().json()
            )
            report = state.get("report")
            if isinstance(report, dict):
                console.print(Panel(escape(str(report["summary"])), title="Latest diagnosis"))
            else:
                console.print(f"[dim]Current incident status: {escape(str(state['status']))}[/]")
            return False
        if command == "/target":
            try:
                readiness = self._connection.get("/admin/readiness").raise_for_status().json()
                target = readiness["target"]
                target_id = str(target["target_id"])
                target_kind = "EC2 host" if target_id.startswith("i-") else "local Docker target"
                console.print(f"[bold]Target:[/] {escape(target_id)} [dim]({target_kind})[/]")
            except (httpx.HTTPError, KeyError, TypeError) as error:
                console.print(f"[red]Could not read target readiness: {escape(str(error))}[/]")
            return False
        if command == "/verbose":
            self._verbose = not self._verbose
            mode = "on" if self._verbose else "off"
            console.print(f"[dim]Technical progress is {mode}.[/]")
            return False
        if command == "/help":
            console.print(
                "Enter an incident description or follow-up question.\n"
                "[bold]/new[/] starts a separate incident. [bold]/status[/] shows the latest "
                "diagnosis. [bold]/target[/] shows which Linux system is connected. "
                "[bold]/verbose[/] toggles technical telemetry. "
                "[bold]/exit[/] leaves the session.\n"
                "Fault injection remains operator-only: run [bold]oncall lab-start[/] and "
                "[bold]oncall lab-stop[/] in another terminal."
            )
            return False
        console.print("[yellow]Unknown command. Use /help to list commands.[/]")
        return False

    def _investigate(self, message: str) -> None:
        parent_id = self._current_run
        try:
            if parent_id is None:
                response = self._connection.post("/admin/start")
                prompt = message
            else:
                response = self._connection.post(f"/admin/runs/{parent_id}/continue")
                prompt = continuation_prompt(parent_id, message)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409 and parent_id is not None:
                console.print(
                    "[yellow]This incident reached its bounded continuation limit. "
                    "Use /new to start a fresh incident.[/]"
                )
                return
            console.print(f"[red]Could not start the investigation: {escape(str(error))}[/]")
            return

        run_id = response.json().get("investigation_id")
        if not isinstance(run_id, str):
            console.print("[red]The broker returned an invalid investigation ID.[/]")
            return
        state = execute_investigation(
            self._connection,
            run_id,
            prompt,
            message,
            parent_id=parent_id,
            verbose=self._verbose,
            exit_on_failure=False,
            allow_conversation=True,
        )
        if state is not None:
            self._current_run = run_id


@app.callback()
def main(
    context: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show technical progress and evidence metadata.")
    ] = False,
) -> None:
    """Open an incident session when no subcommand is supplied."""
    if context.invoked_subcommand is None:
        with client() as connection:
            InteractiveSession(connection, verbose=verbose).run()


@app.command()
def doctor() -> None:
    """Check broker availability and disclose fixture versus live provider mode."""
    with client() as connection:
        response = connection.get("/admin/readiness")
        response.raise_for_status()
        readiness = response.json()
    target = readiness["target"]
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="bold")
    table.add_row("Broker", "ready")
    table.add_row("Provider", str(readiness["provider_mode"]))
    table.add_row("Target", str(target["target_id"]))
    table.add_row("Protocol", str(target["protocol"]))
    table.add_row("Capabilities", ", ".join(target["capabilities"]))
    console.print(Panel(table, title="[bold green]Linux OnCall readiness[/]", border_style="green"))


@app.command()
def investigate(
    symptom: str = "Investigate the target CPU activity and cite evidence.",
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show technical progress and evidence metadata.")
    ] = False,
) -> None:
    """Start the broker investigation and run the real DSH runtime in its container."""
    with client() as connection:
        response = connection.post("/admin/start")
        response.raise_for_status()
        run = response.json()["investigation_id"]
        execute_investigation(connection, run, symptom, symptom, verbose=verbose)


@app.command("continue")
def continue_investigation(
    run_id: str,
    message: Annotated[str, typer.Option("--message", "-m")],
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show technical progress and evidence metadata.")
    ] = False,
) -> None:
    """Create an audited child run using prior evidence as historical context."""
    with client() as connection:
        response = connection.post(f"/admin/runs/{run_id}/continue")
        response.raise_for_status()
        run = response.json()["investigation_id"]
        prompt = continuation_prompt(run_id, message)
        execute_investigation(
            connection,
            run,
            prompt,
            message,
            parent_id=run_id,
            verbose=verbose,
        )


@app.command()
def status(run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Show the current investigation or one stored run by ID."""
    with client() as connection:
        path = f"/admin/runs/{run_id}" if run_id else "/admin/state"
        typer.echo(json.dumps(connection.get(path).raise_for_status().json(), indent=2))


@app.command()
def cancel() -> None:
    """Revoke broker authority; target observations have independent deadlines."""
    with client() as connection:
        connection.post("/admin/cancel").raise_for_status()
    typer.echo("Investigation cancelled")


@app.command()
def close(run_id: str) -> None:
    """Close an active run by ID; accepted reports remain immutable."""
    with client() as connection:
        state = connection.post(f"/admin/runs/{run_id}/close").raise_for_status().json()
    typer.echo(f"Investigation {run_id}: {state['status']}")


@app.command("lab-start")
def lab_start(
    scenario: str,
    inventory: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    ttl_seconds: int = 120,
) -> None:
    """Inject one leased fault on an explicitly disposable AWS target."""
    if scenario not in SCENARIOS:
        raise typer.BadParameter(f"Scenario must be one of: {', '.join(SCENARIOS)}")
    lease = fault_controller(inventory).start(scenario, ttl_seconds)
    typer.echo(json.dumps(asdict(lease), indent=2))


@app.command("lab-stop")
def lab_stop(
    inventory: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """Stop the exact leased workload and verify that the target is clean."""
    fault_controller(inventory).stop()
    typer.echo("Fault cleanup verified")


@app.command()
def report(run_id: str, output: Path | None = None) -> None:
    with client() as connection:
        state = connection.get(f"/admin/runs/{run_id}").raise_for_status().json()
    text = render(state)
    if output:
        output.write_text(text)
    else:
        typer.echo(text)
