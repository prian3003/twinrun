"""Runs inside one worktree. Import the target module, call one callable over N
probes, write results to a file. Results never go to stdout -- the target may print.

Protocol (stdin, JSON):
    {root, file, qualname, kind, n_ctor, lines, probes: [[argsrc, ...], ...], out}
Output (file at `out`, JSON):
    {"results": [...], "reached": [bool, ...]} | {"error": "..."}
"""

import builtins
import contextlib
import itertools
import importlib
import inspect
import importlib.util
import io
import json
import re
import sys
import tempfile
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

# The name of a temporary file is random by design, so a function that makes one
# answers differently every run and its probe is dropped as flaky -- which is
# what `CliRunner.isolated_filesystem` does, and what every helper that writes a
# scratch file does. Only the one segment the interpreter chose is collapsed:
# what the code put underneath it is still the answer.
# A warning or a traceback names the line it came from, and any commit that
# moves code moves that number. `types.py:744: RuntimeWarning: bool is used as
# a file descriptor` against `types.py:773: RuntimeWarning: ...` is the same
# warning about the same call, reported as a delta because the lines above it
# changed. Only a path this repository owns is collapsed -- one printed by the
# code itself is still the answer.
LINENO = re.compile(r"(<repo>[^\s:\"']*\.py):\d+")

TMP = re.compile(re.escape(tempfile.gettempdir().rstrip("/")) + r"/[^/\s'\"]+")

# The target file, and the lines in it the commit touched. A probe that calls the
# changed callable without executing one of these ran the function, not the edit.
TRACE = {"path": "", "lines": set()}

# How much of a generator to drain. Long enough for a body that yields per
# item of an argument, short enough that an endless one is not the whole run.
GEN_CAP = 64
# Writes recorded per probe. A function that streams a file would otherwise put
# its whole output in the comparison, and the first few lines already say
# whether what it writes changed.
WRITES = 20


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
    s = TMP.sub("<tmp>", s)
    s = LINENO.sub(r"\1:<line>", s)
    # A value past LIMIT is shown as its first LIMIT characters, and the length
    # must not go into the compared string. Two values with the same visible
    # prefix would then differ by a byte count alone, and the report would show
    # the reader two identical blocks -- a finding the tool cannot display is a
    # finding it cannot defend. rich's Console._caller_frame_info returns a
    # frame's globals, whose repr grows whenever anything in the module is
    # renamed or imported, so every commit near it differed.
    return s if len(s) <= LIMIT else s[:LIMIT] + "...<truncated>"


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
    """The instance's own attributes. Read either side of a call, so a method
    that mutates and returns None is not invisible, and read once after a
    constructor, which has nothing else to answer with."""
    try:
        return safe_repr(sorted(vars(obj).items()))
    except BaseException:
        return ""


def result(kind, value, type_name, stdout="", mutated=""):
    return {"kind": kind, "value": cap(value), "type": type_name,
            "stdout": cap(stdout), "mutated": cap(mutated)}


class Sibling(dict):
    """The changed module's namespace, plus what its siblings can lend it.

    A construction is synthesised from the constructors of every module in the
    package, and the probe is evaluated in the namespace of one of them. click's
    termui.py can name five of the fifty-nine classes that map holds, so a
    parameter wanting a Command got an expression its own module had never
    heard of and every probe for that callable died on the NameError.

    The class is one import away, and the two revisions resolve the name the
    same way, so lending it cannot invent a difference between them -- it only
    lets the probe run. Modules are consulted in name order so a name that two
    of them export resolves to the same one every time.
    """

    def __init__(self, mod):
        super().__init__(vars(mod))
        top = (getattr(mod, "__package__", "") or mod.__name__).split(".")[0]
        self._pkg = [m for name, m in sorted(sys.modules.items())
                     if m is not None and (name == top or name.startswith(top + "."))]

    def __missing__(self, name):
        if not name.startswith("_"):
            for m in self._pkg:
                if hasattr(m, name):
                    self[name] = v = getattr(m, name)
                    return v
        raise KeyError(name)


