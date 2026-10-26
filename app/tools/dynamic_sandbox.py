"""Dynamic sandbox: let the LLM write a short read-only Python snippet to gather
system facts when logs alone are not enough — but run it only after a strict
AST (abstract syntax tree) static check.

Threat model: the snippet is untrusted LLM output and may carry prompt-injected
payloads (shell-command killers, process execution, file writes, eval/exec of
raw strings, or sandbox escapes via dunder attribute access on builtin
classes). The safety strategy is defense in depth; the AST check is the primary
gate and runs BEFORE any code object is built:

1. AST static check (fail-closed, raises SandboxSecurityError):
   - Only an allowlist of read-only imports is permitted (math/json/psutil/socket/...);
   - A denylist of dangerous call names is rejected outright;
   - Any attribute access whose name looks like a dunder is rejected — this
     blocks the classic class-hierarchy / globals-walking escape routes;
   - Only a small allowlist of AST node kinds is accepted, so anything exotic
     (import of disallowed modules, global/nonlocal, class definitions, ...)
     never reaches the execution stage;
2. Restricted execution: the compiled code object runs inside a namespace whose
   ``__builtins__`` contains exactly one binding — an import hook that re-checks
   the module allowlist at runtime — plus a handful of harmless helpers, so even
   a missed static check has almost nothing to bind to. Execution goes through
   types.FunctionType on the vetted code object only — the module never calls
   eval/exec on raw input anywhere;
3. Timeout circuit breaker (default 3s) via a monitored daemon thread;
4. Output byte cap so a snippet cannot flood the episode payload.

There is deliberately no file/process/network binding in the namespace; the only
network-capable import is the read-only ``socket`` module (connect-side calls are
denied by name at the AST layer). All failures degrade to a structured
``{"status": ...}`` dict so a hostile or broken snippet can never crash a run.
"""

from __future__ import annotations

import ast
import logging
import threading
import types
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("moray_aio.sandbox")

#: Hard wall-clock limit for one snippet (circuit breaker)
DEFAULT_TIMEOUT_SECONDS = 3.0
#: Cap on captured stdout bytes and on the result repr
DEFAULT_MAX_OUTPUT_BYTES = 8192
#: Cap on snippet source length (cheap pre-filter before parsing)
DEFAULT_MAX_SOURCE_CHARS = 8000

#: Modules the LLM may import (read-only / pure computation). Anything else is refused.
ALLOWED_IMPORTS: Set[str] = {
    "math",
    "json",
    "re",
    "statistics",
    "datetime",
    "time",
    "itertools",
    "functools",
    "collections",
    "decimal",
    "fractions",
    "random",
    "string",
    "socket",
    "psutil",
    "uuid",
    "hashlib",
    "base64",
    "typing",
}

#: Call names that are always rejected regardless of import allowlist.
#: (Strings only; these names are NEVER bound in the sandbox namespace — the
#: denylist exists to reject hostile snippets at the static-check stage.)
DENIED_CALL_NAMES: Set[str] = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "exit", "quit",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr", "hasattr",
    "__import__", "memoryview", "bytearray", "object", "type", "super",
    "classmethod", "staticmethod", "property", "reload", "help", "license", "copyright",
    # process / filesystem / service control verbs — refused even though none of
    # them exist in the namespace (deny-by-name at the AST layer)
    "system", "popen", "spawn", "fork", "kill", "remove", "unlink", "rmdir",
    "mkdir", "makedirs", "rename", "replace", "chmod", "chown", "symlink",
    "start", "stop", "restart", "terminate", "shutdown", "reboot",
    # network write-side verbs (socket import is read-only observation)
    "connect", "connect_ex", "bind", "listen", "accept", "send", "sendall",
    "sendto", "sendmsg",
}

