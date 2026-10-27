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
   - A denylist of dangerous call names is rejected outright — at CALL time and
     at REFERENCE time (``k = p.kill; k()`` must not launder a denied verb
     through an alias);
   - Any attribute access whose name looks like a dunder is rejected — this
     blocks the classic class-hierarchy / globals-walking escape routes — and
     so are interpreter-internal attributes (``gi_frame``/``f_back``/
     ``f_builtins``/``co_consts``/... : the frame-escape ladder);
   - Call sites whose target cannot be statically resolved (subscripted
     callables like ``d["exec"](...)``) are rejected rather than trusted, and
     str.format/format_map are refused (their mini-language walks arbitrary
     attribute chains past the AST);
   - Decorators and class definitions are refused; only a small allowlist of
     AST node kinds is accepted, so anything exotic never reaches execution;
   - Literal arithmetic that constant-folds to >= 10**10 is refused: one C-level
     bigint/string op that size holds the GIL long enough to defeat any
     in-process timeout;
2. Process-pool isolation (v3.0): the code object is built and executed ONLY
   inside a dedicated worker PROCESS from a spawn-context multiprocessing
   pool. The worker re-runs the AST gate itself (defense in depth), runs the
   vetted code through types.FunctionType in a namespace whose ``__builtins__``
   contains exactly one binding — a runtime import re-check — and returns a
   JSON-serialized result string. Nothing of the host interpreter's state is
   importable from the snippet;
3. OS-level timeout breaker: an overdue worker is terminate()d (SIGTERM /
   TerminateProcess) and replaced — a physical kill that stops C-level
   GIL-holding computations no thread-based breaker could ever interrupt;
   IPC carries JSON-only payloads so hostile results cannot smuggle objects;
4. Output byte cap enforced worker-side, so a snippet cannot flood the episode
   payload or the IPC channel.

There is deliberately no file/process/network binding in the namespace; the only
network-capable import is the read-only ``socket`` module — connect-side verbs
and DNS resolvers are refused by name at the AST layer AND the import hook hands
back a facade exposing only constants, pure converters and local-host lookups.
All failures degrade to a structured ``{"status": ...}`` dict so a hostile or
broken snippet can never crash a run.
"""

from __future__ import annotations

import ast
import atexit
import json
import logging
import multiprocessing
import queue
import threading
import time
import types
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("moray_aio.sandbox")

#: Hard wall-clock limit for one snippet (process-pool breaker)
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
    # DNS / outbound dialers: these bypass the connect-verb denylist by doing
    # connect + (optionally) I/O inside one call, and DNS queries double as a
    # data-exfiltration channel — refused at reference AND call time
    "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
    "gethostbyaddr", "getnameinfo", "getfqdn", "fromfd", "makefile",
    # filesystem / process verbs reachable through non-obvious module surfaces
    "rmtree", "startfile", "execv", "execve", "execvp", "spawnv", "spawnve",
    "killpg", "setuid", "setgid", "seteuid", "setegid", "getstatusoutput",
    "getoutput", "dup", "dup2", "pipe",
    # psutil process-control verbs beyond kill/terminate
    "suspend", "resume", "send_signal", "nice", "ionice",
    # attribute-oriented raw interpreters / traversal helpers (str.format walks
    # arbitrary attribute chains via its own mini-language, bypassing the AST)
    "format", "format_map",
    # no legitimate use inside a bounded read-only snippet; pure budget burn
    "sleep",
    # iter(callable, sentinel) builds an infinite C-level iterator (the fuel for
    # sum()/max() GIL-hog wedges); one-arg iter has no use a for-loop lacks
    "iter",
    # decimal context tuning would unclamp C-level precision work
    "getcontext", "setcontext", "localcontext",
}

#: Attribute-name prefixes that expose interpreter internals (frames, live
#: generators/coroutines, code objects, tracebacks). Walking these is the
#: frame-escape ladder: generator.gi_frame.f_back.f_builtins → real exec.
_DENIED_ATTR_PREFIXES: Tuple[str, ...] = ("f_", "gi_", "cr_", "ag_", "co_", "tb_")
#: Exact attribute names outside the prefixes that must never be referenced.
_DENIED_ATTR_NAMES: Set[str] = {"mro"}

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


#: Constant-fold ceiling. A single C-level bigint/string operation at this
#: magnitude (``10**10**10``, ``'A'*10**11``) holds the GIL for its whole
#: duration, so the timeout circuit breaker's join can never wake up and the
#: whole host process wedges. Pre-baked monster arithmetic is refused instead.
_FOLD_LIMIT = 10 ** 10
#: Exponents beyond this are treated as past the limit without computing them.
_FOLD_POW_MAX_EXP = 64


def _fold_int(node: ast.AST) -> Optional[int]:
    """Best-effort fold of literal int arithmetic through ``**``/``*`` chains.
    Returns the folded value, or None when the expression is not constant."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Pow, ast.Mult)):
        left = _fold_int(node.left)
        right = _fold_int(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Pow):
                if abs(right) > _FOLD_POW_MAX_EXP:
                    return _FOLD_LIMIT  # sentinel: astronomically past the limit
                return left ** right
            return left * right
        except (OverflowError, ValueError, MemoryError):
            return _FOLD_LIMIT
    return None


