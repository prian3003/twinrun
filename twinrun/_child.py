"""Runs inside one worktree. Import target module, call one function over N probes,
write results to a file. Never writes results to stdout -- the target may print.

Protocol (stdin, JSON): {root, file, qualname, probes: [[argsrc, ...], ...], out}
Output (file at `out`, JSON): {"results": [...]} | {"error": "..."}
"""

import builtins
import contextlib
import importlib
import importlib.util
import io
import json
import sys
from pathlib import Path

LIMIT = 2000


def cap(s):
    return s if len(s) <= LIMIT else s[:LIMIT] + f"...<{len(s)} chars total>"


def load(root: Path, relfile: str):
    """Import module at root/relfile. Uses dotted import when it lives in a package,
    so relative imports inside it still resolve."""
    p = (root / relfile).resolve()
    parts = [p.stem]
    d = p.parent
    while (d / "__init__.py").exists() and d != d.parent:
        parts.insert(0, d.name)
        d = d.parent
    sys.path.insert(0, str(d))
    if len(parts) > 1:
        return importlib.import_module(".".join(parts))
    spec = importlib.util.spec_from_file_location(parts[0], p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[parts[0]] = mod
    spec.loader.exec_module(mod)
    return mod


def safe_repr(v):
    try:
        return repr(v)
    except BaseException as e:
        return f"<repr raised {type(e).__name__}>"


def call(fn, argsrc):
    buf = io.StringIO()
    try:
        args = [eval(a, {"__builtins__": builtins}) for a in argsrc]
    except BaseException as e:
        return {"kind": "probe-error", "value": f"{type(e).__name__}: {e}", "stdout": ""}
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            r = fn(*args)
        return {"kind": "return", "value": cap(safe_repr(r)), "stdout": cap(buf.getvalue())}
    except BaseException as e:
        return {"kind": "raise", "value": cap(f"{type(e).__name__}: {e}"), "stdout": cap(buf.getvalue())}


def main():
    payload = json.load(sys.stdin)
    out = Path(payload["out"])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            mod = load(Path(payload["root"]), payload["file"])
            fn = getattr(mod, payload["qualname"])
        results = [call(fn, a) for a in payload["probes"]]
        out.write_text(json.dumps({"results": results}))
    except BaseException as e:
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
