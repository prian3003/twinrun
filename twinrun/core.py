"""Twin-run verification.

Run the base and head versions of a changed callable on identical inputs and
diff the outputs. The old code is the oracle, so no specification is needed.

Every probe runs twice per side. A probe whose own side disagrees with itself is
non-deterministic and is dropped, not reported. Noise is the thing that gets a
tool like this muted, so the filter is not optional.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import itertools
import json
import random
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CHILD = Path(__file__).parent / "_child.py"

# Probe corpus. Deliberately small and boring: edge values a human would try.
CORPUS = {
    "int": ["0", "1", "-1", "2", "7", "100", "-100", "2**31"],
    "float": ["0.0", "1.0", "-1.5", "0.1", "1e9", "float('inf')", "float('nan')"],
    "str": ["''", "'a'", "'hello world'", "'0'", "' '", "'\\n'", "'Uni\\u0308code'", "'x' * 200"],
    "bool": ["True", "False"],
    "bytes": ["b''", "b'a'", "b'\\x00\\xff'"],
    "list": ["[]", "[1]", "[1, 2, 3]", "[-1, 0, 1]", "['a', 'b']"],
    "dict": ["{}", "{'a': 1}", "{'a': 1, 'b': 2}"],
    "tuple": ["()", "(1,)", "(1, 2)"],
    "set": ["set()", "{1}", "{1, 2}"],
    "none": ["None"],
    # A file parameter is annotated IO, TextIO or BinaryIO and is never a name
    # the target module imported, so it is built from `io` directly.
    "io": ["__import__('io').BytesIO(b'')", "__import__('io').BytesIO(b'a.b')"],
    "binaryio": ["__import__('io').BytesIO(b'')", "__import__('io').BytesIO(b'a.b')"],
    "textio": ["__import__('io').StringIO('')", "__import__('io').StringIO('a.b')"],
    # The type argument of a container is not the container. `Iterable[V]` was
    # resolving to the TypeVar and probed with `V()`, which is not a name that
    # exists; the abstract names get the concrete corpus of what they describe,
    # and because they stand ahead of the element type in the annotation they
    # win the search below.
    "iterable": ["[]", "[1]", "[1, 2, 3]", "['a', 'b']"],
    "sequence": ["[]", "[1]", "[1, 2, 3]", "['a', 'b']"],
    "iterator": ["iter([])", "iter([1, 2, 3])", "iter(['a', 'b'])"],
    "mapping": ["{}", "{'a': 1}", "{'a': 1, 'b': 2}"],
    "mutablemapping": ["{}", "{'a': 1}", "{'a': 1, 'b': 2}"],
    "callable": ["(lambda *a, **k: None)", "str", "len"],
    "any": ["0", "1", "-1", "''", "'a'", "[]", "None", "True"],
    "object": ["0", "'a'", "None"],
}
# No annotation: try a spread. Most land on TypeError identically on both sides,
# which costs a probe and reports nothing. LLM probe synthesis is the next step.
UNTYPED = ["0", "1", "-1", "''", "'a'", "[]", "{}", "None", "True", "-0.5"]

DECOR_OK = {"staticmethod", "classmethod"}

# Exceptions the interpreter raises when a value is the wrong kind of thing.
# If the old code refused a probe with one of these, the probe was never a
# valid call, and what the new code does with it is not a behaviour anyone was
# relying on. jinja's pyupgrade commit reported 19 findings on the strength of
# TemplateSyntaxError(msg, lineno='a'): the base raised "%d format: a real
# number is required" and the f-string that replaced it formatted the string
# happily. True, and not a regression.
# ponytail: exception type is the whole test; a function whose contract is to
# raise TypeError has its message changes dropped with the noise. Narrow it by
# checking that the raise came from below the target's own frame if that bites.
REFUSED = {"TypeError", "AttributeError"}

# How many levels of project type a constructor may want before the synthesis
# gives up. One level is enough for a library of leaf types and not enough for a
# framework: click's Context takes a Command, and at one level every probe that
# needed a Context died in setup.
CTOR_DEPTH = 2

# Probing a test file means calling its entry points, which runs the suite and
# then reports that its own output changed. True, and useless.
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


# --------------------------------------------------------------------------
# blast radius: which callables actually changed behaviour-relevant source
# --------------------------------------------------------------------------

@dataclass
class Change:
    file: str
    qualname: str
    kind: str = "function"          # function | static | classmethod | instance
    params: list[tuple[str, str]] = field(default_factory=list)
    ctor_params: list[tuple[str, str]] = field(default_factory=list)
    ctors: dict = field(default_factory=dict)   # local class -> __init__ params
    producers: dict = field(default_factory=dict)   # annotation -> call expressions
    built: list[str] = field(default_factory=list)  # constructions taken from the tests
    lines: dict = field(default_factory=dict)   # side -> line numbers the commit touched
    guards: dict = field(default_factory=dict)  # parameter -> literals a branch demands
    hints: list = field(default_factory=list)   # literals the moved lines operate on
    risky: set = field(default_factory=set)  # producer calls the commit changed
    unknown: list = field(default_factory=list)  # producer calls with no declared type
    skip: str | None = None
    is_async: bool = False          # awaited in the child rather than called

    @property
    def instances(self) -> list[str]:
        """Whole constructions to probe with, in place of assembling the
        constructor's arguments from the corpus. A constructor with six
        parameters spends six probe columns on getting itself built; one the
        test suite already wrote spends one, and is known to work."""
        return self.built if self.kind in ("instance", "property") and self.built else []


@dataclass
class Delta:
    file: str
    qualname: str
    args: list[str]
    base: dict
    head: dict
    kind: str = "function"
    n_ctor: int = 0
    built: bool = False


@dataclass
class Report:
    deltas: list[Delta] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (qualname, reason)
    checked: int = 0        # callables actually twin-run
    probes: int = 0         # probes compared after the flake filter
    flaky: int = 0          # probes dropped as non-deterministic
    refused: int = 0        # probes the oracle rejected as the wrong type
    known: list = field(default_factory=list)   # deltas a verdict already ruled on
    reached: int = 0        # of those, probes that executed a line the commit touched


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def resolve(repo, rev: str) -> str:
    """Turn a revision into a commit sha, with a message a human can act on."""
    p = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True, text=True,
    )
    if p.returncode:
        raise RuntimeError(
            f"no such revision {rev!r} in {repo}"
            + (" (the repository has only one commit)"
               if rev.endswith("~1") else "")
        )
    return p.stdout.strip()


def _targets(src: str) -> dict[str, tuple]:
    """qualname -> (node, enclosing ClassDef or None). Module functions and methods."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = (n, None)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = (m, n)
    return out


def _ctor_map(srcs, owners: dict | None = None) -> dict[str, list]:
    """Constructor parameters for every class in a package, with an inherited
    __init__ resolved to the base class that defines it. TimestampSigner takes
    its constructor from Signer in another module; without this it is built
    with no arguments at all, and every probe dies in setup.

    `owners`, if given, is filled with the class each name took its __init__
    from -- itself, or the ancestor the walk below landed on. The caller needs
    it to tell whose constructor a commit actually changed."""
    own, bases = {}, {}
    for src in srcs:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        al = _aliases(tree)
        for n in tree.body:
            if not isinstance(n, ast.ClassDef):
                continue
            bases[n.name] = [ast.unparse(b).split(".")[-1] for b in n.bases]
            init = _init_of(n)
            if init is not None:
                own[n.name] = None if _bad_sig(init) else [
                    (name, _expand(ann, al)) for name, ann in _sig_params(init, True)]
                if owners is not None:
                    owners[n.name] = n.name
    out = dict(own)
    for cls in bases:
        cur, seen = cls, set()
        while cls not in out and cur in bases and cur not in seen:
            seen.add(cur)
            cur = next((b for b in bases[cur] if b in bases), None)
            if cur is None:
                break
            if cur in own:
                out[cls] = own[cur]
                if owners is not None:
                    owners[cls] = cur
    return {k: v for k, v in out.items() if v is not None}