def _deny(node: ast.AST, reason: str) -> SandboxSecurityError:
    return SandboxSecurityError(
        "{} (line {}, node {})".format(reason, getattr(node, "lineno", "?"), type(node).__name__)
    )


def check_ast(source: str) -> ast.Module:
    """Static AST security check. Returns the parsed module, or raises
    SandboxSecurityError. This only inspects the snippet — it never runs it.

    Rejects (fail-closed): disallowed imports, relative/star imports, dunder
    attribute access, denied names at REFERENCE time (alias-then-call bypasses
    call-time-only checks), interpreter-internal attributes (frame/generator/
    code-object ladders), calls whose target cannot be statically resolved
    (e.g. ``d["exec"](...)`` — subscripted callables sidestep name checks),
    decorators, denied call names, dunder call names, global/nonlocal, class
    definitions, and any node kind outside the allowlist.
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

        # 3. attribute access: dunder = escape attempt; denied name = the
        #    reference itself is refused (binding ``k = p.kill`` then calling
        #    ``k()`` must not launder the denied name through an alias);
        #    interpreter-internal prefixes = frame/generator/code-object ladder
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise _deny(node, "dunder attribute access not allowed: {!r}".format(node.attr))
            if node.attr in DENIED_CALL_NAMES or node.attr in _DENIED_ATTR_NAMES:
                raise _deny(node, "access to denied name not allowed: {!r}".format(node.attr))
            if node.attr.startswith(_DENIED_ATTR_PREFIXES):
                raise _deny(node, "interpreter-internal attribute not allowed: {!r}".format(node.attr))

        # 3b. bare dunder NAME references (e.g. __builtins__, __name__) are refused too
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id.endswith("__"):
                raise _deny(node, "dunder name not allowed: {!r}".format(node.id))

        # 4. denied call names (shell verbs, file verbs, raw-string interpreters).
        #    Fail-closed on unresolvable targets: a Subscript/chain call site
        #    hides the callee's name, so it is rejected rather than trusted.
        elif isinstance(node, ast.Call):
            func = node.func
            name: Optional[str] = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            elif not isinstance(func, ast.Lambda):
                # Lambda call sites are fine: the lambda body is walked too.
                raise _deny(node, "call target not statically resolvable")
            if name is not None:
                if name in DENIED_CALL_NAMES:
                    raise _deny(node, "call to denied name: {!r}".format(name))
                if name.startswith("__") and name.endswith("__"):
                    raise _deny(node, "dunder call not allowed: {!r}".format(name))

        # 5. global/nonlocal/classdef/decorators refused outright (escape surface)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise _deny(node, "global/nonlocal not allowed")
        elif isinstance(node, ast.ClassDef):
            raise _deny(node, "class definition not allowed")
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                raise _deny(node, "decorators not allowed")

        # 6. constant-fold guard: pre-baked monster arithmetic holds the GIL
        #    inside one C call long enough to defeat the timeout circuit breaker
        elif isinstance(node, ast.BinOp):
            folded = _fold_int(node)
            if folded is not None and abs(folded) >= _FOLD_LIMIT:
                raise _deny(node, "suspiciously large constant computation")

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


#: Members the sandbox may reach on the socket module at runtime: address
#: constants, pure converters and local-host name lookup. Everything with an
#: outbound capability (dialers, DNS resolvers, fd constructors) is absent —
#: the runtime facade is the third gate behind the AST reference/call denies.
_SOCKET_SAFE_EXPORTS: Set[str] = {
    "AF_INET", "AF_INET6", "AF_UNIX", "SOCK_STREAM", "SOCK_DGRAM", "SOCK_RAW",
    "SOCK_RDM", "SOCK_SEQPACKET", "SOL_SOCKET", "IPPROTO_TCP", "IPPROTO_UDP",
    "IPPROTO_IP", "SHUT_RD", "SHUT_WR", "SHUT_RDWR", "MSG_PEEK",
    "socket", "gethostname",
    "ntohl", "htonl", "ntohs", "htons",
    "inet_aton", "inet_ntoa", "inet_pton", "inet_ntop",
    "if_nameindex", "if_nametoindex", "if_indextoname",
    "error", "gaierror", "herror", "timeout",
}


#: Members the sandbox may reach on itertools: bounded combinators only. The
#: infinite producers (count/cycle/repeat/tee) are absent because a C-level
#: consumer (sum/max/...) over an infinite C iterator holds the GIL in a loop
#: that never returns to bytecode — wedging the process past the circuit
#: breaker. Bounded combinator chains return to bytecode every step.
_ITERTOOLS_SAFE_EXPORTS: Set[str] = {
    "chain", "combinations", "combinations_with_replacement", "permutations",
    "product", "groupby", "accumulate", "starmap", "filterfalse", "dropwhile",
    "takewhile", "islice", "zip_longest", "compress",
}


def _restrict_socket(module: Any) -> Any:
    """A facade with NO attributes of its own: every lookup goes through
    ``__getattr__`` (unreachable via the sandbox — dunder/attribute probes are
    AST-denied), and only the safe export set resolves."""
    return _restrict_module(module, _SOCKET_SAFE_EXPORTS)


def _restrict_module(module: Any, exports: Set[str]) -> Any:
    class _ModuleFacade(object):
        def __getattr__(self, name: str) -> Any:
            if name in exports:
                return getattr(module, name)
            raise AttributeError(
                "sandbox: this module member is not available: {0!r}".format(name))

    return _ModuleFacade()


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
        module = real_import(name, globals, locals, fromlist, level)
        if root == "socket":
            return _restrict_socket(module)
        if root == "itertools":
            return _restrict_module(module, _ITERTOOLS_SAFE_EXPORTS)
        return module

    return {
        "__builtins__": {"__import__": _guarded_import},  # nothing else binds
        "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "round": round,
        "sorted": sorted, "range": range, "enumerate": enumerate, "zip": zip,
        "list": list, "dict": dict, "set": set, "tuple": tuple, "str": str,
        "int": int, "float": float, "bool": bool, "repr": repr, "any": any, "all": all,
        "isinstance": isinstance, "True": True, "False": False, "None": None,
    }


# --------------------------------------------------------------------------
# Process-pool isolation (v3.0). Snippets never execute in the host
# interpreter: each runs inside a persistent daemonic worker PROCESS from a
# spawn-context pool. The timeout contract is enforced by the operating
# system — overdue workers are terminate()d, which physically kills even
# C-level GIL-holding computations. Only JSON-serialized strings cross the
# process boundary.
# --------------------------------------------------------------------------

#: Extra seconds granted beyond the snippet timeout, covering queue transit
#: and worker scheduling jitter before the OS-level kill fires.
_POOL_WAIT_GRACE_SECONDS = 0.5
#: Persistent worker processes (spawned lazily, respawned after a kill)
DEFAULT_POOL_WORKERS = 2
#: Upper bound for one IPC payload (defensive; results are pre-capped)
_MAX_IPC_BYTES = 256 * 1024

_POOL_LOCK = threading.Lock()
_POOL = None  # type: Optional["SandboxWorkerPool"]


def _safe_repr(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - defensive: hostile __repr__ is not reachable here
        text = "<unreprable>"
    return text[:cap]


def _execute_vetted_snippet(source: str, max_output_bytes: int) -> Dict[str, Any]:
    """Worker-side: (re-)run the AST gate, then execute in the restricted
    namespace. The worker trusts nothing the parent claims about vetting."""
    try:
        tree = check_ast(source)
    except SandboxSecurityError as exc:
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

    try:
        code = compile(tree, "<sandbox>", "exec")
    except (ValueError, TypeError, SyntaxError) as exc:  # pragma: no cover - defensive
        return {"status": "error", "error": "{}: {}".format(type(exc).__name__, exc)}

    try:
        runner = types.FunctionType(code, namespace, "<sandbox>")
        runner()
    except BaseException as exc:  # noqa: BLE001 - surface everything as a tool error
        return {"status": "error", "error": "{}: {}".format(type(exc).__name__, exc)}

    return {
        "status": "ok",
        "stdout": "".join(stdout_lines)[:max_output_bytes],
        "result": _safe_repr(namespace.get("__sandbox_result__"), max_output_bytes),
    }


def _sandbox_worker_main(task_q: Any, result_q: Any) -> None:
    """Persistent worker process: pull one task, re-vet + run it, push a
    ``json.dumps``-ed result string. The snippet's namespace holds no
    reference to either queue; ``None`` is the shutdown sentinel."""
    while True:
        try:
            task = task_q.get()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if task is None:
            return
        try:
            max_out = int(task.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES)
        except (TypeError, ValueError):
            max_out = DEFAULT_MAX_OUTPUT_BYTES
        try:
            result = _execute_vetted_snippet(str(task.get("source") or ""), max_out)
        except BaseException as exc:  # noqa: BLE001 - never leak the worker loop
            result = {"status": "error", "error": "{}: {}".format(type(exc).__name__, exc)}
        try:
            payload = json.dumps(result, ensure_ascii=True, default=str)
            result_q.put(payload[:_MAX_IPC_BYTES])
        except (ValueError, OSError):
            try:
                result_q.put(json.dumps(
                    {"status": "error", "error": "result not JSON-serializable"}))
            except (ValueError, OSError):
                return


class SandboxWorkerPool(object):
    """Round-robin pool of daemonic spawn-context worker processes. One
    snippet in flight at a time (a lock serializes calls); an overdue or
    dead worker is physically terminated and replaced for the next task."""

    def __init__(self, workers: int = DEFAULT_POOL_WORKERS, ctx_method: str = "spawn"):
        self._ctx = multiprocessing.get_context(ctx_method)
        self._slots = [self._new_slot() for _ in range(max(1, int(workers)))]
        self._next_slot = 0

    def _new_slot(self) -> Dict[str, Any]:
        task_q = self._ctx.Queue(maxsize=1)
        result_q = self._ctx.Queue(maxsize=1)
        proc = self._ctx.Process(
            target=_sandbox_worker_main, args=(task_q, result_q), daemon=True)
        proc.start()
        return {"proc": proc, "task_q": task_q, "result_q": result_q}

    def _replace_slot(self, index: int) -> None:
        slot = self._slots[index]
        proc = slot["proc"]
        try:
            proc.terminate()  # OS-level kill: SIGTERM (POSIX) / TerminateProcess (Win)
            proc.join(2.0)
        except (AssertionError, ValueError, OSError):  # pragma: no cover - defensive
            pass
        if proc.is_alive():  # pragma: no cover - defensive escalation
            try:
                proc.kill()
                proc.join(1.0)
            except (AssertionError, ValueError, OSError):
                pass
        for q in (slot["task_q"], slot["result_q"]):
            try:
                q.close()
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass
        self._slots[index] = self._new_slot()

    def run_snippet(self, source: str, timeout: float,
                    max_output_bytes: int) -> Dict[str, Any]:
        wait_budget = float(timeout) + _POOL_WAIT_GRACE_SECONDS
        index = self._next_slot % len(self._slots)
        self._next_slot += 1
        slot = self._slots[index]

        try:
            slot["task_q"].put(
                {"source": source, "max_output_bytes": int(max_output_bytes)},
                timeout=wait_budget)
        except (queue.Full, OSError, ValueError):
            self._replace_slot(index)
            return {
                "status": "timeout",
                "error": "sandbox worker did not accept the task within "
                         "{0}s; the worker process was terminated and the "
                         "pool respawned a fresh worker".format(wait_budget),
            }

        deadline = time.time() + wait_budget
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._replace_slot(index)  # physical kill of the overdue worker
                return {
                    "status": "timeout",
                    "error": "snippet exceeded {}s timeout (process-pool "
                             "breaker); the worker process was killed by the "
                             "OS and the pool respawned a fresh worker".format(timeout),
                }
            try:
                payload = slot["result_q"].get(timeout=min(0.25, remaining))
            except queue.Empty:
                if not slot["proc"].is_alive():
                    exitcode = slot["proc"].exitcode
                    self._replace_slot(index)
                    return {
                        "status": "error",
                        "error": "sandbox worker process exited unexpectedly "
                                 "(exitcode={}) before returning a result; "
                                 "the pool respawned a fresh worker".format(exitcode),
                    }
                continue
            except (EOFError, OSError, ValueError):  # pragma: no cover - defensive
                self._replace_slot(index)
                return {
                    "status": "error",
                    "error": "sandbox IPC channel failed; the pool respawned "
                             "a fresh worker",
                }
            try:
                return json.loads(payload)
            except (ValueError, TypeError):  # pragma: no cover - defensive
                self._replace_slot(index)
                return {
                    "status": "error",
                    "error": "sandbox worker returned a malformed result; "
                             "the pool respawned a fresh worker",
                }

    def shutdown(self) -> None:
        for index in range(len(self._slots)):
            slot = self._slots[index]
            try:
                slot["task_q"].put(None, timeout=0.2)  # graceful stop sentinel
            except (queue.Full, OSError, ValueError):
                pass
            proc = slot["proc"]
            try:
                proc.join(1.0)
            except (AssertionError, ValueError, OSError):  # pragma: no cover
                pass
            if proc.is_alive():
                self._replace_slot(index)
                slot = self._slots[index]
                try:
                    slot["proc"].join(1.0)
                except (AssertionError, ValueError, OSError):  # pragma: no cover
                    pass


def get_sandbox_pool() -> SandboxWorkerPool:
    """Lazy process-pool singleton (spawned on first snippet execution)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = SandboxWorkerPool()
            atexit.register(shutdown_sandbox_pool)
    return _POOL


