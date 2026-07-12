"""Contract tests locking two import-boundary invariants that today hold
only by discipline, not by enforcement:

1. HA-free planner core — the pure-Python planning modules (and the
   vendored forecast_core copy shipped with the addon) must never import
   ``homeassistant``. They run outside HA (in the addon container, in
   backtests, in unit tests without the HA test harness).
2. Leaf isolation — the lowest-level modules must not import upward into
   ``controller``/``coordinator`` (or the package itself, whose
   ``__init__.py`` imports ``controller`` and pulls in HomeAssistant),
   which would create a cycle risk and defeat their role as leaves.

Both checks parse source with ``ast`` and walk the *entire* tree (so
imports nested in functions, classes, or try/except blocks are caught
too). They deliberately do NOT import the modules under test — importing
most of them transitively pulls in Home Assistant via
``custom_components/anker_x1_smartgrid/__init__.py``, which is exactly
what this file exists to prevent tests from needing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CUSTOM_COMPONENT_DIR = REPO_ROOT / "custom_components" / "anker_x1_smartgrid"
FORECAST_CORE_DIR = REPO_ROOT / "addon" / "anker_x1_forecast" / "forecast_core"
PACKAGE_DOTTED = "custom_components.anker_x1_smartgrid"

# Invariant 1: modules (by stem, under custom_components/anker_x1_smartgrid/)
# that must never import homeassistant, directly or transitively-in-source.
# NOTE: Task C2 will add 'decision' to this list once it exists.
HA_FREE_MODULES = [
    "optimize",
    "regret",
    "models",
    "resolution",
    "efficiency",
    "export_filter",
    "energy",
]

# Invariant 2: leaf modules that must not import upward into controller,
# coordinator, or the package itself (whose __init__.py imports controller).
# NOTE: Task C2 will add 'decision' to this list once it exists.
LEAF_MODULES = [
    "const",
    "models",
    "resolution",
]
FORBIDDEN_LEAF_TARGETS = {"controller", "coordinator"}


def _ha_free_targets():
    """(id, path) pairs for invariant 1: named custom_components modules
    plus every .py file vendored under addon/anker_x1_forecast/forecast_core/.
    """
    targets = []
    for name in HA_FREE_MODULES:
        path = CUSTOM_COMPONENT_DIR / f"{name}.py"
        assert path.is_file(), f"expected module not found: {path}"
        targets.append((f"custom_components/{name}.py", path))

    forecast_core_files = sorted(FORECAST_CORE_DIR.glob("*.py"))
    assert forecast_core_files, f"no .py files found under {FORECAST_CORE_DIR}"
    for path in forecast_core_files:
        targets.append((f"forecast_core/{path.name}", path))

    return targets


def _leaf_targets():
    targets = []
    for name in LEAF_MODULES:
        path = CUSTOM_COMPONENT_DIR / f"{name}.py"
        assert path.is_file(), f"expected module not found: {path}"
        targets.append((f"custom_components/{name}.py", path))
    return targets


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _find_homeassistant_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (lineno, rendered-import) for every Import/ImportFrom node
    anywhere in the tree (including nested in functions/try-except) whose
    target module is `homeassistant` or a submodule of it.
    """
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "homeassistant" or alias.name.startswith("homeassistant."):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (module == "homeassistant" or module.startswith("homeassistant.")):
                names = ", ".join(alias.name for alias in node.names)
                violations.append((node.lineno, f"from {module} import {names}"))
    return violations


def _find_leaf_violations(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (lineno, rendered-import) for every Import/ImportFrom node
    anywhere in the tree that reaches controller/coordinator, or the
    package itself (custom_components.anker_x1_smartgrid), either via a
    relative import or the fully-qualified absolute path.
    """
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == PACKAGE_DOTTED:
                    violations.append((node.lineno, f"import {name}"))
                elif name.startswith(PACKAGE_DOTTED + "."):
                    tail = name[len(PACKAGE_DOTTED) + 1 :].split(".")[0]
                    if tail in FORBIDDEN_LEAF_TARGETS:
                        violations.append((node.lineno, f"import {name}"))
        elif isinstance(node, ast.ImportFrom):
            names = ", ".join(alias.name for alias in node.names)
            if node.level > 0:
                # Relative import: `from . import X` (module is None) or
                # `from .X import Y` (module is "X").
                if node.module is None:
                    for alias in node.names:
                        if alias.name in FORBIDDEN_LEAF_TARGETS:
                            dots = "." * node.level
                            violations.append((node.lineno, f"from {dots} import {alias.name}"))
                else:
                    top = node.module.split(".")[0]
                    if top in FORBIDDEN_LEAF_TARGETS:
                        dots = "." * node.level
                        violations.append((node.lineno, f"from {dots}{node.module} import {names}"))
            else:
                module = node.module or ""
                if module == PACKAGE_DOTTED:
                    for alias in node.names:
                        if alias.name in FORBIDDEN_LEAF_TARGETS:
                            violations.append((node.lineno, f"from {module} import {alias.name}"))
                elif module.startswith(PACKAGE_DOTTED + "."):
                    tail = module[len(PACKAGE_DOTTED) + 1 :].split(".")[0]
                    if tail in FORBIDDEN_LEAF_TARGETS:
                        violations.append((node.lineno, f"from {module} import {names}"))
    return violations


@pytest.mark.parametrize(
    "label, path", _ha_free_targets(), ids=[label for label, _ in _ha_free_targets()]
)
def test_planner_core_is_homeassistant_free(label, path):
    """Pure planner-core modules must never import homeassistant, at any
    nesting depth (module scope, functions, try/except, etc.).
    """
    tree = _parse(path)
    violations = _find_homeassistant_imports(tree)
    assert not violations, (
        f"{label} imports homeassistant, breaking HA-free planner core "
        f"invariant:\n"
        + "\n".join(f"  line {lineno}: {stmt}" for lineno, stmt in violations)
    )


@pytest.mark.parametrize(
    "label, path", _leaf_targets(), ids=[label for label, _ in _leaf_targets()]
)
def test_leaf_modules_do_not_import_upward(label, path):
    """const/models/resolution are leaves: they must not import
    controller, coordinator, or the package itself (whose __init__.py
    imports controller and therefore homeassistant).
    """
    tree = _parse(path)
    violations = _find_leaf_violations(tree)
    assert not violations, (
        f"{label} imports upward into controller/coordinator/package, "
        f"breaking leaf isolation invariant:\n"
        + "\n".join(f"  line {lineno}: {stmt}" for lineno, stmt in violations)
    )
