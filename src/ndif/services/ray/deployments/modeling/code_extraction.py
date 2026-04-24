"""Extract user code from deserialized nnsight requests."""

import ast
import textwrap
from typing import Optional, List, Set, Callable

from nnsight.schema.request import RequestModel


# Internal method names -> user-facing names
METHOD_NAMES = {
    '__nnsight_generate__': 'generate',
    '__nnsight_trace__': 'trace',
    '__nnsight_session__': 'session',
}


def get_user_code(request: RequestModel) -> Optional[str]:
    """Extract user code from an nnsight request.

    Returns a formatted string with:
    1. Referenced function definitions (those with __source__)
    2. Main trace code wrapped in reconstructed trace context
    """
    parts = []
    seen_sources: Set[str] = set()
    function_defs: List[str] = []
    trace_code: List[str] = []

    # Try mediators first (InterleavingTracer)
    mediators = getattr(request.tracer, 'mediators', [])

    if mediators:
        # Build the trace wrapper: with model.trace(...):
        wrapper = _build_trace_wrapper(request.tracer)

        for mediator in mediators:
            intervention = mediator.intervention

            # Get referenced functions with __source__
            for source in _get_referenced_functions(intervention, seen_sources):
                function_defs.append(source)

            # Get main trace code
            if code := _get_trace_code(intervention):
                trace_code.append(code)

        # Wrap trace code in the reconstructed context
        if trace_code and wrapper:
            inner_code = "\n".join("    " + line for block in trace_code for line in block.split("\n"))
            trace_code = [f"{wrapper}\n{inner_code}"]

    else:
        # No mediators - likely a session (base Tracer)
        # Fall back to request.interventions which has the compiled session code
        intervention = request.interventions

        # Get referenced functions
        for source in _get_referenced_functions(intervention, seen_sources):
            function_defs.append(source)

        # Get the session code (already includes with blocks)
        if code := _get_trace_code(intervention):
            trace_code.append(code)

    if function_defs:
        parts.append("# --- Definitions ---\n" + "\n\n".join(function_defs))

    if trace_code:
        parts.append("# --- Trace ---\n" + "\n\n".join(trace_code))

    return "\n\n".join(parts) if parts else None


def _build_trace_wrapper(tracer) -> Optional[str]:
    """Build the 'with model.trace(...)' line from tracer metadata."""
    fn = getattr(tracer, 'fn', None)
    if not fn:
        return None

    method = METHOD_NAMES.get(fn, fn)

    # Get args
    args = getattr(tracer, 'args', None)
    if not args:
        batcher = getattr(tracer, 'batcher', None)
        if batcher:
            args = getattr(batcher, 'batched_args', None)

    # Get kwargs
    kwargs = getattr(tracer, 'kwargs', None)
    if not kwargs:
        batcher = getattr(tracer, 'batcher', None)
        if batcher:
            kwargs = getattr(batcher, 'batched_kwargs', None)

    # Build argument string
    arg_parts = []
    if args:
        arg_parts.extend(_truncate(repr(a), 50) for a in args)
    if kwargs:
        arg_parts.extend(f"{k}={_truncate(repr(v), 30)}" for k, v in kwargs.items())

    args_str = ", ".join(arg_parts)
    return f"with model.{method}({args_str}):"


def _get_trace_code(func: Callable) -> Optional[str]:
    """Extract user code from a compiled intervention function."""
    source = getattr(func, '__source__', None)
    if not source:
        return None

    return _unwrap_intervention(source)


def _get_referenced_functions(func: Callable, seen: Set[str]) -> List[str]:
    """Get source of referenced functions that have __source__."""
    results = []

    code = getattr(func, '__code__', None)
    if not code:
        return results

    func_globals = getattr(func, '__globals__', {})

    for name in code.co_names:
        if name.startswith('__'):
            continue

        obj = func_globals.get(name)
        if obj is None:
            continue

        if callable(obj) and hasattr(obj, '__source__'):
            source = obj.__source__.strip()
            if source not in seen:
                seen.add(source)
                results.append(source)

    return results


def _unwrap_intervention(source: str) -> str:
    """Remove nnsight wrapper from compiled intervention code.

    Compiled interventions look like:
        def __nnsight_tracer_123__(__nnsight_mediator__, ...):
            __nnsight_mediator__.pull()
            try:
                <user code>
            except Exception as exception:
                __nnsight_mediator__.exception(exception)
            ...

    We extract just <user code>.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))

        if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
            return source

        func_body = tree.body[0].body
        user_stmts = []

        # Look for try block (Invoker pattern)
        for stmt in func_body:
            if isinstance(stmt, ast.Try):
                user_stmts = [s for s in stmt.body if not _is_wrapper_call(s)]
                break

        # No try block - use direct statements (Tracer pattern)
        if not user_stmts:
            user_stmts = [s for s in func_body
                         if not _is_wrapper_call(s) and not _is_wrapper_return(s)]

        if user_stmts:
            return ast.unparse(ast.Module(body=user_stmts, type_ignores=[]))

    except Exception:
        pass

    return source


def _is_wrapper_call(node: ast.AST) -> bool:
    """Check if node is a wrapper call like __nnsight_*.pull()"""
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        if isinstance(call.func, ast.Attribute):
            return call.func.attr in ('pull', 'push', 'end', 'exception', 'get_frame')
    return False


def _is_wrapper_return(node: ast.AST) -> bool:
    """Check if node is 'return __nnsight_*.push()'"""
    if isinstance(node, ast.Return) and node.value:
        if isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute):
                return call.func.attr in ('pull', 'push', 'end', 'exception')
    return False


def _truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis."""
    return s if len(s) <= max_len else s[:max_len] + "..."