def _decorators(node) -> set[str]:
    return {ast.unparse(d).split("(")[0].split(".")[-1] for d in node.decorator_list}


def _sig_params(node, drop_first: bool):
    args = [*node.args.posonlyargs, *node.args.args]
    if drop_first:
        args = args[1:]
    return [(a.arg, ast.unparse(a.annotation) if a.annotation else "") for a in args]


def _init_of(cls_node):
    if cls_node is None:
        return None
    return next((m for m in cls_node.body
                 if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)


def _sig_msg(base_node, head_node, drop_first) -> str:
    b = _fmt(_sig_params(base_node, drop_first))
    h = _fmt(_sig_params(head_node, drop_first))
    return f"signature changed: ({b}) -> ({h})"


def _bad_sig(node) -> bool:
    """*args and **kwargs are optional by construction, and a keyword-only
    parameter with a default can be left at it -- none of the three is a reason
    to give up on a callable. A keyword-only parameter with no default has to be
    passed by name, which the positional probe path cannot do."""
    return any(d is None for d in node.args.kw_defaults)


def _names(params):
    return [n for n, _ in params]


def _fmt(params):
    return ", ".join(_names(params))


def _reconcile(base_params, head_params, head_defaults):
    """The parameter list both revisions can be called with, or None.

    A signature change is already visible in the diff, so it is not the hidden
    behaviour change this tool exists to find. What matters is whether the old
    calls still do the old thing. When head only appends optional parameters,
    that question still has an answer: call both sides with the old list.
    """
    if _names(base_params) == _names(head_params):
        return head_params
    added = len(head_params) - len(base_params)
    if 0 < added <= head_defaults and _names(head_params[:len(base_params)]) == _names(base_params):
        return head_params[:len(base_params)]
    return None


def _transparent(src: str) -> set[str]:
    """Decorators in this module that hand the function back unchanged.

    A marker decorator records something about a function and returns it:
    jinja's @internalcode registers a code object, @pass_context sets an
    attribute the renderer reads later. The decorated name is still the plain
    function, so skipping it costs a callable and buys nothing -- 51 of them in
    one click and jinja sweep. A wrapper is not transparent and is not matched:
    its return is a call or a closure, never its own parameter.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef) or not n.args.args:
            continue
        first = n.args.args[0].arg
        rets = [r for r in ast.walk(n) if isinstance(r, ast.Return)]
        if rets and all(isinstance(r.value, ast.Name) and r.value.id == first
                        for r in rets):
            out.add(n.name)
    return out


def _describe(file: str, qualname: str, node, cls_node, base_node, base_cls,
              transparent: frozenset = frozenset()) -> Change:
    name = qualname.rsplit(".", 1)[-1]
    # An `async def` is an ordinary callable whose answer arrives through an
    # event loop, and the child runs one. What stays skipped is a shape the
    # comparison cannot make sense of: a coroutine on one side and a plain
    # function on the other is a different way of being called, and an async
    # generator has no single result to compare.
    if isinstance(node, ast.AsyncFunctionDef) != isinstance(base_node, ast.AsyncFunctionDef):
        return Change(file, qualname, skip="changed between sync and async")
    if isinstance(node, ast.AsyncFunctionDef) and \
            any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(node)):
        return Change(file, qualname, skip="async generator")
    decs = _decorators(node) - transparent

    if cls_node is None:
        if decs:
            return Change(file, qualname, skip=f"decorated ({', '.join(sorted(decs))})")
        if _bad_sig(node) or _bad_sig(base_node):
            return Change(file, qualname, skip="a keyword-only parameter has no default")
        params = _reconcile(_sig_params(base_node, False), _sig_params(node, False),
                            len(node.args.defaults))
        if params is None:
            return Change(file, qualname, skip=_sig_msg(base_node, node, False))
        return Change(file, qualname, params=params)

    if name in ("__init__", "__new__"):
        # A constructor returns nothing, so the instance it left behind is the
        # answer, and comparing that is the whole probe. Reading it off the
        # methods instead reported one attribute rename once per method and
        # never at the line it happened on.
        if decs:
            return Change(file, qualname, skip=f"decorated ({', '.join(sorted(decs))})")
        if _bad_sig(node) or _bad_sig(base_node):
            return Change(file, qualname, skip="a keyword-only parameter has no default")
        params = _reconcile(_sig_params(base_node, True), _sig_params(node, True),
                            len(node.args.defaults))
        if params is None:
            return Change(file, qualname, skip=_sig_msg(base_node, node, True))
        return Change(file, qualname, kind="ctor", params=params)
    # A property is a method whose call is an attribute read. The value it
    # computes is behaviour like any other -- a commit that changes what
    # `cart.total` comes back with is exactly what this tool is for -- and it
    # was the largest single class of skip in the click and jinja sweeps.
    unknown = decs - DECOR_OK - {"property"}
    if unknown:
        return Change(file, qualname, skip=f"decorated ({', '.join(sorted(unknown))})")
    if _bad_sig(node) or _bad_sig(base_node):
        return Change(file, qualname, skip="a keyword-only parameter has no default")
    if decs & DECOR_OK:
        drop = "classmethod" in decs
        params = _reconcile(_sig_params(base_node, drop), _sig_params(node, drop),
                            len(node.args.defaults))
        if params is None:
            return Change(file, qualname, skip=_sig_msg(base_node, node, drop))
        kind = "classmethod" if drop else "static"
        return Change(file, qualname, kind=kind, params=params)

    h_init, b_init = _init_of(cls_node), _init_of(base_cls)
    if (h_init is not None and _bad_sig(h_init)) or (b_init is not None and _bad_sig(b_init)):
        return Change(file, qualname, skip=f"{cls_node.name}.__init__ has a keyword-only parameter with no default")
    if (h_init is None) != (b_init is None):
        return Change(file, qualname, skip=f"{cls_node.name}.__init__ was added or removed")
    if h_init is None:
        ctor = []
    else:
        ctor = _reconcile(_sig_params(b_init, True), _sig_params(h_init, True),
                          len(h_init.args.defaults))
        if ctor is None:
            return Change(file, qualname,
                          skip=f"{cls_node.name}.__init__ " + _sig_msg(b_init, h_init, True))
    params = _reconcile(_sig_params(base_node, True), _sig_params(node, True),
                        len(node.args.defaults))
    if params is None:
        return Change(file, qualname, skip=_sig_msg(base_node, node, True))
    kind = "property" if "property" in decs else "instance"
    return Change(file, qualname, kind=kind, params=params, ctor_params=ctor)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


# Callers pulled in per file when a helper they call changed. Capped because the
# match is by name: an attribute access happens to look like a call to a method
# of the same name, and probing a few extra callables is cheaper than resolving
# that properly.
CALLER_LIMIT = 4


def _refs(node) -> set[str]:
    """Every bare name and attribute name mentioned in a body."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


CALL_LIMIT = 3              # constructions harvested per class


def _literal(n) -> bool:
    """An expression that evaluates to the same value anywhere, looking up no
    name at all."""
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
        return all(_literal(e) for e in n.elts)
    if isinstance(n, ast.Dict):
        return all(k is not None and _literal(k) for k in n.keys) and \
            all(_literal(v) for v in n.values)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        return _literal(n.operand)
    return False


def _literal_args(node) -> str | None:
    """The argument source of a call, when every argument is a literal.

    Anything else -- a fixture, an attribute, a local -- means the call is not
    an expression outside the test that wrote it, and borrowing it would give
    the two sides different values or no value at all.
    """
    parts = []
    for a in node.args:
        if not _literal(a):
            return None
        parts.append(ast.unparse(a))
    for kw in node.keywords:
        if kw.arg is None or not _literal(kw.value):
            return None
        parts.append(f"{kw.arg}={ast.unparse(kw.value)}")
    return ", ".join(parts)


def _fixture_rank(v):
    """A signed token first, an assertion about an error message last.

    The suite's long literals are a mix of the two and only one of them gets
    past a signature check: `'[42].-9cNi0CxsSB3hZPNCe9a2eEs1ZM'` is a payload,
    a separator and a digest, while `'not supported'` is prose quoted from an
    exception. Whitespace is what tells them apart, and a separator is what
    says the rest of it has structure. Longer breaks the tie, because a token
    carries a digest and a message does not.
    """
    text = v.decode("latin-1") if isinstance(v, bytes) else v
    return (any(c.isspace() for c in text),
            not any(c in text for c in "._-:$"),
            -len(text))


def _fixtures(repo, rev, wanted: set[str], per_type: int = 6,
              min_len: int = 12, max_len: int = 400):
    """Literals and literal constructions from the repository's own tests.

    No corpus of edge values will produce "value.TgPVoaGhoQ.AGBfQ6G6cr07byTRt0z"
    -- a signed payload whose timestamp is years old -- and none guesses a
    separator that a constructor validating its arguments will accept. The test
    suite has both, written by someone who knew what a valid one looks like.

    A literal is the identical value on both sides by construction. A harvested
    call is code, so it is only taken when its arguments are literals too.

    Only tests that mention something that changed are read.

    Returns (literals by type name, constructions by callee name).
    """
    lits, calls = {}, {}
    for f in git(repo, "ls-tree", "-r", "--name-only", rev).split("\n"):
        if not f.endswith(".py") or not is_test_path(f):
            continue
        src = git(repo, "show", f"{rev}:{f}", check=False)
        if not src or not (wanted & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", src))):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute)):
                # partial(TimestampSigner, secret_key="secret-key") is a
                # construction; it is how a pytest fixture writes one.
                # A suite that imports the package rather than the names in it
                # writes click.Option(["--x"]), and taking only the bare form
                # threw away every construction click's own tests perform. The
                # name is kept and the qualifier dropped, because the caller
                # only keeps names that are classes of the module being probed,
                # where the bare name is what resolves.
                name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                node = n
                if name == "partial" and n.args and isinstance(n.args[0], ast.Name):
                    name = n.args[0].id
                    node = ast.Call(func=n.func, args=n.args[1:], keywords=n.keywords)
                args = _literal_args(node)
                if args is None:
                    continue
                got = calls.setdefault(name, [])
                expr = "%s(%s)" % (name, args)
                if expr not in got and len(got) < CALL_LIMIT:
                    got.append(expr)
                continue
            if not isinstance(n, ast.Constant) or not isinstance(n.value, (str, bytes)):
                continue
            if not min_len <= len(n.value) <= max_len:
                continue
            key = "str" if isinstance(n.value, str) else "bytes"
            got = lits.setdefault(key, [])
            # Collected past the cap and ranked below: taking the first six the
            # walk happens to reach is how three error messages beat the one
            # signed token in the file.
            if n.value not in got and len(got) < per_type * 8:
                got.append(n.value)
    return ({k: [repr(x) for x in sorted(v, key=_fixture_rank)[:per_type]]
             for k, v in lits.items()}, calls)


SIBLING_LIMIT = 12          # modules read from a package to look for producers


def _imported(src: str) -> set[str]:
    """Names the module has bound by importing them. A producer expression is
    only worth emitting if the name it starts with resolves here."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {a.asname or a.name.split(".")[0] for a in n.names}
    return out


def _head(expr: str) -> str:
    return expr.split("(")[0].split(".")[0]


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(repo, base, head, f: str) -> dict[str, list[int]]:
    """The line numbers the commit touched in this file, per side.

    Calling a changed callable is not the same as reaching the change inside it.
    A probe that runs the function but never executes one of these lines could
    not have been affected by the edit, and counting it as coverage overstates
    what the sweep actually verified.
    """
    out = {"base": [], "head": []}
    for line in git(repo, "diff", "-U0", base, head, "--", f, check=False).split("\n"):
        m = HUNK.match(line)
        if not m:
            continue
        bs, bn, hs, hn = (int(x) if x else 1 for x in m.groups())
        out["base"].extend(range(bs, bs + bn))
        out["head"].extend(range(hs, hs + hn))
    return out


def _own(node, lines) -> list[int]:
    """The moved lines that lie inside one callable.

    A hunk is read off the file, so every callable in it was handed the whole
    file's moved lines and counted a probe as coverage for running any of them.
    Probing a method builds the instance first, and a commit that touches
    `__init__` alongside a method therefore had every method on the class
    reporting that it reached a change it never executed. Ask each callable for
    its own lines instead.
    """
    end = node.end_lineno or node.lineno
    return [n for n in lines if node.lineno <= n <= end]


def _guard_key(n) -> str | None:
    """The probe column a guard names: a bare parameter, or a `self` attribute
    a constructor conventionally sets from one of its own."""
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
            and n.value.id == "self":
        return n.attr
    return None


def _hints(node, lines, cap: int = 3) -> list[str]:
    """String and bytes literals written on the lines the commit moved.

    A guard mines the constant a branch demands. This mines the constant the
    changed code operates on, which is the other half: click's revert dropped
    the colon escaping from `item.value.replace(":", "\\:")`, and no corpus of
    edge values contains a string with a colon in it, so both revisions agreed
    on every probe that ran. The literal is sitting on the moved line.

    Short ones first: a separator or an escape is the sort of thing a value has
    to contain for the changed line to do anything, and a paragraph of prose in
    an error message is not.
    """
    want = set(lines)
    seen = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Constant) or not isinstance(n.value, (str, bytes)):
            continue
        if n.lineno not in want or not 1 <= len(n.value) <= 24:
            continue
        if (r := repr(n.value)) not in seen:
            seen.append(r)
    return sorted(seen, key=len)[:cap]


def _guards(node, lines) -> dict[str, list[str]]:
    """Literals a name has to hold for one of `lines` to execute.

    No corpus of edge values guesses the constant in `if version == 0x8f`, so
    the probe calls the changed function and stops at the branch standing in
    front of the change. That is most of what "no probe reached the change"
    means, and the constant is sitting in the source: read it off the branch
    enclosing the moved lines and try it first.

    Only `==` and `in` are mined. A `<` names a direction rather than a value,
    and the corpus already carries both ends of the range.
    """
    want, out = set(lines), {}
    for n in ast.walk(node):
        if not isinstance(n, (ast.If, ast.While)):
            continue
        body = {i for s in n.body
                for i in range(s.lineno, (s.end_lineno or s.lineno) + 1)}
        if not want & body:
            continue
        for c in ast.walk(n.test):
            if not isinstance(c, ast.Compare) or len(c.ops) != 1:
                continue
            op, left, right = c.ops[0], c.left, c.comparators[0]
            if isinstance(op, ast.Eq):
                pairs = ((left, right), (right, left))   # either side can be it
            elif isinstance(op, ast.In):
                pairs = ((left, right),)                 # `in` is not symmetric
            else:
                continue
            for name, val in pairs:
                key = _guard_key(name)
                if key is None or not _literal(val):
                    continue
                # `x in (a, b)` wants an element, not the container. A string
                # is left whole: it is in itself.
                vals = [ast.unparse(e) for e in val.elts] \
                    if isinstance(op, ast.In) and isinstance(val, (ast.List, ast.Tuple, ast.Set)) \
                    else [ast.unparse(val)]
                for v in vals:
                    if v not in out.setdefault(key, []):
                        out[key].append(v)
    return out


DOC_HOLDERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _shape(node) -> str:
    """`ast.dump` cut down to what a twin run can observe.

    Docstrings and annotations are both nodes, so editing either makes the AST
    differ and puts the callable in the blast radius, where it spends a full
    probe budget proving that nothing an output comparison can see has moved.
    Neither is an executed line. A docstring is a constant nothing here reads,
    and an annotation is a string under `from __future__ import annotations` or
    else evaluated once at import; the twin run compares return values,
    exceptions, stdout and mutation, not `__doc__` or `__annotations__`.

    Annotations are dropped from parameters and return types only. The one on a
    class-level assignment stays, because a dataclass field annotation is not a
    comment on the behaviour, it is the behaviour. Parameter names go the same
    way, into positional slots.
    """
    node = copy.deepcopy(node)

    # A parameter name is not observable through a positional call, and a
    # positional call is the only kind a twin run makes: `loads(s)` and
    # `loads(payload)` are the same function to it. Renaming to slots keeps a
    # rename out of the radius, and the body has to move with the signature --
    # the name appears in both. Renaming by position rather than by identity is
    # what makes it safe: `f(a, b) -> a` and `f(b, a) -> b` collapse, and they
    # should, because `f(1, 2)` is 1 on both sides.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        slots = {p.arg: f"_p{i}" for i, p in enumerate(
            [*a.posonlyargs, *a.args, *([a.vararg] if a.vararg else []),
             *a.kwonlyargs, *([a.kwarg] if a.kwarg else [])])}
        for n in ast.walk(node):
            if isinstance(n, ast.arg) and n.arg in slots:
                n.arg = slots[n.arg]
            # A nested scope that shadows the name is renamed too. That makes
            # the two sides differ where they might not have, which leaves the
            # callable in the radius -- the safe direction to be wrong in.
            elif isinstance(n, ast.Name) and n.id in slots:
                n.id = slots[n.id]

    for n in ast.walk(node):
        if isinstance(n, DOC_HOLDERS) and ast.get_docstring(n) is not None:
            n.body = n.body[1:] or [ast.Pass()]
        if isinstance(n, ast.arg):
            n.annotation = None
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.returns = None
    return ast.dump(node)


def changed_functions(repo, base, head, include_tests: bool = False) -> list[Change]:
    """Callables present in both revisions whose AST differs.

    Present-in-both is the requirement, not an approximation: a callable with no
    twin has nothing to be compared against.
    """
    out = []
    names = [n for n in git(repo, "diff", "--name-only", base, head).split("\n")
             if n.endswith(".py")]

    # What moved, across every changed file. A producer that changed anywhere is
    # unusable everywhere: it would hand the two sides different inputs.
    seen, moved = {}, set()
    for f in names:
        if not include_tests and is_test_path(f):
            out.append(Change(f, f, skip="test file, pass --include-tests to probe it"))
            continue
        b = git(repo, "show", f"{base}:{f}", check=False)
        h = git(repo, "show", f"{head}:{f}", check=False)
        if not b or not h:
            continue
        bt, ht = _targets(b), _targets(h)
        pairs = sorted(set(bt) & set(ht))
        hit = [q for q in pairs if _shape(bt[q][0]) != _shape(ht[q][0])]
        seen[f] = (b, h, bt, ht, pairs, hit)
        moved |= set(hit) | {q.rsplit(".", 1)[-1] for q in hit}

    if not moved:
        return out
    # A literal is the same value whichever revision it was read from, so the
    # fixtures come from head, where the commit's own new test may have added
    # one. A harvested construction is code, and code comes from base for the
    # same reason producers do.
    fixtures, _ = _fixtures(repo, head, moved)
    _, tcalls = _fixtures(repo, base, moved)
    tree_cache = {}

    for f, (b, h, bt, ht, pairs, hit) in seen.items():
        if not hit:
            continue
        # Both sides have to agree that a decorator is transparent. One that
        # started wrapping in head changes how the name is called, which is a
        # difference the probe cannot attribute to the body.
        clear = frozenset(_transparent(b) & _transparent(h))
        # Producers are read from the base revision, never the head one. A
        # function that only exists in head raises NameError on one side and
        # returns a value on the other, which is a delta on every callable that
        # consumes its type -- a false positive machine. Anything present in
        # base and absent from `moved` is identical in both.
        sibs = _siblings(repo, base, f, tree_cache)
        owners: dict[str, str] = {}
        ctors = _ctor_map([b] + [x for x in sibs if x != b], owners)

        # Extracting a helper leaves its callers byte-identical while their
        # behaviour moves underneath them. Probe one level of those too: the
        # helper's own finding names a private function, the caller's names the
        # thing anyone actually calls.
        tails = {q.rsplit(".", 1)[-1] for q in hit}
        callers = [q for q in pairs
                   if q not in hit and _refs(ht[q][0]) & tails][:CALLER_LIMIT]

        # The constructor's own probe says what the instance became; the methods
        # say what that costs. No method body names __init__, so `_refs` cannot
        # find them and they have to be taken by class -- without which a commit
        # touching nothing else is a single finding with nothing behind it.
        for cls in sorted({q[:-9] for q in hit if q.endswith(".__init__")}):
            done = set(hit) | set(callers)
            callers += [q for q in pairs
                        if q not in done and q.startswith(cls + ".")
                        and not q.endswith(".__init__")][:CALLER_LIMIT]

        # The exclusion is off for this module's own producers: they are read
        # from base and frozen to base's value before either side runs, so a
        # producer the commit changed still hands both sides the same input --
        # base's, which is the revision being trusted. What it cannot do is run
        # unfrozen, so the ones the exclusion used to drop are tracked and
        # pulled back out if the freeze does not take.
        seek = []
        made = _producers(b, set(), ctors, fixtures, calls=tcalls, unknown=seek)
        safe = {e for v in _producers(b, moved, ctors, fixtures,
                                      calls=tcalls).values() for _, e in v}
        # Relaxed for the two types a corpus cannot write down, and nothing
        # else. A signed token has to come from the code that signs it; an int
        # does not, and eight edge values beat any producer of one. A project
        # type is worse than useless relaxed -- an instance has no literal form,
        # so it would freeze into nothing and take the whole probe out with it.
        for ann, v in made.items():
            if set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ann)) - {"str", "bytes"}:
                made[ann] = [x for x in v if x[1] in safe]
        risky = {e for v in made.values() for _, e in v} - safe

        # A construction the tests perform is a producer of its own class: a
        # parameter annotated `Signer` gets one the suite already proved valid,
        # instead of a corpus guess the constructor refuses.
        for cls, exprs in tcalls.items():
            if cls not in ctors or f"{cls}.__init__" in moved:
                continue
            for e in exprs:
                _produced(made, cls, cls, e, per_type=CALL_LIMIT)

        # The producer is often in the module the consumer imported it from:
        # timed.py takes what signer.py signs. Read the siblings this file
        # actually imports from, and keep only what it can name.
        bound = _imported(b)
        for sib in sibs:
            if sib == b:
                continue
            for ann, exprs in _producers(sib, moved, ctors, fixtures,
                                         calls=tcalls).items():
                for ident, e in exprs:
                    if _head(e) in bound:
                        _produced(made, ann, ident, e, per_type=8)

        # Two fixtures ahead of the producers, the rest behind. A literal from
        # the test suite is the input someone who knew the format wrote down,
        # and appending it left it below the cut every time; putting all of it
        # in front would starve the producer a round-trip needs, so it splits.
        for k, v in fixtures.items():
            lit = [("fixture", e) for e in v]
            made[k] = lit[:2] + made.get(k, []) + lit[2:]
        made = {k: [e for _, e in v] for k, v in made.items()}
        touched = _hunks(repo, base, head, f)
        for qual in hit + callers:
            ch = _describe(f, qual, *ht[qual], *bt[qual], transparent=clear)
            ch.is_async = isinstance(ht[qual][0], ast.AsyncFunctionDef)
            ch.ctors, ch.producers = ctors, made
            # A caller is in the radius for a change it does not contain, so it
            # keeps the file's whole set: reaching the helper's edit is the
            # coverage it is here for. Everyone else answers for its own lines.
            ch.lines = touched if qual in callers else {
                "base": _own(bt[qual][0], touched["base"]),
                "head": _own(ht[qual][0], touched["head"])}
            # Both revisions, for the same reason the hints take both: the
            # branch standing in front of a removed line is in base, and
            # reaching the edit on either side is coverage of it.
            gh = _guards(ht[qual][0], touched["head"])
            gb = _guards(bt[qual][0], touched["base"])
            ch.guards = {k: gh.get(k, []) + [x for x in gb.get(k, [])
                                             if x not in gh.get(k, [])]
                         for k in set(gh) | set(gb)}
            # Both sides: a commit that removes a line leaves the literal it
            # operated on only in base, and a revert is nothing but removals.
            ch.hints = _hints(ht[qual][0], touched["head"]) + \
                [x for x in _hints(bt[qual][0], touched["base"])
                 if x not in _hints(ht[qual][0], touched["head"])]
            ch.risky = risky
            ch.unknown = seek
            # A changed constructor disqualifies the harvested constructions
            # of the classes that inherit it: __init__ is resolved through the
            # bases, so the one that moved is not always the one named on the
            # class. It disqualifies nothing else. `moved` holds the bare tail
            # of every changed method as well as its qualified name, so asking
            # it for "__init__" was asking whether any constructor in the whole
            # commit had changed -- and a commit that touches one takes the test
            # suite's constructions away from every class in the package.
            cls = qual.split(".")[0] if "." in qual else ""
            if cls and f"{owners.get(cls, cls)}.__init__" not in moved:
                ch.built = list(tcalls.get(cls, []))
            out.append(ch)
    return out


def _siblings(repo, head, f: str, cache: dict) -> list[str]:
    """Sources of the other modules in this file's directory."""
    d = f.rsplit("/", 1)[0] if "/" in f else ""
    if d in cache:
        return cache[d]
    listed = git(repo, "ls-tree", "--name-only", f"{head}:{d}" if d else head).split("\n")
    srcs = []
    for n in listed[:SIBLING_LIMIT * 3]:
        if not n.endswith(".py") or is_test_path(n):
            continue
        src = git(repo, "show", f"{head}:{d}/{n}" if d else f"{head}:{n}", check=False)
        if src:
            srcs.append(src)
        if len(srcs) >= SIBLING_LIMIT:
            break
    cache[d] = srcs
    return srcs


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def _ctor_exprs(name: str, ctors: dict, limit: int = 3, aliases: dict | None = None,
                calls: dict | None = None, depth: int = CTOR_DEPTH,
                producers: dict | None = None, hints: list | None = None) -> list[str]:
    """Source expressions that build an instance of a local class, recursively:
    a constructor that itself wants a project type gets one built, down to
    CTOR_DEPTH. Stopping at the first level is what a framework's own types
    defeat -- click's Context takes a Command, Option lives on a Parameter --
    and the fallback for an unbuildable parameter is a no-argument call, which
    raises in setup and takes every probe for that callable with it.

    A construction the test suite already performs comes first. Signer rejects
    every separator the corpus contains -- ASCII letters, digits and -_= are
    all refused -- so the guessed constructions all die in setup while the
    tests, four lines away, hold one that works."""
    real = list((calls or {}).get(name, []))
    params = ctors.get(name)
    if not params:
        return real[:limit] or [f"{name}()"]
    cols = []
    for pname, pann in params:
        v = _values(_expand(pann, aliases or {}),
                    ctors if depth > 0 else None, producers,
                    depth=depth - 1, aliases=aliases, calls=calls, hints=hints)
        if v is None:
            return [f"{name}()"]
        cols.append(v)
    out = []
    # One variant per corpus index rather than the product: a constructor that
    # rejects the empty string usually accepts the value next to it, and the
    # product only ever varies the last parameter.
    for i in range(limit):
        out.append("%s(%s)" % (name, ", ".join(c[i % len(c)] for c in cols)))
    # Those advance in lockstep, and the columns are different lengths, so a
    # literal that has to appear in two parameters at once is paired with itself
    # only by luck of index. A hint is the constant the changed line operates
    # on, so spend one variant putting it in every column that will take it:
    # click's zsh formatter escapes a colon in the value if and only if the help
    # is not the sentinel, and `CompletionItem(':', ':', ':')` is the call that
    # separates the two revisions.
    hv = ["%s(%s)" % (name, ", ".join(h if h in c else c[0] for c in cols))
          for h in hints or [] if any(h in c for c in cols)]
    return list(dict.fromkeys(real + hv + out))[:max(limit, len(real)) + len(hv)]


def _is_type_expr(n) -> bool:
    """A syntactic test for something written as a type. Cheaper and stricter
    than guessing from the names in it: `_t_secret_key` is built out of other
    aliases and mentions no builtin at all."""
    if isinstance(n, ast.Name):
        return True
    if isinstance(n, (ast.Attribute, ast.Subscript)):
        return _is_type_expr(n.value)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        return _is_type_expr(n.left) and _is_type_expr(n.right)
    if isinstance(n, ast.Constant):
        return n.value is None
    if isinstance(n, (ast.Tuple, ast.List)):
        return bool(n.elts) and all(_is_type_expr(e) for e in n.elts)
    return False


def _aliases(tree) -> dict[str, str]:
    """Module-level type aliases. Typed code is full of `_t_str_bytes = str |
    bytes`, and left unexpanded it reads as a project type nobody can build --
    and never matches the `str | bytes` the interpreter reports for the
    parameter that consumes it."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            name, value = n.targets[0].id, n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                and n.value is not None:
            name, value = n.target.id, n.value
        else:
            continue
        try:
            text = ast.unparse(value)
        except Exception:
            continue
        if _is_type_expr(value):
            out[name] = text
    return out


# Names that describe a type without being one. The type in Optional[Signer] is
# Signer, and the module qualifier in t.Optional is not a type at all.
TYPING = {"Optional", "Union", "List", "Dict", "Tuple", "Set", "Sequence",
          "Iterable", "Iterator", "Mapping", "Callable", "Type", "Final",
          "Annotated", "Literal", "ClassVar", "None"}
QUALIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.")


def _expand(ann: str, aliases: dict, depth: int = 4) -> str:
    """Substitute aliases wherever they appear, not just when the whole
    annotation is one: _t_secret_key is written in terms of _t_str_bytes."""
    for _ in range(depth):
        nxt = re.sub(r"[A-Za-z_][A-Za-z0-9_]*",
                     lambda m: aliases.get(m.group(0), m.group(0)), ann)
        if nxt == ann:
            break
        ann = nxt
    return ann


def _corpus_typed(ann: str) -> bool:
    """True when the corpus can fill this parameter without inventing a type."""
    if not ann:
        return True
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", QUALIFIER.sub("", ann))
    return any(n.lower() in CORPUS or n == "Any" for n in names)


def _produced(out, ann, ident, expr, per_type):
    """Budget producers by identity, not by expression. Two classes overriding
    sign() are two producers worth trying; the third class implementing
    get_signature() is not a third idea, and letting it fill the budget crowds
    out the producer that was actually worth calling."""
    got = out.setdefault(ann, [])
    if any(e == expr for _, e in got):
        return
    method = ident.rsplit(".", 1)[-1]
    if len({i for i, _ in got if i.rsplit(".", 1)[-1] == method} - {ident}) >= 2:
        return
    # Nor may one producer spend the budget on variants of itself. Four ways of
    # calling TimestampSigner.sign is one idea; it was crowding out Signer.sign,
    # which is the producer the consumer was actually written against.
    if sum(1 for i, _ in got if i == ident) >= 2:
        return
    if len(got) < per_type:
        got.append((ident, expr))


def _args_from(params, drop_first: bool, fixtures: dict | None = None,
               aliases: dict | None = None):
    """Argument lists for calling a producer, or nothing when one of its own
    parameters needs a type the corpus cannot fill -- a producer is only worth
    calling when calling it is trivial.

    Two lists at most: one from the corpus, and one that feeds the producer a
    fixture from the test suite. Interesting products come from interesting
    inputs, and the test suite is where those live."""
    plain, fixed = [], []
    for pname, pann in params[1:] if drop_first else params:
        pann = _expand(pann, aliases or {})
        if not _corpus_typed(pann):
            return []
        vals = _values(pann)
        if not vals:
            return []
        plain.append(vals[0])
        f = _made(pann, fixtures, 1)
        fixed.append(f[0] if f else vals[0])
    return [plain] if plain == fixed else [plain, fixed]


def _producers(src: str, exclude: set[str], ctors: dict | None = None,
               fixtures: dict | None = None, per_type: int = 4,
               calls: dict | None = None,
               unknown: list | None = None) -> dict[str, list[str]]:
    """Module functions that make a value of some type, as call expressions.

    A corpus of edge values cannot build a signed token, a parsed config or an
    open connection. The module that consumes one almost always contains the
    function that makes one, and calling it is how a person writes the test.

    Anything that changed between the revisions is excluded, directly or by
    reference: a producer whose own behaviour moved would hand the two sides
    different inputs, and then nothing being compared means anything.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}
    al = _aliases(tree)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            if n.name in exclude or (n.returns is None and unknown is None):
                continue
            if _bad_sig(n) or _refs(n) & exclude:
                continue
            for args in _args_from(_sig_params(n, False), False, fixtures, al):
                expr = "%s(%s)" % (n.name, ", ".join(args))
                # Nothing in the source says what an unannotated function makes.
                # Park it: one call in base names the type and the value at once.
                if n.returns is None:
                    unknown.append((n.name, expr))
                else:
                    _produced(out, _expand(ast.unparse(n.returns), al), n.name,
                              expr, per_type)

        # Most real code puts the producer on the same class as the consumer:
        # loads takes what dumps made. Reaching those needs an instance, which
        # is the same one-level construction a project-typed parameter gets.
        elif isinstance(n, ast.ClassDef) and ctors:
            if n.name not in ctors or f"{n.name}.__init__" in exclude:
                continue
            # Two constructions, not one: plenty of constructors reject the
            # empty string outright and take the value next to it.
            insts = _ctor_exprs(n.name, ctors, limit=2, aliases=al, calls=calls)
            for m in n.body:
                if not isinstance(m, ast.FunctionDef):
                    continue
                if m.returns is None and unknown is None:
                    continue
                if m.decorator_list or m.name.startswith("__"):
                    continue
                if f"{n.name}.{m.name}" in exclude or m.name in exclude:
                    continue
                if _bad_sig(m) or _refs(m) & exclude:
                    continue
                for args in _args_from(_sig_params(m, False), True, fixtures, al):
                    for inst in insts:
                        ident = "%s.%s" % (n.name, m.name)
                        expr = "%s.%s(%s)" % (inst, m.name, ", ".join(args))
                        if m.returns is None:
                            unknown.append((ident, expr))
                        else:
                            _produced(out, _expand(ast.unparse(m.returns), al),
                                      ident, expr, per_type)
    return out


UNION = {"Union", "Optional", "None", "NoneType"}


def _typekey(ann: str) -> frozenset[str]:
    """An annotation in a form two spellings of it can be compared in.

    Producers are keyed by what the parse tree said and parameters ask by what
    the interpreter resolved, so `_t.Union[str, bytes]` and `str | bytes` were
    two unrelated strings and the module's own `dumps` was invisible to its own
    `loads`. A union is its members, because a producer of either one fills it.
    Anything else is itself: `Iterator[Signer]` is not a Signer.
    """
    try:
        node = ast.parse(QUALIFIER.sub("", ann.strip()), mode="eval").body
    except (SyntaxError, ValueError):
        return frozenset()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _typekey(ast.unparse(node.left)) | _typekey(ast.unparse(node.right))
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
            and node.value.id in UNION:
        arg = node.slice
        parts = arg.elts if isinstance(arg, ast.Tuple) else [arg]
        return frozenset().union(*(_typekey(ast.unparse(e)) for e in parts))
    name = ast.unparse(node)
    return frozenset() if name in UNION else frozenset({name})


def _made(ann: str, producers: dict | None, limit: int = 4) -> list[str]:
    """Producer expressions for this annotation. A producer returning `bytes`
    fills a parameter annotated `str | bytes`, which is how half of a typed
    codebase is written."""
    if not producers:
        return []
    out = list(producers.get(ann, []))
    want = _typekey(ann)
    for k, v in producers.items():
        if k != ann and want & _typekey(k):
            out += v
    return out[:limit]


GUESSED = 3         # producer values offered per type to an unannotated parameter


def _values(ann: str, ctors: dict | None = None,
            producers: dict | None = None, depth: int = CTOR_DEPTH,
            aliases: dict | None = None, calls: dict | None = None,
            hints: list | None = None) -> list[str] | None:
    if not ann:
        # An unannotated parameter used to see nothing but the spread, which
        # threw away every producer and fixture the module had. A codebase with
        # no type hints is one where that is all of them, and the spread is the
        # part that cannot work: the value that gets past a signature check is
        # the token the test suite wrote down, never `0`. They go in front,
        # because the spread lands on the same TypeError on both sides and
        # reports nothing whatever order it is tried in.
        made = (_made("str", producers, GUESSED)
                + _made("bytes", producers, GUESSED))
        return made + list(hints or []) + UNTYPED
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", QUALIFIER.sub("", ann))
    optional = "None" in names or "Optional" in names
    # `none` is a corpus type, so the None in `Context | None` was answering for
    # the whole annotation: the search below walks the names in order, misses
    # Context, and settles on the None that only ever meant optional. Every
    # parameter typed as an optional project type was probed with None and
    # nothing else -- which is why click's ProgressBar, whose iterable is
    # `Iterable[V] | None`, raised in setup for all thirteen of its methods.
    if len(names) > 1:
        names = [n for n in names if n != "None"]
    if "Any" in names:
        # `Any` says nothing on its own. But `dict[str, Any]` and `IO[Any]` say
        # plenty: the type argument is not the type, and taking the whole thing
        # for untyped throws away the container -- which is how a file parameter
        # ends up being probed with `0` and raising on the first line every time.
        # Drop it only when something modelled is left underneath.
        rest = [n for n in names if n != "Any"]
        if rest and any(n.lower() in CORPUS or (ctors and n in ctors) for n in rest):
            names = rest
        else:
            # a type that says nothing gets the spread, and whatever the moved
            # line was written to operate on
            return list(hints or []) + UNTYPED
    made = _made(ann, producers)
    for n in names:
        if n.lower() in CORPUS:
            lead = [h for h in (hints or [])
                    if n.lower() in ("str", "bytes") and h not in CORPUS[n.lower()]]
            vals = lead + list(CORPUS[n.lower()]) + made
            return vals + ["None"] if optional else vals
    real = [n for n in names if n not in TYPING]
    if not real:
        return None
    # Optional[Signer] is a Signer. Prefer a class this repository defines.
    head = next((n for n in real if ctors and n in ctors), real[-1])
    if made:
        return made + ["None"] if optional else made
    if ctors is not None and head in ctors:
        vals = _ctor_exprs(head, ctors, aliases=aliases, calls=calls,
                           depth=depth, producers=producers, hints=hints)
        # None goes first here, not last. A class that wants another of its own
        # kind -- click's Context has a Context parent -- recurses until the
        # depth runs out and bottoms out on the no-argument call below, which
        # raises for a missing required argument; nested inside an otherwise
        # good construction that guess takes the whole of it down, and all four
        # probes with it. A parameter that accepts None always has one probe
        # that works, and the built instances still follow it.
        return ["None"] + vals if optional else vals
    # An imported or unknown type, or one the recursion has no depth left to
    # build. Try the no-argument constructor: the name is resolved in the target
    # module's own namespace, and if it cannot be built the failure is identical
    # on both sides. None is not a guess, so an optional parameter takes it.
    return ["None"] if optional else [f"{head}()"]


def make_probes(change: Change, limit: int, seed: int = 0):
    """One probe is the constructor arguments followed by the call arguments."""
    cols = []
    inst = change.instances
    if inst:
        cols.append(inst)
    for pname, ann in [*([] if inst else change.ctor_params), *change.params]:
        v = _values(ann, change.ctors, change.producers, hints=change.hints)
        if v is None:
            return None, f"unmodelled type {ann!r} on {pname}"
        # A guard literal goes to the front. The sampler covers index 0 of every
        # column in its first probe, so one probe carries every constant at once
        # -- which is what a guard reading `a == 1 and b == 2` needs.
        g = [x for x in change.guards.get(pname, []) if x not in v]
        cols.append(g + v)
    if not cols:
        return [[]], None
    total = 1
    for c in cols:
        total *= len(c)
    if total <= limit:
        return [list(p) for p in itertools.product(*cols)], None
    # Cover every value in every column at least once before sampling the rest.
    # Sampling the product at random can drop an edge value entirely, and the
    # edge values are the ones that find things.
    seen, out = set(), []
    for i in range(min(limit, max(len(c) for c in cols))):
        cand = tuple(c[i % len(c)] for c in cols)
        if cand not in seen:
            seen.add(cand)
            out.append(list(cand))

    # Then spend what is left one factor at a time: vary one column and hold the
    # others at their first value, which is the one most likely to work -- a
    # producer's, where there is one. The diagonal above advances every column
    # together, so a signed token is paired with a garbage max_age and the call
    # dies before the signature is even checked. It has to stay first: a change
    # that only shows up when two parameters are both interesting is invisible
    # to a sweep that holds one of them at zero.
    base = [c[0] for c in cols]
    for i in range(max(len(c) for c in cols)):
        if len(out) >= limit:
            break
        for j, col in enumerate(cols):
            if i >= len(col) or len(out) >= limit:
                continue
            cand = tuple(col[i] if k == j else base[k] for k in range(len(cols)))
            if cand not in seen:
                seen.add(cand)
                out.append(list(cand))

    rng = random.Random(seed)
    for _ in range(limit * 30):
        if len(out) >= limit:
            break
        cand = tuple(rng.choice(c) for c in cols)
        if cand in seen:
            continue
        seen.add(cand)
        out.append(list(cand))
    return out, None


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _invoke(worktree: Path, payload: dict, timeout: float, tmp: Path):
    out = tmp / "res.json"
    out.unlink(missing_ok=True)
    payload = {**payload, "root": str(worktree), "out": str(out)}
    try:
        subprocess.run(
            [sys.executable, str(CHILD)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(worktree),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    if not out.exists():
        return None, "child died without writing results"
    data = json.loads(out.read_text())
    if "error" in data:
        return None, data["error"]
    return data, None


def run_side(side: str, worktree: Path, change: Change, probes, timeout: float,
             tmp: Path):
    """Results for each probe, and whether each one executed a line the commit
    touched. Reached-ness is kept out of the result dict: it is a property of the
    run, not of the answer, and comparing it would report a delta of its own."""
    data, err = _invoke(worktree, {
        "file": change.file,
        "qualname": change.qualname,
        "kind": change.kind,
        "is_async": change.is_async,
        "n_ctor": 1 if change.instances else len(change.ctor_params),
        "built": bool(change.instances),
        "lines": change.lines.get(side, []),
        "probes": probes,
    }, timeout, tmp)
    if data is None:
        return None, None, err
    return data["results"], data.get("reached") or [], None


def _const(text: str) -> bool:
    """Already a value, so there is nothing to evaluate."""
    try:
        ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def resolve_producers(worktree: Path, change: Change, timeout: float, tmp: Path):
    """Call every producer once in base and keep what it made.

    Two answers come out of the one run. A value that freezes to a literal can
    be handed to both sides as it stands, which is what lets the commit have
    touched the producer at all: what reaches head is base's answer, not head's
    code. And a producer with no return annotation gets a type -- it is whatever
    calling it gave back, which is written nowhere in a codebase from before
    anyone typed it.

    Whatever will not freeze keeps its call expression, and that is only allowed
    for a producer the commit left alone. The rest goes: it is exactly the input
    that would have differed between the two sides.
    """
    pool = {e for v in change.producers.values() for e in v if not _const(e)}
    pool |= {e for _, e in change.unknown}
    if not pool:
        return
    data, _ = _invoke(worktree, {"mode": "freeze", "file": change.file,
                                 "exprs": sorted(pool)}, timeout, tmp)
    got = (data or {}).get("frozen") or {}
    out = {}
    for ann, exprs in change.producers.items():
        out[ann] = [got[e][0] if e in got else e
                    for e in exprs if e in got or e not in change.risky]
    # The unannotated ones file themselves under the type they turned out to
    # make, behind everything the annotations already offered: a declared type
    # is a claim about every call, and one observed value is a claim about one.
    kept = {}
    for ident, e in change.unknown:
        if e not in got:
            continue
        lit, tname = got[e]
        col = out.setdefault(tname, [])
        if lit in col or kept.get(ident, 0) >= 2 or kept.get(tname, 0) >= CALL_LIMIT:
            continue
        kept[ident] = kept.get(ident, 0) + 1
        kept[tname] = kept.get(tname, 0) + 1
        col.append(lit)
    change.producers = out


def signatures(worktree: Path, change: Change, timeout: float, tmp: Path):
    """Real parameter lists for the target and its constructor, from the running
    interpreter rather than the parse tree."""
    data, err = _invoke(worktree, {
        "mode": "introspect",
        "file": change.file,
        "qualname": change.qualname,
        "kind": change.kind,
    }, timeout, tmp)
    if data is None:
        return None, None, err
    if data["params"] is None:
        return None, None, data.get("reason", "no signature")
    return data["params"], data.get("ctor") or [], None


def _agree(bsig, hsig, required_only: bool):
    """Reconcile two introspected parameter lists.

    A constructor is called only to get an instance, so inventing values for its
    optional parameters mostly fails to build anything and the default is what
    real calls use. A target's own optional parameters are the opposite: they are
    often exactly where the behaviour moved.
    """
    keep = _reconcile([(n, a) for n, a, _ in bsig],
                      [(n, a) for n, a, _ in hsig],
                      sum(1 for _, _, d in hsig if d))
    if keep is None:
        return None
    trimmed = hsig[:len(keep)]
    return [(n, a) for n, a, d in trimmed if not (required_only and d)]


def _sweep(ch: Change, probes, bw: Path, hw: Path, repeats: int, timeout: float,
           td: Path):
    """Run both sides on these probes and tally the result.

    The repeats interleave the two sides rather than running each side's to
    completion, which is what makes the flake filter see wall-clock drift. A
    producer that embeds a timestamp -- signing a token, stamping a record --
    gives the same answer twice in a row and a different one a second later, so
    back-to-back base runs agree with each other, the head runs agree with each
    other, and the two sides disagree for a reason that has nothing to do with
    the commit. Interleaved, each side straddles the same window, and the drift
    shows up where it belongs: as a side disagreeing with itself.
    """
    runs, reached = {}, []
    for i in range(repeats):
        for side, wt in (("base", bw), ("head", hw)):
            r, hit, err = run_side(side, wt, ch, probes, timeout, td)
            if r is None:
                return None, f"{side}: {err}"
            runs[side, i] = r
            # Either side reaching the edit is coverage of it: a probe can run
            # the removed lines and none of the added ones.
            reached = [a or b for a, b in
                       itertools.zip_longest(reached, hit, fillvalue=False)]

    # Attempting to build an input and failing is not coverage. Say so rather
    # than counting the callable as verified.
    dead = {"probe-error", "setup-raise"}
    if all(r["kind"] in dead for r in runs["base", 0]):
        return None, "no usable inputs could be built"

    kept = hits = flaky = refused = 0
    found = []
    for i, args in enumerate(probes):
        bs = [runs["base", k][i] for k in range(repeats)]
        hs = [runs["head", k][i] for k in range(repeats)]
        # a side that disagrees with itself is unverifiable, not a finding
        if any(x != bs[0] for x in bs) or any(x != hs[0] for x in hs):
            flaky += 1
            continue
        kept += 1
        if i < len(reached) and reached[i]:
            hits += 1
        if bs[0] != hs[0]:
            if bs[0]["kind"] == "raise" and bs[0]["type"] in REFUSED:
                refused += 1
                continue
            found.append(Delta(ch.file, ch.qualname, args, bs[0], hs[0], ch.kind,
                               1 if ch.instances else len(ch.ctor_params),
                               bool(ch.instances)))
    return (kept, hits, flaky, refused, found), None


def verify(repo, base, head, limit=24, timeout=20.0, seed=0, repeats=2,
           include_tests=False) -> Report:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repository")
    base, head = resolve(repo, base), resolve(repo, head)
    rep = Report()
    changes = changed_functions(repo, base, head, include_tests)
    if not changes:
        return rep

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bw, hw = td / "base", td / "head"
        git(repo, "worktree", "add", "--detach", str(bw), base)
        git(repo, "worktree", "add", "--detach", str(hw), head)
        try:
            for ch in changes:
                if ch.skip:
                    rep.skipped.append((ch.qualname, ch.skip))
                    continue
                bp, bc, berr = signatures(bw, ch, timeout, td)
                hp, hc, herr = signatures(hw, ch, timeout, td)
                if bp is None or hp is None:
                    rep.skipped.append((ch.qualname, berr or herr))
                    continue
                params = _agree(bp, hp, required_only=False)
                ctor = _agree(bc, hc, required_only=True)
                if params is None or ctor is None:
                    rep.skipped.append((ch.qualname, "signature changed"))
                    continue
                ch.params, ch.ctor_params = params, ctor

                resolve_producers(bw, ch, timeout, td)
                probes, why = make_probes(ch, limit, seed)
                if probes is None:
                    rep.skipped.append((ch.qualname, why))
                    continue

                res, err = _sweep(ch, probes, bw, hw, repeats, timeout, td)
                if res is None:
                    rep.skipped.append((ch.qualname, err))
                    continue

                kept, hits, flaky, refused, found = res
                # Cost is counted whatever the verdict: these probes ran, and the
                # flake filter really did fire. Only checked, reached and the
                # deltas are a claim about what the sweep established.
                rep.probes += kept
                rep.flaky += flaky
                rep.refused += refused

                # A probe that never executed a moved line could not have been
                # affected by the edit, so a callable no probe reached was run,
                # not verified. A delta overrides that: a moved default argument
                # or class attribute is evaluated at import, before the trace
                # starts, and differs without any moved line being stepped on.
                if kept and not hits and not found and any(ch.lines.values()):
                    rep.skipped.append((ch.qualname, "no probe reached the change"))
                    continue

                rep.checked += 1
                rep.reached += hits
                rep.deltas.extend(found)
        finally:
            git(repo, "worktree", "remove", "--force", str(bw), check=False)
            git(repo, "worktree", "remove", "--force", str(hw), check=False)

    # A finding someone has already ruled intended is set aside rather than
    # dropped: the run still knows about it, and `--accept` still sees it, but
    # it is not reported again. Reporting the same intended change every time is
    # how a check gets turned off.
    accepted = read_verdicts(repo)
    if accepted:
        rep.known = [d for d in rep.deltas if fingerprint(d) in accepted]
        rep.deltas = [d for d in rep.deltas if fingerprint(d) not in accepted]
    return rep


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _key(d: Delta) -> tuple:
    """The shape of a difference, without the arguments that exposed it.

    Two runs pick different probes -- a different seed, a wider budget, a corpus
    that grew -- so anything derived from the arguments makes a finding that
    cannot be recognised twice. What stays the same is where it happened and
    what changed about the answer.
    """
    return (
        d.file, d.qualname,
        d.base["kind"], d.head["kind"],
        d.base["type"], d.head["type"],
        d.base["value"] == d.head["value"],          # differs only in mutation
    )


def fingerprint(d: Delta) -> str:
    """A finding's name in the verdict file. Short enough to read in a diff."""
    return hashlib.sha256("\x00".join(map(str, _key(d))).encode()).hexdigest()[:12]


def cluster(deltas: list[Delta]) -> list[list[Delta]]:
    """One root cause is one finding. Group by the shape of the difference, not
    the arguments that happened to expose it."""
    groups: dict[tuple, list[Delta]] = {}
    for d in deltas:
        groups.setdefault(_key(d), []).append(d)
    return list(groups.values())


VERDICTS = ".twinrun.json"


def read_verdicts(repo) -> dict:
    """Findings a human has already ruled on, by fingerprint.

    A tool that reports the same intended change on every run is one people
    turn off. The old code is the oracle for what the behaviour *was*; only a
    person can say whether changing it was the point. Writing that down is the
    difference between a check that gets quieter as it is used and one that
    does not.
    """
    p = Path(repo) / VERDICTS
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("accepted", {}) if isinstance(data, dict) else {}


def write_verdicts(repo, deltas, note: str = "") -> int:
    """Record every current finding as intended. Returns how many were new."""
    p = Path(repo) / VERDICTS
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    accepted = data.setdefault("accepted", {}) if isinstance(data, dict) else {}
    added = 0
    for g in cluster(deltas):
        d = g[0]
        fp = fingerprint(d)
        if fp in accepted:
            continue
        accepted[fp] = {"where": f"{d.file}::{d.qualname}",
                        "was": f"{d.base['kind']} {d.base['type']}",
                        "now": f"{d.head['kind']} {d.head['type']}",
                        "note": note}
        added += 1
    p.write_text(json.dumps({"accepted": accepted}, indent=1, sort_keys=True) + "\n")
    return added
