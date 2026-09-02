"""Runs inside one worktree. Import the target module, call one callable over N
probes, write results to a file. Results never go to stdout -- the target may print.

Protocol (stdin, JSON):
    {root, file, qualname, kind, n_ctor, lines, probes: [[argsrc, ...], ...], out}
Output (file at `out`, JSON):
    {"results": [...], "reached": [bool, ...]} | {"error": "..."}
"""

import builtins
import contextlib
import importlib
import inspect
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

LIMIT = 2000

# The two revisions are checked out at different paths, so anything that surfaces
# its own location -- a cwd, a __file__, a path in an error message -- would differ
# for a reason that has nothing to do with the change. Both roots collapse to the
# same placeholder before anything is compared.
ROOTS = []

# A default repr carries the object's address, which changes every run. Left in,
# it makes every object-valued result look non-deterministic.
ADDR = re.compile(r"(?<= at 0x)[0-9a-fA-F]+(?=>)")

# The target file, and the lines in it the commit touched. A probe that calls the
# changed callable without executing one of these ran the function, not the edit.
TRACE = {"path": "", "lines": set()}


def tracer(path: str, lines: set, seen: set):
    """A trace function that watches one file. The global hook fires on every
    call in the process, so it hands back a line tracer for frames belonging to
    the target module and None for everything else, which leaves the rest of the
    program running at full speed."""
    def local(frame, event, arg):
        if event == "line" and frame.f_lineno in lines:
            seen.add(frame.f_lineno)
        return local

    def top(frame, event, arg):
        return local if frame.f_code.co_filename == path else None

    return top


def cap(s):
    for r in ROOTS:
        s = s.replace(r, "<repo>")
    s = ADDR.sub("...", s)
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
    seen = set()
    try:
        vals = [eval(a, env) for a in argsrc]
    except BaseException as e:
        return result("probe-error", f"{type(e).__name__}: {e}", "probe-error"), False
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
                # A construction harvested from the tests arrives already built;
                # anything else is assembled from its constructor's arguments.
                inst = ctor_args[0] if payload.get("built") else owner(*ctor_args)
                fn = getattr(inst, name)
            else:
                fn = getattr(owner, name)
    except BaseException as e:
        return result("setup-raise", f"{type(e).__name__}: {e}",
                      type(e).__name__, buf.getvalue()), False

    before = [safe_repr(a) for a in args]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if TRACE["lines"]:
                sys.settrace(tracer(TRACE["path"], TRACE["lines"], seen))
            try:
                r = fn(*args)
            finally:
                sys.settrace(None)
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
    return out, bool(seen)


def _ann(a):
    """Annotation as source text a probe can be built from. Type aliases and
    string annotations are resolved by inspect; typing constructs keep their full
    form, since collapsing Union[str, bytes] to "Union" loses the whole point."""
    if a is inspect.Parameter.empty:
        return ""
    if isinstance(a, str):
        return a
    if isinstance(a, type):
        return a.__name__
    return str(a)


def _params_of(fn, drop_first):
    try:
        sig = inspect.signature(fn, eval_str=True)
    except BaseException:
        try:
            sig = inspect.signature(fn)
        except BaseException as e:
            return None, f"{type(e).__name__}: {e}"
    out = []
    for i, (name, p) in enumerate(sig.parameters.items()):
        if drop_first and i == 0:
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue                    # nothing has to be passed for these
        if p.kind is p.KEYWORD_ONLY:
            if p.default is not p.empty:
                continue                # leave it at its default
            return None, f"{name} is keyword-only with no default"
        out.append([name, _ann(p.annotation), p.default is not p.empty])
    return out, None


def introspect(mod, payload):
    """Real signatures, resolved by the interpreter. The parse tree cannot see an
    inherited constructor, cannot expand a type alias, and cannot resolve a
    string annotation."""
    kind, qualname = payload["kind"], payload["qualname"]
    if kind == "function":
        target, ctor_src = getattr(mod, qualname), None
    else:
        cls_name, name = qualname.split(".", 1)
        cls = getattr(mod, cls_name)
        target = getattr(cls, name)
        ctor_src = cls

    params, why = _params_of(target, drop_first=(kind == "instance"))
    if params is None:
        return {"params": None, "reason": why}

    ctor = []
    if kind == "instance" and ctor_src.__init__ is not object.__init__:
        ctor, why = _params_of(ctor_src.__init__, drop_first=True)
        if ctor is None:
            return {"params": None, "reason": f"__init__: {why}"}
    return {"params": params, "ctor": ctor}


def main():
    payload = json.load(sys.stdin)
    out = Path(payload["out"])
    try:
        root = Path(payload["root"])
        ROOTS.extend(sorted({str(root), str(root.resolve())}, key=len, reverse=True))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            mod = load(root, payload["file"])
        if payload.get("mode") == "introspect":
            out.write_text(json.dumps(introspect(mod, payload)))
            return
        env = dict(vars(mod))
        env["__builtins__"] = builtins
        TRACE["path"] = str((root / payload["file"]).resolve())
        TRACE["lines"] = set(payload.get("lines") or [])
        runs = [call(mod, env, payload, a) for a in payload["probes"]]
        out.write_text(json.dumps({"results": [r for r, _ in runs],
                                   "reached": [h for _, h in runs]}))
    except BaseException as e:
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