def shutdown_sandbox_pool() -> None:
    """Stop all worker processes (registered via atexit; safe to call twice)."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown()


def execute_safe_python(
    source: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
) -> Dict[str, Any]:
    """Statically check then safely run a read-only Python snippet inside an
    isolated worker process (process-pool isolation; see module docstring).

    Returns a structured dict; never raises for hostile/broken input:
      {"status": "ok", "stdout": "...", "result": "..."}
      {"status": "rejected", "error": "..."}   <- AST check refused it
      {"status": "timeout", "error": "..."}    <- OS-level kill of the worker
      {"status": "error", "error": "..."}      <- compile/runtime/worker error
    """
    src = str(source or "")
    if not src.strip():
        return {"status": "rejected", "error": "empty snippet"}
    if len(src) > max_source_chars:
        return {
            "status": "rejected",
            "error": "snippet too large: {} > {} chars".format(len(src), max_source_chars),
        }

    # ---- stage 1: static gate in the host (no code object exists here) ----
    try:
        check_ast(src)
    except SandboxSecurityError as exc:
        logger.warning("sandbox rejected snippet: %s", exc)
        return {"status": "rejected", "error": str(exc)}
    except SyntaxError as exc:
        return {"status": "error", "error": "SyntaxError: {}".format(exc)}

    # ---- stage 2: execute in an isolated worker process (JSON-only IPC) ----
    try:
        result = get_sandbox_pool().run_snippet(src, timeout, max_output_bytes)
    except Exception as exc:  # noqa: BLE001 - pool failure must not crash a run
        logger.warning("sandbox pool failure: %s", exc)
        return {"status": "error", "error": "sandbox pool failure: {}".format(exc)}

    status = result.get("status")
    if status == "ok":
        return {
            "status": "ok",
            "stdout": str(result.get("stdout") or ""),
            "result": result.get("result"),
        }
    if status in ("rejected", "timeout", "error"):
        return {"status": status, "error": str(result.get("error") or "unknown error")}
    return {  # pragma: no cover - worker only emits the statuses above
        "status": "error",
        "error": "sandbox worker returned an unexpected status: {!r}".format(status),
    }
