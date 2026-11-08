"""Import-scan lint: modules stay isolated, core stays module-free.

A module may import only its own package, ``housesitter.sdk``, and the
stdlib. The core (``core/**``, ``sdk.py``, ``__init__.py``,
``__main__.py``) must never import ``housesitter.modules.<anything>``;
importing the ``housesitter.modules`` package itself is allowed because
the registry iterates it. Beyond imports, the scan flags uses that reach
a module another way: ``__builtins__``, ``sys.modules``, ``__import__``
as an attribute, ``os`` process functions, ``pty``/``multiprocessing``
process control, ``asyncio`` subprocess spawners, and calls to the bare
name ``open`` or ``io.open``. It is a guard rail that review backs up,
not a sandbox. The scan helpers live here so the mutation tests can
reuse them on synthetic files.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import sysconfig
from pathlib import Path

import harness
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src" / "housesitter"

# Banned even though some are stdlib: raw sockets, TLS, URL handling,
# subprocesses, FFI, process control, and dynamic imports would bypass the
# allow-list. ``concurrent.futures.process`` is banned by dotted path below:
# plain ``concurrent.futures`` (thread pools) stays allowed.
_BANNED_TOP = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "requests",
        "subprocess",
        "ctypes",
        "importlib",
        "builtins",
        "pty",
        "multiprocessing",
    }
)

_OS_PROCESS_ATTRS = frozenset({"system", "popen"})
_ASYNCIO_SUBPROCESS = frozenset(
    {"create_subprocess_exec", "create_subprocess_shell"}
)


def _stdlib_names() -> set | None:
    names = getattr(sys, "stdlib_module_names", None)
    return set(names) if names is not None else None


def _is_stdlib(top: str) -> bool:
    """True when ``top`` is a standard-library top-level module."""
    known = _stdlib_names()
    if known is not None:
        return top in known
    # Python 3.9 fallback: builtin/frozen, or under the stdlib dir but
    # not under site-packages/dist-packages.
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError, AttributeError):
        return False
    if spec is None or spec.origin is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    try:
        stdlib = sysconfig.get_paths()["stdlib"]
    except Exception:
        return False
    origin = os.path.realpath(spec.origin)
    stdlib = os.path.realpath(stdlib)
    if not origin.startswith(stdlib + os.sep):
        return False
    parts = origin.split(os.sep)
    return "site-packages" not in parts and "dist-packages" not in parts


def _check_absolute(dotted: str, package: str) -> str | None:
    """Problem text for ``import a.b.c``, or None when allowed."""
    top = dotted.split(".")[0]
    if dotted == "housesitter.sdk" or dotted.startswith("housesitter.sdk."):
        return None
    if dotted == package or dotted.startswith(package + "."):
        return None
    if dotted == "concurrent.futures.process" or dotted.startswith(
        "concurrent.futures.process."
    ):
        return f"must not import {dotted}"
    if top == "housesitter":
        return f"must not import {dotted}"
    if top == "http":
        # The whole package: with the core loaded, ``import http`` alone
        # exposes ``http.client`` as an attribute (review finding 1).
        return f"must not import {dotted}"
    if top in _BANNED_TOP:
        return f"must not import {top}"
    if _is_stdlib(top):
        return None
    return f"must not import non-stdlib {dotted}"


def _check_from(module: str, names: list, package: str) -> str | None:
    """Problem text for ``from m import a, b``, or None when allowed."""
    top = module.split(".")[0] if module else ""
    if module in ("housesitter.sdk",) or module.startswith("housesitter.sdk."):
        return None
    if module == "housesitter":
        if set(names) == {"sdk"}:
            return None
        return f"must not import {sorted(names)} from housesitter"
    if module == package or module.startswith(package + "."):
        return None
    if module == "concurrent.futures.process" or (
        module == "concurrent.futures" and "process" in names
    ):
        return f"must not import from {module}"
    if top == "housesitter":
        return f"must not import from {module}"
    if top == "http":
        return f"must not import from {module}"
    if top in _BANNED_TOP:
        return f"must not import from {top}"
    if not module:
        return None
    if _is_stdlib(top):
        return None
    return f"must not import non-stdlib {module}"


def _check_attribute(node: ast.Attribute) -> str | None:
    """Problem text for ``base.attr``, or None when allowed.

    Catches indirect routes that no import scan can see as imports:
    ``sys.modules``, ``__import__`` as an attribute, ``os`` process
    functions, ``asyncio`` subprocess spawners, and ``io.open``.
    """
    if not isinstance(node.value, ast.Name):
        return None
    base, attr = node.value.id, node.attr
    if attr == "__import__":
        return "must not use __import__"
    if base == "sys" and attr == "modules":
        return "must not use sys.modules"
    if base == "os" and (
        attr in _OS_PROCESS_ATTRS
        or attr.startswith(("spawn", "exec", "fork", "posix_spawn"))
    ):
        return f"must not use os.{attr}"
    if base == "asyncio" and attr in _ASYNCIO_SUBPROCESS:
        return f"must not use asyncio.{attr}"
    if base == "io" and attr == "open":
        return "must not use io.open"
    return None


def scan_module_file(path: Path, package: str) -> list:
    """Import problems in one module file against its package name."""
    problems = []
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot parse: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                problem = _check_absolute(alias.name, package)
                if problem is not None:
                    problems.append(f"{path}:{node.lineno}: {problem}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            problem = _check_from(
                node.module or "", [alias.name for alias in node.names], package
            )
            if problem is not None:
                problems.append(f"{path}:{node.lineno}: {problem}")
        elif isinstance(node, ast.Name) and node.id in ("__import__", "__builtins__"):
            problems.append(f"{path}:{node.lineno}: must not use {node.id}")
        elif isinstance(node, ast.Attribute):
            problem = _check_attribute(node)
            if problem is not None:
                problems.append(f"{path}:{node.lineno}: {problem}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                # Conservative by design: even a local binding of the
                # name ``open`` is flagged. Modules must not bind or
                # call a name ``open``; file I/O goes through
                # ``ctx.store`` and ``ctx.secret``.
                problems.append(
                    f"{path}:{node.lineno}: must not bind or call "
                    "the name 'open'"
                )
    return problems


def scan_core_file(path: Path) -> list:
    """Import problems in one core file: no ``housesitter.modules.<x>``."""
    problems = []
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot parse: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                if dotted.startswith("housesitter.modules."):
                    problems.append(
                        f"{path}:{node.lineno}: core must not import {dotted}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") == "housesitter.modules":
                problems.append(
                    f"{path}:{node.lineno}: core must not import from "
                    "housesitter.modules"
                )
            elif (node.module or "").startswith("housesitter.modules."):
                problems.append(
                    f"{path}:{node.lineno}: core must not import from "
                    f"{node.module}"
                )
    return problems


def module_files(module_cls: type) -> list:
    """Every ``.py`` file in a module's package directory."""
    module = importlib_module(module_cls)
    base = Path(module.__file__).resolve().parent
    return sorted(path for path in base.rglob("*.py") if path.is_file())


