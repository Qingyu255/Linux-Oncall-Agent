"""Operator CLI. Docker authority is here, never exposed as an agent tool."""

import json
import selectors
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from oncall.faults import SCENARIOS, FaultController, SsmOperatorExecutor
from oncall.harness_progress import PROGRESS_PROTOCOL, progress_message

app = typer.Typer(help="Linux OnCall Agent local lab")
ROOT = Path(__file__).resolve().parents[2]
console = Console()


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


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
    state: dict[str, Any], report_path: Path, json_path: Path, elapsed_seconds: float
) -> None:
    """Render a compact terminal result while Markdown retains complete evidence."""
    report = state.get("report")
    if not isinstance(report, dict):
        raise ValueError("Completed investigation has no report")

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

    console.print("\n[bold]Evidence-backed findings[/]")
    for index, claim in enumerate(report["claims"], start=1):
        references = ", ".join(str(item)[:8] for item in claim["evidence_ids"])
        console.print(f"  [bold cyan]{index}.[/] {escape(str(claim['text']))}")
        scope = str(claim.get("evidence_scope", "current"))
        console.print(f"     [dim]Evidence ({scope}): {references}[/]")

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

    console.print(f"[bold]Markdown report:[/] {display_path(report_path)}")
    console.print(f"[bold]JSON report:[/]     {display_path(json_path)}")


def save_state(state: dict[str, Any], filename: str = "report.json") -> Path:
    run_id = state.get("investigation_id")
    if not isinstance(run_id, str):
        raise ValueError("Investigation state is missing its run ID")
    output = ROOT / ".local/reports" / run_id
    output.mkdir(parents=True, exist_ok=True)
    path = output / filename
    path.write_text(json.dumps(state, indent=2) + "\n")
    return path


def show_progress(event: dict[str, Any], started: float) -> None:
    """Render one safe event produced by the sandbox progress adapter."""
    rendered = progress_message(event)
    if rendered is None:
        return
    style, symbol, message = rendered
    elapsed = time.monotonic() - started
    console.print(f"[dim]{elapsed:6.1f}s[/] [{style}]{symbol}[/] {escape(message)}")


def run_harness(run: str, symptom: str, started: float, timeout: float = 175) -> int:
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
                    handle_harness_line(line, started)
        for line in process.stdout:
            handle_harness_line(line, started)
        return process.wait()
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


def handle_harness_line(line: str, started: float) -> None:
    """Accept only the explicit progress protocol; discard runtime diagnostics."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(event, dict) and event.get("protocol") == PROGRESS_PROTOCOL:
        show_progress(event, started)


def execute_investigation(
    connection: httpx.Client,
    run: str,
    prompt: str,
    display_message: str,
    parent_id: str | None = None,
) -> None:
    heading = f"[bold]Run[/] {run}"
    if parent_id is not None:
        heading += f"\n[bold]Parent[/] {parent_id}"
    console.print(Panel.fit(f"{heading}\n[dim]{escape(display_message)}[/]", title="Linux OnCall"))
    started = time.monotonic()
    try:
        console.print("\n[bold]Investigation progress[/]")
        return_code = run_harness(run, prompt, started)
        if return_code:
            raise RuntimeError(f"Harness exited with {return_code}")
        state = connection.get("/admin/state").raise_for_status().json()
        if state["status"] == "running":
            raise RuntimeError("Harness returned without an accepted report")
        json_path = save_state(state)
        report_path = json_path.with_name("report.md")
        report_path.write_text(render(state))
        show_result(state, report_path, json_path, time.monotonic() - started)
    except BaseException as error:
        connection.post("/admin/cancel")
        state = connection.get("/admin/state").raise_for_status().json()
        failure_path = save_state(state, "failed-state.json")
        console.print(
            Panel(
                f"{escape(str(error))}{failure_detail(state)}\n\n"
                f"Audit state: {display_path(failure_path)}",
                title="[bold red]Investigation failed[/]",
                border_style="red",
            )
        )
        raise typer.Exit(1) from error


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
) -> None:
    """Start the broker investigation and run the real DSH runtime in its container."""
    with client() as connection:
        response = connection.post("/admin/start")
        response.raise_for_status()
        run = response.json()["investigation_id"]
        execute_investigation(connection, run, symptom, symptom)


@app.command("continue")
def continue_investigation(
    run_id: str,
    message: Annotated[str, typer.Option("--message", "-m")],
) -> None:
    """Create an audited child run using prior evidence as historical context."""
    with client() as connection:
        response = connection.post(f"/admin/runs/{run_id}/continue")
        response.raise_for_status()
        run = response.json()["investigation_id"]
        prompt = (
            f"This is an explicit follow-up to investigation {run_id}. "
            "First call get_investigation_state. Prior-run evidence is historical. "
            "Use evidence_scope='historical' only for retrospective claims. "
            "For any claim about current target conditions, collect fresh evidence in this child "
            "investigation and use evidence_scope='current'. "
            f"Operator follow-up: {message}"
        )
        execute_investigation(connection, run, prompt, message, parent_id=run_id)


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
