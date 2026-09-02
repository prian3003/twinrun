"""Runs inside one worktree. Import the target module, call one callable over N
probes, write results to a file. Results never go to stdout -- the target may print.

Protocol (stdin, JSON):
    {root, file, qualname, kind, n_ctor, probes: [[argsrc, ...], ...], out}
Output (file at `out`, JSON):
    {"results": [...]} | {"error": "..."}
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
    """Import module at root/relfile. Uses a dotted import when it lives inside a
    package, so relative imports in it still resolve."""
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


def state_of(obj):
    """Instance attributes after the call. Methods that mutate and return None
    are invisible without this."""
    try:
        return safe_repr(sorted(vars(obj).items()))
    except BaseException:
        return ""


def result(kind, value, type_name, stdout="", mutated=""):
    return {"kind": kind, "value": cap(value), "type": type_name,
            "stdout": cap(stdout), "mutated": cap(mutated)}


def call(mod, env, payload, argsrc):
    kind, qualname, n_ctor = payload["kind"], payload["qualname"], payload["n_ctor"]
    buf = io.StringIO()
    try:
        vals = [eval(a, env) for a in argsrc]
    except BaseException as e:
        return result("probe-error", f"{type(e).__name__}: {e}", "probe-error")
    ctor_args, args = vals[:n_ctor], vals[n_ctor:]

    owner = mod
    name = qualname
    if "." in qualname:
        cls_name, name = qualname.split(".", 1)
        owner = getattr(mod, cls_name)

    inst = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if kind == "instance":
                inst = owner(*ctor_args)          # fresh instance per probe
                fn = getattr(inst, name)
            else:
                fn = getattr(owner, name)
    except BaseException as e:
        return result("setup-raise", f"{type(e).__name__}: {e}",
                      type(e).__name__, buf.getvalue())

    before = [safe_repr(a) for a in args]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            r = fn(*args)
        out = result("return", safe_repr(r), type(r).__name__, buf.getvalue())
    except BaseException as e:
        out = result("raise", f"{type(e).__name__}: {e}", type(e).__name__, buf.getvalue())

    after = [safe_repr(a) for a in args]
    marks = []
    if after != before:
        marks.append("args=" + safe_repr(after))
    if inst is not None:
        marks.append("self=" + state_of(inst))
    out["mutated"] = cap(" ".join(marks))
    return out


def main():
    payload = json.load(sys.stdin)
    out = Path(payload["out"])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            mod = load(Path(payload["root"]), payload["file"])
        env = dict(vars(mod))
        env["__builtins__"] = builtins
        results = [call(mod, env, payload, a) for a in payload["probes"]]
        out.write_text(json.dumps({"results": results}))
    except BaseException as e:
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