def importlib_module(module_cls: type):
    """Import the module holding a module class."""
    import importlib

    return importlib.import_module(module_cls.__module__)


def core_files() -> list:
    """Every core file the lint covers."""
    roots = [SRC_ROOT / name for name in ("sdk.py", "__init__.py", "__main__.py")]
    core_dir = SRC_ROOT / "core"
    found = [path for path in roots if path.is_file()]
    found.extend(sorted(path for path in core_dir.glob("*.py") if path.is_file()))
    return found


class TestModuleImports:
    @pytest.mark.parametrize(
        "module_id", [cls.id for cls, _ in harness.modules_under_test()]
    )
    def test_module_imports_are_isolated(self, module_id):
        cls = next(cls for cls, _ in harness.modules_under_test() if cls.id == module_id)
        files = module_files(cls)
        assert files, f"{module_id} has no scanned files; the lint is vacuous"
        problems = []
        for path in files:
            problems.extend(scan_module_file(path, cls.__module__))
        assert problems == []

    def test_scan_finds_module_files(self):
        total = sum(
            len(module_files(cls)) for cls, _ in harness.modules_under_test()
        )
        assert total > 0


class TestCoreImports:
    def test_core_never_imports_modules(self):
        files = core_files()
        assert files, "no core files found; the lint is vacuous"
        problems = []
        for path in files:
            problems.extend(scan_core_file(path))
        assert problems == []