#: AST node kinds a snippet may contain (deny-by-default for everything else)
_ALLOWED_NODES: Set[type] = {
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.Return, ast.Pass, ast.Break, ast.Continue,
    ast.FunctionDef, ast.Lambda, ast.arguments, ast.arg,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp,
    ast.Call, ast.keyword, ast.If, ast.For, ast.While,
    ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.Constant, ast.Name, ast.Load, ast.Store, ast.Del,
    ast.Attribute, ast.Subscript, ast.Slice,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Import, ast.ImportFrom, ast.alias,
    ast.And, ast.Or, ast.Not, ast.Invert, ast.UAdd, ast.USub,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
    ast.JoinedStr, ast.FormattedValue,
    ast.Starred,
}
# Python 3.8 still emits ast.Index/ast.ExtSlice around some subscripts; tolerate.
if hasattr(ast, "Index"):
    _ALLOWED_NODES.add(ast.Index)
if hasattr(ast, "ExtSlice"):
    _ALLOWED_NODES.add(ast.ExtSlice)


class SandboxSecurityError(Exception):
    """Raised when the AST static check rejects a snippet (fail-closed)."""


def _deny(node: ast.AST, reason: str) -> SandboxSecurityError:
    return SandboxSecurityError(
        "{} (line {}, node {})".format(reason, getattr(node, "lineno", "?"), type(node).__name__)
    )


