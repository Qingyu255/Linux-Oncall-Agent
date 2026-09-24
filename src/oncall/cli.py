"""Operator CLI. Docker authority is here, never exposed as an agent tool."""

import json
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


def render(state: dict[str, Any]) -> str:
    lines = [
        "# Linux OnCall investigation",
        "",
        f"Run: `{state['investigation_id']}`",
        f"Status: **{state['status']}**",
        f"Provider mode: **{state['mode']}**",
        "",
    ]
    report = state.get("report")
    if report:
        lines += [report["summary"], "", "## Findings", ""]
        for claim in report["claims"]:
            refs = ", ".join(f"`{ref}`" for ref in claim["evidence_ids"])
            lines.append(f"- {claim['text']} Evidence: {refs}")
        for field in ("alternatives", "limitations", "next_steps"):
            lines += ["", f"## {field.replace('_', ' ').title()}", ""]
            lines += [f"- {item}" for item in report[field]]
    lines += ["", "## Captured evidence", ""]
    for evidence in state["evidence"]:
        lines += [
            f"### {evidence['evidence_id']}",
            "",
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
        console.print(f"     [dim]Evidence: {references}[/]")

    evidence_table = Table(title="Captured evidence", header_style="bold magenta")
    evidence_table.add_column("Probe")
    evidence_table.add_column("Quality")
    evidence_table.add_column("Bytes", justify="right")
    evidence_table.add_column("Evidence ID")
    for evidence in state["evidence"]:
        evidence_table.add_row(
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
        console.print(
            Panel.fit(f"[bold]Run[/] {run}\n[dim]{escape(symptom)}[/]", title="Linux OnCall")
        )
        started = time.monotonic()
        try:
            with console.status(
                "[bold cyan]Agent is collecting bounded evidence and testing hypotheses…[/]",
                spinner="dots",
            ):
                result = subprocess.run(
                    [
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
                    ],
                    cwd=ROOT,
                    timeout=175,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            if result.returncode:
                raise RuntimeError(f"Harness exited with {result.returncode}")
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
def status() -> None:
    with client() as connection:
        typer.echo(json.dumps(connection.get("/admin/state").raise_for_status().json(), indent=2))


@app.command()
def cancel() -> None:
    """Revoke broker authority; target observations have independent deadlines."""
    with client() as connection:
        connection.post("/admin/cancel").raise_for_status()
    typer.echo("Investigation cancelled")


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