@contextlib.contextmanager
def traced(seen):
    """Record which of the commit's lines run inside this block.

    Setting up a probe executes the target's code too. A commit that moves only
    `__init__` runs entirely while the instance is being built, and a trace that
    starts at the call itself sees none of it -- the probe was told it never
    reached a change it had already run.
    """
    if not TRACE["lines"]:
        yield
        return
    sys.settrace(tracer(TRACE["path"], TRACE["lines"], seen))
    try:
        yield
    finally:
        sys.settrace(None)


def call(mod, env, payload, argsrc):
    kind, qualname, n_ctor = payload["kind"], payload["qualname"], payload["n_ctor"]
    buf = io.StringIO()
    seen = set()
    try:
        with traced(seen):
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
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf), \
                traced(seen):
            if kind == "ctor":
                fn = owner                    # the class, called to build one
            elif kind in ("instance", "property"):
                # A construction harvested from the tests arrives already built;
                # anything else is assembled from its constructor's arguments.
                inst = ctor_args[0] if payload.get("built") else owner(*ctor_args)
                # Reading a property runs its getter, so the read belongs with
                # the call and not with the setup: an exception it raises is the
                # answer, not a failure to build the probe.
                fn = (lambda i=inst: getattr(i, name)) if kind == "property" \
                    else getattr(inst, name)
            else:
                fn = getattr(owner, name)
    except BaseException as e:
        return result("setup-raise", f"{type(e).__name__}: {e}",
                      type(e).__name__, buf.getvalue()), False

    before = [safe_repr(a) for a in args]
    was = state_of(inst) if inst is not None else None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf), \
                traced(seen):
            r = fn(*args)
            if payload.get("is_async"):
                import asyncio
                r = asyncio.run(r)
            if inspect.isgenerator(r):
                # A generator function returns without running a line of its
                # own body. The probe compared two <generator object> reprs,
                # which are equal on every commit, and no moved line was ever
                # traced -- the callable was reported as one no probe reached
                # and nothing about it was checked. Drain a bounded prefix
                # under the same trace: the body runs, the yields are the
                # answer, and an infinite one still stops.
                r = list(itertools.islice(r, GEN_CAP))
            elif payload.get("is_cm"):
                # Entering runs the body up to the yield, leaving runs the rest,
                # and the yielded value is what the caller of a `with` gets.
                with r as v:
                    r = v
        val = state_of(r) if kind == "ctor" else safe_repr(r)
        out = result("return", val, type(r).__name__, buf.getvalue())
    except BaseException as e:
        # A note is part of what the caller is shown and part of what changed.
        # networkx's 05809740 replaced `raise KeyError("The edge is not in the
        # graph")` with add_note on the original KeyError, so str(e) went from
        # the sentence to `1` -- a real break for anyone matching the message,
        # and unreadable in a report that shows only str(e).
        notes = "".join(f" [{n}]" for n in getattr(e, "__notes__", ()) or ())
        out = result("raise", f"{type(e).__name__}: {e}{notes}",
                     type(e).__name__, buf.getvalue())

    after = [safe_repr(a) for a in args]
    marks = []
    if after != before:
        marks.append("args=" + safe_repr(after))
    # What the call did to the instance, not what the instance held when it
    # arrived. A commit that renames an attribute in __init__ leaves every
    # method on the class holding a different-looking object while every one of
    # them returns exactly what it used to -- one root cause, reported once per
    # method, and none of them the place it happened.
    if inst is not None and (now := state_of(inst)) != was:
        marks.append("self=" + now)
    out["mutated"] = cap(" ".join(marks))
    return out, bool(seen)


FROZEN = (str, bytes, int, float, bool, type(None))


