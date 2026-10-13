"""The run loop read as one function, for the tests that guard its structure.

``run_locked`` is split into phase functions that share a ``RunFrame``: setup
steps, the steps of one loop pass, the crash handler and the final settlement.
Several tests assert where inside the loop something happens -- one arming
site, a call after the loop, a guard before a stop. Those claims are about the
sequence, not about which module holds a step, so this view reassembles the
phases back into the single ``run_locked`` the claims were written against:
frame attributes become plain names again, a ``LoopSignal`` return becomes the
``continue`` or ``break`` it stands for, and the helpers and classes of every
``run_loop_*`` module are gathered into one module tree.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

from agent_session_loop import run_loop

PACKAGE = Path(run_loop.__file__ or "").parent
STEP_TUPLES = ("_SETUP_STEPS", "_PASS_STEPS")
DRIVER_NAMES = {"run_locked", "_run_loop_pass", *STEP_TUPLES}
CRASH_HANDLER = "_handle_run_crash"
FINALIZER = "_finalize_run"


class _Unframe(ast.NodeTransformer):
    """Turn ``frame.name`` back into ``name`` and signals into loop jumps."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "frame":
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "LoopSignal"
        ):
            jump = ast.Continue() if value.attr == "CONTINUE" else ast.Break()
            return ast.copy_location(jump, node)
        return self.generic_visit(node)


def _definitions() -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for path in sorted(PACKAGE.glob("run_loop*.py")):
        if path.name == "run_loop_frame.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            name = getattr(node, "name", None)
            if name is None and isinstance(node, ast.Assign):
                target = node.targets[0]
                name = target.id if isinstance(target, ast.Name) else None
            if name is not None:
                found[name] = node
    return found


def _step_body(node: ast.AST) -> list[ast.stmt]:
    assert isinstance(node, ast.FunctionDef)
    body = [_Unframe().visit(statement) for statement in node.body[1:]]
    last = body[-1]
    if (
        isinstance(last, ast.Return)
        and isinstance(last.value, ast.Constant)
        and last.value.value is None
    ):
        body.pop()
    return body


def _step_names(rump: ast.Module, tuple_name: str) -> list[str]:
    for node in rump.body:
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == tuple_name:
            assert isinstance(node.value, ast.Tuple)
            return [ast.unparse(item) for item in node.value.elts]
    raise AssertionError(f"{tuple_name} not found in run_loop")


@functools.cache
def assembled_module() -> ast.Module:
    """One module holding every run-loop helper and a reassembled run_locked."""
    definitions = _definitions()
    rump = ast.parse(Path(run_loop.__file__ or "").read_text(encoding="utf-8"))
    run_locked = next(
        node
        for node in rump.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_locked"
    )
    steps = _step_names(rump, "_SETUP_STEPS")
    passes = _step_names(rump, "_PASS_STEPS")
    handler = definitions[CRASH_HANDLER]
    assert isinstance(handler, ast.FunctionDef)
    loop = ast.While(
        test=ast.Constant(value=True),
        body=[s for name in passes for s in _step_body(definitions[name])],
        orelse=[],
    )
    crash = ast.ExceptHandler(
        type=ast.Name(id="BaseException", ctx=ast.Load()),
        name="error",
        body=_step_body(handler),
    )
    guarded = ast.Try(body=[loop], handlers=[crash], orelse=[], finalbody=[])
    rebuilt = ast.FunctionDef(
        name="run_locked",
        args=run_locked.args,
        body=(
            [s for name in steps for s in _step_body(definitions[name])]
            + [guarded]
            + _step_body(definitions[FINALIZER])
        ),
        decorator_list=[],
        returns=run_locked.returns,
        type_params=[],
    )
    module_body: list[ast.stmt] = []
    for name, node in definitions.items():
        if name in DRIVER_NAMES:
            continue
        if name in steps or name in passes or name in (CRASH_HANDLER, FINALIZER):
            continue
        assert isinstance(node, ast.stmt)
        module_body.append(node)
    module_body.append(rebuilt)
    module = ast.Module(body=module_body, type_ignores=[])
    return ast.fix_missing_locations(module)


@functools.cache
def assembled_source() -> str:
    """The reassembled run loop as source text."""
    return ast.unparse(assembled_module())


@functools.cache
def run_locked_source() -> str:
    """Just the reassembled ``run_locked`` as source text."""
    for node in assembled_module().body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_locked":
            return ast.unparse(node)
    raise AssertionError("run_locked missing from the assembled view")
