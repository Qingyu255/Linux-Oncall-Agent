import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/oncall"
LAB_COMPOSITION_ROOTS = {"broker.py", "cli.py"}
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
    for path in SOURCE.glob("*.py"):
        if path.name in LAB_COMPOSITION_ROOTS:
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