def freeze(env, exprs):
    """What each expression evaluates to in this revision, as source and a type.

    The type is the other half of the answer. A producer written before anyone
    typed the codebase declares no return annotation, and there is nothing in
    the source that says what it makes -- calling it is the only way to find
    out, and calling it is what this does.

    Only a scalar comes back. An object has no source form that rebuilds it and
    a container holds objects, but the value worth freezing -- a signed token, a
    serialized payload, a formatted key -- is a str or bytes, and that is the
    one an output comparison was going to be blocked on.
    """
    out = {}
    for e in exprs:
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                v = eval(e, env)
        except BaseException:
            continue
        if type(v) not in FROZEN:
            continue
        # repr is a promise, not a guarantee: `inf` and `nan` do not parse back,
        # and a str subclass reprs as something that is not it.
        r = repr(v)
        try:
            back = eval(r, {"__builtins__": {}})
        except BaseException:
            continue
        if type(back) is type(v) and back == v:
            out[e] = [r, type(v).__name__]
    return out


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


STAR = "*"          # stands in for a `*args` in a parameter list


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
        if p.kind is p.VAR_POSITIONAL:
            # Nothing has to be passed for it, but anything may: a `*args` on
            # one side accepts whatever the other side names. Recorded under a
            # name no parameter can have, and read off by `_agree`.
            out.append([STAR, "", True])
            continue
        if p.kind is p.VAR_KEYWORD:
            continue                    # a positional call never fills it
        if p.kind is p.KEYWORD_ONLY:
            if p.default is not p.empty:
                continue                # leave it at its default
            return None, f"{name} is keyword-only with no default"
        out.append([name, _ann(p.annotation), p.default is not p.empty])
    return out, None


def transparent(fn, path: str) -> bool:
    """Whether calling the decorated name still runs the function in this file.

    A decorator that wraps the target leaves __wrapped__ behind -- functools.wraps
    sets it, and networkx's @_dispatchable is one of these: the parameters it adds
    are keyword-only, so the positional call the parse tree wrote still reaches the
    body. @lru_cache is another, and a cached call is the same call: both sides run
    in their own process, so both fill their own cache with the same values.

    A decorator that hands back something else is not the function under test and is
    not called. click's @command builds a Command, whose call runs a command line --
    it has no __wrapped__ and no code of its own, and neither has a class.
    """
    if not callable(fn):
        return False
    code = getattr(inspect.unwrap(fn), "__code__", None)
    return code is not None and \
        Path(code.co_filename).resolve() == Path(path).resolve()


def introspect(mod, payload):
    """Real signatures, resolved by the interpreter. The parse tree cannot see an
    inherited constructor, cannot expand a type alias, and cannot resolve a
    string annotation."""
    kind, qualname = payload["kind"], payload["qualname"]
    if kind == "function":
        target, ctor_src = getattr(mod, qualname), None
        decs = payload.get("decorated") or []
        if decs and not transparent(target, Path(payload["root"]) / payload["file"]):
            return {"params": None, "reason": f"decorated ({', '.join(decs)})"}
    else:
        cls_name, name = qualname.split(".", 1)
        cls = getattr(mod, cls_name)
        target = getattr(cls, name)
        ctor_src = cls

    if kind == "property":
        params, why = [], None       # an attribute read takes no arguments
    else:
        params, why = _params_of(target, drop_first=kind in ("instance", "ctor"))
    if params is None:
        return {"params": None, "reason": why}

    ctor = []
    if kind in ("instance", "property") and ctor_src.__init__ is not object.__init__:
        ctor, why = _params_of(ctor_src.__init__, drop_first=True)
        if ctor is None:
            return {"params": None, "reason": f"__init__: {why}"}
    return {"params": params, "ctor": ctor}


