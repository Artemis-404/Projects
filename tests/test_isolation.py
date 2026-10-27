"""Persona code cannot see real traffic (FR-15).

The SRS requires a test that fails if ``persona/`` (and the clearnet learner in
``profile/``, which produces personas) imports the transport, the TLS tunnel or
the scheduler's queues. Two checks:

* static: every ``import``/``from`` in the source, including ones inside
  functions and relative imports, against a deny list;
* dynamic: import every module in a fresh interpreter and look at
  ``sys.modules``, which also catches *transitive* imports.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ISOLATED = ("persona", "profile")
# Traffic state lives here: the PT and its multiplexer, the TLS tunnel, records
# and scheduler queues, frames, the fail-closed guard, the live engine, the lab.
FORBIDDEN = ("pt", "tunnel", "cover", "protocol", "security", "app_api.engine", "lab")


def _is_forbidden(module: str) -> bool:
    return any(module == bad or module.startswith(bad + ".") for bad in FORBIDDEN)


def imported_modules(path: Path, package: str) -> set[str]:
    """Every module a source file imports, with relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                anchor = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def violations(package_dir: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(package_dir.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        for module in sorted(imported_modules(path, package)):
            if _is_forbidden(module):
                problems.append(f"{path.relative_to(package_dir.parent)} imports {module}")
    return problems


def isolated_modules() -> list[str]:
    modules: list[str] = []
    for package in ISOLATED:
        for path in sorted((ROOT / package).rglob("*.py")):
            parts = path.relative_to(ROOT).with_suffix("").parts
            modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return modules


@pytest.mark.parametrize("package", ISOLATED)
def test_source_never_imports_traffic_code(package: str) -> None:
    assert violations(ROOT / package) == []


def test_importing_persona_code_loads_no_traffic_module() -> None:
    script = (
        "import importlib, json, sys\n"
        f"for name in {isolated_modules()!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert [name for name in loaded if _is_forbidden(name)] == []


def test_the_check_catches_a_violation(tmp_path: Path) -> None:
    package = tmp_path / "persona"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "ok.py").write_text("import json\nfrom persona import model\n", encoding="utf-8")
    (package / "sub" / "bad.py").write_text(
        "def peek():\n    from pt.mux import MuxSession\n    return MuxSession\n",
        encoding="utf-8",
    )
    (package / "rel.py").write_text("from ..cover import scheduler\n", encoding="utf-8")
    found = violations(package)
    assert any("bad.py imports pt.mux" in item for item in found)
    assert any("rel.py imports cover" in item for item in found)
    assert not any("ok.py" in item for item in found)
