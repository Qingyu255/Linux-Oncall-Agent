import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/oncall"
LAB_COMPOSITION_ROOTS = {"broker/app.py", "cli.py"}
LAB_ALLOWED_CORE_IMPORTS = {"oncall.aws_ssm", "oncall.domain"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_diagnostic_modules_do_not_depend_on_lab_implementation():
    for path in SOURCE.rglob("*.py"):
        relative = path.relative_to(SOURCE).as_posix()
        if relative.startswith("lab/") or relative in LAB_COMPOSITION_ROOTS:
            continue
        assert not any(name.startswith("oncall.lab") for name in imported_modules(path)), path


def test_lab_support_depends_only_on_stable_core_contracts():
    for path in (SOURCE / "lab").glob("*.py"):
        core_imports = {
            name
            for name in imported_modules(path)
            if name.startswith("oncall.") and not name.startswith("oncall.lab")
        }
        assert core_imports <= LAB_ALLOWED_CORE_IMPORTS, (path, core_imports)


def test_target_package_does_not_depend_on_broker_or_harness():
    for path in (SOURCE / "target").glob("*.py"):
        imports = imported_modules(path)
        assert not any(name.startswith(("oncall.broker", "oncall.harness")) for name in imports), (
            path
        )


def test_broker_package_does_not_depend_on_target_or_harness_implementation():
    for path in (SOURCE / "broker").glob("*.py"):
        imports = imported_modules(path)
        assert not any(name.startswith(("oncall.target", "oncall.harness")) for name in imports), (
            path
        )


def test_harness_package_does_not_depend_on_broker_or_target_implementation():
    for path in (SOURCE / "harness").glob("*.py"):
        imports = imported_modules(path)
        assert not any(name.startswith(("oncall.broker", "oncall.target")) for name in imports), (
            path
        )