def neutralise():
    """Record what a probe tries to run instead of running it.

    Probing click meant probing `Editor.edit_files` and `open_url`, which launch
    a real editor and a real browser: two per side, per repeat, each ending in a
    20-second timeout with nothing to show for it. Executing them is not the
    only loss -- a command that goes out to the shell is invisible to an output
    comparison, so the commit that started quoting its arguments with
    `shlex.quote` changed something no probe could see.

    Printing the call instead puts it in captured stdout, which is already part
    of what the two sides are compared on. The argument list becomes the
    observable, so a change in how a command is assembled reads as a delta and
    nothing runs. A raised connection is the same bargain for the network,
    minus the interest in what was sent.

    ponytail: an in-process shim, not a sandbox. It stops the paths a probe
    stumbles into, not code that means to escape; os.fork, ctypes and a C
    extension all go around it. Real isolation is a container or seccomp.
    """
    import os
    import socket
    import subprocess

    def show(what, args):
        print(f"[twinrun] {what} {args!r}")

    class Stub:
        """Enough of a finished process for a caller to inspect and move on."""
        returncode, stdout, stderr, pid = 0, "", "", 0

        def __init__(self, *a, **k):
            self.args = a[0] if a else None

        def communicate(self, *a, **k):
            return "", ""

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def popen(*a, **k):
        show("subprocess", a[0] if a else k.get("args"))
        return Stub(*a)

    subprocess.Popen = popen
    subprocess.run = lambda *a, **k: (show("subprocess", a[0] if a else k.get("args"))
                                      or Stub(*a))
    subprocess.call = subprocess.check_call = lambda *a, **k: (
        show("subprocess", a[0] if a else k.get("args")) or 0)
    subprocess.check_output = lambda *a, **k: (
        show("subprocess", a[0] if a else k.get("args")) or "")
    os.system = lambda cmd: show("shell", cmd) or 0
    os.popen = lambda cmd, *a, **k: show("shell", cmd) or io.StringIO("")
    for name in ("execv", "execvp", "execve", "execl", "execlp", "spawnv", "spawnl"):
        if hasattr(os, name):
            setattr(os, name, lambda *a, _n=name, **k: show(_n, a))
    try:
        import webbrowser
        webbrowser.open = lambda url, *a, **k: show("browser", url) or True
        webbrowser.open_new = webbrowser.open_new_tab = webbrowser.open
    except ImportError:
        pass

    # A file write is invisible to an output comparison for the same reason a
    # shell command was: the bytes land on disk and the return value says
    # nothing about them. `fh.write("v1:" + name)` and `fh.write("v2:" + name)`
    # return the same count. Recorded rather than stubbed, because code that
    # writes a file and reads it back has to keep working, and because the
    # path is worth seeing even when the content is not.
    written = [0]
    real_open = builtins.open

    def opened(file, mode="r", *a, **k):
        f = real_open(file, mode, *a, **k)
        if any(c in mode for c in "wxa+"):
            show("open", cap(str(file)))
            inner = f.write

            def write(data, _inner=inner):
                if written[0] < WRITES:
                    written[0] += 1
                    show("wrote", cap(data if isinstance(data, str) else repr(data)))
                return _inner(data)
            try:
                f.write = write
            except (AttributeError, TypeError):
                pass        # a file object that will not take one is left alone
        return f

    builtins.open = io.open = opened

    def refuse(*a, **k):
        raise OSError("twinrun: the network is not available to a probe")

    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse


def main():
    payload = json.load(sys.stdin)
    out = Path(payload["out"])
    try:
        root = Path(payload["root"])
        ROOTS.extend(sorted({str(root), str(root.resolve())}, key=len, reverse=True))
        neutralise()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            mod = load(root, payload["file"])
        if payload.get("mode") == "introspect":
            out.write_text(json.dumps(introspect(mod, payload)))
            return
        env = Sibling(mod)
        env["__builtins__"] = builtins
        if payload.get("mode") == "freeze":
            out.write_text(json.dumps({"frozen": freeze(env, payload["exprs"])}))
            return
        TRACE["path"] = str((root / payload["file"]).resolve())
        TRACE["lines"] = set(payload.get("lines") or [])
        runs = [call(mod, env, payload, a) for a in payload["probes"]]
        out.write_text(json.dumps({"results": [r for r, _ in runs],
                                   "reached": [h for _, h in runs]}))
    except BaseException as e:
        out.write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