def check_ast(source: str) -> ast.Module:
    """Static AST security check. Returns the parsed module, or raises
    SandboxSecurityError. This only inspects the snippet — it never runs it.

    Rejects (fail-closed): disallowed imports, relative/star imports, dunder
    attribute access, denied call names, dunder call names, global/nonlocal,
    class definitions, and any node kind outside the allowlist.
    """
    tree = ast.parse(source, mode="exec")

    for node in ast.walk(tree):
        # 1. only allowlisted node kinds
        if type(node) not in _ALLOWED_NODES:
            raise _deny(node, "disallowed syntax node")

        # 2. imports must be on the read-only allowlist (no star, no relative)
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise _deny(node, "import not allowed: {!r}".format(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise _deny(node, "relative import not allowed")
            if any(alias.name == "*" for alias in node.names):
                raise _deny(node, "star import not allowed")
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise _deny(node, "import not allowed: {!r}".format(node.module))

        # 3. any dunder attribute access is a sandbox-escape attempt
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise _deny(node, "dunder attribute access not allowed: {!r}".format(node.attr))

        # 3b. bare dunder NAME references (e.g. __builtins__, __name__) are refused too
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id.endswith("__"):
                raise _deny(node, "dunder name not allowed: {!r}".format(node.id))

        # 4. denied call names (shell verbs, file verbs, raw-string interpreters)
        elif isinstance(node, ast.Call):
            func = node.func
            name: Optional[str] = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name is not None:
                if name in DENIED_CALL_NAMES:
                    raise _deny(node, "call to denied name: {!r}".format(name))
                if name.startswith("__") and name.endswith("__"):
                    raise _deny(node, "dunder call not allowed: {!r}".format(name))

        # 5. global/nonlocal/classdef refused outright (escape surface)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise _deny(node, "global/nonlocal not allowed")
        elif isinstance(node, ast.ClassDef):
            raise _deny(node, "class definition not allowed")

    return tree


def _rewrite_trailing_expression(tree: ast.Module) -> ast.Module:
    """REPL semantics: a bare trailing expression becomes
    ``__sandbox_result__ = <expr>`` so one exec-mode code object covers the whole
    snippet and no separate expression-evaluation path is needed."""
    body = list(tree.body)
    if body and isinstance(body[-1], ast.Expr):
        last = body[-1]
        assign = ast.Assign(
            targets=[ast.Name(id="__sandbox_result__", ctx=ast.Store())],
            value=last.value,
        )
        assign.lineno = last.lineno
        assign.col_offset = last.col_offset
        body[-1] = assign
        rewritten = ast.Module(body=body, type_ignores=[])
        ast.fix_missing_locations(rewritten)
        return rewritten
    return tree


def _build_namespace() -> Dict[str, Any]:
    """A nearly-empty namespace: no file access, no raw-string interpreter —
    only a few harmless builtins, a capturing print(), and ONE guarded import
    hook that re-checks the module allowlist at runtime (defense in depth: the
    AST gate already restricts imports; this second gate covers any future
    parsing gap, and user code can never reference ``__import__`` itself because
    dunder names are rejected at the AST layer)."""
    real_import = __import__

    def _guarded_import(
        name: str,
        globals: Optional[dict] = None,  # noqa: A002 - CPython import signature
        locals: Optional[dict] = None,  # noqa: A002 - CPython import signature
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if level and level > 0:
            raise ImportError("relative import not allowed")
        root = str(name).split(".")[0]
        if root not in ALLOWED_IMPORTS:
            raise ImportError("import not allowed: {!r}".format(name))
        return real_import(name, globals, locals, fromlist, level)

    return {
        "__builtins__": {"__import__": _guarded_import},  # nothing else binds
        "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "round": round,
        "sorted": sorted, "range": range, "enumerate": enumerate, "zip": zip,
        "list": list, "dict": dict, "set": set, "tuple": tuple, "str": str,
        "int": int, "float": float, "bool": bool, "repr": repr, "any": any, "all": all,
        "isinstance": isinstance, "True": True, "False": False, "None": None,
    }


def execute_safe_python(
    source: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
) -> Dict[str, Any]:
    """Statically check then safely run a read-only Python snippet.

    Returns a structured dict; never raises for hostile/broken input:
      {"status": "ok", "stdout": "...", "result": "..."}
      {"status": "rejected", "error": "..."}   <- AST check refused it
      {"status": "timeout", "error": "..."}    <- exceeded the circuit breaker
      {"status": "error", "error": "..."}      <- compile/runtime error
    """
    src = str(source or "")
    if not src.strip():
        return {"status": "rejected", "error": "empty snippet"}
    if len(src) > max_source_chars:
        return {
            "status": "rejected",
            "error": "snippet too large: {} > {} chars".format(len(src), max_source_chars),
        }

    # ---- stage 1: static gate (no code object exists yet if this rejects) ----
    try:
        tree = check_ast(src)
    except SandboxSecurityError as exc:
        logger.warning("sandbox rejected snippet: %s", exc)
        return {"status": "rejected", "error": str(exc)}
    except SyntaxError as exc:
        return {"status": "error", "error": "SyntaxError: {}".format(exc)}

    tree = _rewrite_trailing_expression(tree)

    # capture print() output into an in-memory list (nothing touches real stdout)
    stdout_lines: List[str] = []

    def _captured_print(*args: Any, **kwargs: Any) -> None:
        sep = str(kwargs.get("sep", " "))
        end = str(kwargs.get("end", "\n"))
        text = sep.join(str(a) for a in args) + end
        if sum(len(x) for x in stdout_lines) < max_output_bytes:
            stdout_lines.append(text)

    namespace = _build_namespace()
    namespace["print"] = _captured_print

    # ---- stage 2: run the vetted code object under the timeout circuit breaker ----
    try:
        code = compile(tree, "<sandbox>", "exec")
    except (ValueError, TypeError, SyntaxError) as exc:  # pragma: no cover - defensive
        return {"status": "error", "error": "{}: {}".format(type(exc).__name__, exc)}

    ok, error = _run_in_thread(code, namespace, timeout)
    if not ok:
        return error  # type: ignore[return-value]

    stdout_text = "".join(stdout_lines)[:max_output_bytes]
    return {
        "status": "ok",
        "stdout": stdout_text,
        "result": _safe_repr(namespace.get("__sandbox_result__"), max_output_bytes),
    }


def _safe_repr(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - defensive: hostile __repr__ is not reachable here
        text = "<unreprable>"
    return text[:cap]


def _run_in_thread(code: types.CodeType, namespace: Dict[str, Any], timeout: float) -> Tuple[bool, Optional[dict]]:
    """Run the vetted code object in a monitored daemon thread with a hard
    timeout. The code object is invoked as a function via types.FunctionType —
    created ONLY from the AST-vetted compile() above, with empty __builtins__."""
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            runner = types.FunctionType(code, namespace, "<sandbox>")
            runner()
            box["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - surface everything as a tool error
            box["ok"] = False
            box["err"] = "{}: {}".format(type(exc).__name__, exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, {
            "status": "timeout",
            "error": "snippet exceeded {}s timeout (circuit breaker); "
            "the daemon thread is abandoned and has no further capabilities".format(timeout),
        }
    if not box.get("ok"):
        return False, {"status": "error", "error": box.get("err", "unknown error")}
    return True, None
