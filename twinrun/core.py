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
    "any": ["0", "1", "-1", "''", "'a'", "[]", "None", "True"],
    "object": ["0", "'a'", "None"],
}
# No annotation: try a spread. Most land on TypeError identically on both sides,
# which costs a probe and reports nothing. LLM probe synthesis is the next step.
UNTYPED = ["0", "1", "-1", "''", "'a'", "[]", "{}", "None", "True", "-0.5"]

DECOR_OK = {"staticmethod", "classmethod"}

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
    skip: str | None = None

    @property
    def instances(self) -> list[str]:
        """Whole constructions to probe with, in place of assembling the
        constructor's arguments from the corpus. A constructor with six
        parameters spends six probe columns on getting itself built; one the
        test suite already wrote spends one, and is known to work."""
        return self.built if self.kind == "instance" and self.built else []


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


def _class_ctors(src: str) -> dict[str, list]:
    """Local classes and the parameters their __init__ takes, so a parameter
    annotated with a project type can be built instead of skipped."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}
    for n in tree.body:
        if not isinstance(n, ast.ClassDef):
            continue
        init = _init_of(n)
        if init is None:
            out[n.name] = []
        elif _bad_sig(init):
            out[n.name] = None          # not constructible from a positional sweep
        else:
            out[n.name] = _sig_params(init, True)
    return out


def _ctor_map(srcs) -> dict[str, list]:
    """Constructor parameters for every class in a package, with an inherited
    __init__ resolved to the base class that defines it. TimestampSigner takes
    its constructor from Signer in another module; without this it is built
    with no arguments at all, and every probe dies in setup."""
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


def _describe(file: str, qualname: str, node, cls_node, base_node, base_cls) -> Change:
    name = qualname.rsplit(".", 1)[-1]
    if isinstance(node, ast.AsyncFunctionDef):
        return Change(file, qualname, skip="async")
    decs = _decorators(node)

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
        # Observed indirectly: a changed constructor shows up in every method's
        # recorded instance state.
        return Change(file, qualname, skip="constructor, observed through its methods")
    unknown = decs - DECOR_OK
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
    return Change(file, qualname, kind="instance", params=params, ctor_params=ctor)


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
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                # partial(TimestampSigner, secret_key="secret-key") is a
                # construction; it is how a pytest fixture writes one.
                name, node = n.func.id, n
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
            src_text = repr(n.value)
            if src_text not in got and len(got) < per_type:
                got.append(src_text)
    return lits, calls


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


def _guard_key(n) -> str | None:
    """The probe column a guard names: a bare parameter, or a `self` attribute
    a constructor conventionally sets from one of its own."""
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
            and n.value.id == "self":
        return n.attr
    return None


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
        # Producers are read from the base revision, never the head one. A
        # function that only exists in head raises NameError on one side and
        # returns a value on the other, which is a delta on every callable that
        # consumes its type -- a false positive machine. Anything present in
        # base and absent from `moved` is identical in both.
        sibs = _siblings(repo, base, f, tree_cache)
        ctors = _ctor_map([b] + [x for x in sibs if x != b])

        # Extracting a helper leaves its callers byte-identical while their
        # behaviour moves underneath them. Probe one level of those too: the
        # helper's own finding names a private function, the caller's names the
        # thing anyone actually calls.
        tails = {q.rsplit(".", 1)[-1] for q in hit}
        callers = [q for q in pairs
                   if q not in hit and _refs(ht[q][0]) & tails][:CALLER_LIMIT]

        made = _producers(b, moved, ctors, fixtures, calls=tcalls)

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
            ch = _describe(f, qual, *ht[qual], *bt[qual])
            ch.ctors, ch.producers, ch.lines = ctors, made, touched
            ch.guards = _guards(ht[qual][0], touched["head"])
            # A changed constructor anywhere disqualifies every harvested
            # construction: __init__ is resolved through inheritance, so the
            # one that moved is not always the one named on the class.
            cls = qual.split(".")[0] if "." in qual else ""
            if cls and "__init__" not in moved:
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
                calls: dict | None = None) -> list[str]:
    """Source expressions that build an instance of a local class. One level
    deep: a constructor that itself wants a project type falls back to a
    no-argument call, which fails identically on both sides and so reports
    nothing.

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
        v = _values(_expand(pann, aliases or {}))    # no ctors: depth stops here
        if v is None:
            return [f"{name}()"]
        cols.append(v)
    out = []
    # One variant per corpus index rather than the product: a constructor that
    # rejects the empty string usually accepts the value next to it, and the
    # product only ever varies the last parameter.
    for i in range(limit):
        out.append("%s(%s)" % (name, ", ".join(c[i % len(c)] for c in cols)))
    return list(dict.fromkeys(real + out))[:max(limit, len(real))]


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
        f = _made(pann, re.findall(r"[A-Za-z_][A-Za-z0-9_]*", pann), fixtures, 1)
        fixed.append(f[0] if f else vals[0])
    return [plain] if plain == fixed else [plain, fixed]


def _producers(src: str, exclude: set[str], ctors: dict | None = None,
               fixtures: dict | None = None, per_type: int = 4,
               calls: dict | None = None) -> dict[str, list[str]]:
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
            if n.returns is None or n.name in exclude:
                continue
            if _bad_sig(n) or _refs(n) & exclude:
                continue
            for args in _args_from(_sig_params(n, False), False, fixtures, al):
                _produced(out, _expand(ast.unparse(n.returns), al), n.name,
                          "%s(%s)" % (n.name, ", ".join(args)), per_type)

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
                if not isinstance(m, ast.FunctionDef) or m.returns is None:
                    continue
                if m.decorator_list or m.name.startswith("__"):
                    continue
                if f"{n.name}.{m.name}" in exclude or m.name in exclude:
                    continue
                if _bad_sig(m) or _refs(m) & exclude:
                    continue
                for args in _args_from(_sig_params(m, False), True, fixtures, al):
                    for inst in insts:
                        _produced(out, _expand(ast.unparse(m.returns), al),
                                  "%s.%s" % (n.name, m.name),
                                  "%s.%s(%s)" % (inst, m.name, ", ".join(args)),
                                  per_type)
    return out


def _made(ann: str, names: list[str], producers: dict | None, limit: int = 4) -> list[str]:
    """Producer expressions for this annotation. A producer returning `bytes`
    fills a parameter annotated `str | bytes`, which is how half of a typed
    codebase is written."""
    if not producers:
        return []
    out = list(producers.get(ann, []))
    for k, v in producers.items():
        if k != ann and k in names:
            out += v
    return out[:limit]


GUESSED = 3         # producer values offered per type to an unannotated parameter


def _values(ann: str, ctors: dict | None = None,
            producers: dict | None = None) -> list[str] | None:
    if not ann:
        # An unannotated parameter used to see nothing but the spread, which
        # threw away every producer and fixture the module had. A codebase with
        # no type hints is one where that is all of them, and the spread is the
        # part that cannot work: the value that gets past a signature check is
        # the token the test suite wrote down, never `0`. They go in front,
        # because the spread lands on the same TypeError on both sides and
        # reports nothing whatever order it is tried in.
        made = (_made("str", ["str"], producers, GUESSED)
                + _made("bytes", ["bytes"], producers, GUESSED))
        return made + UNTYPED if made else UNTYPED
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", QUALIFIER.sub("", ann))
    optional = "None" in names or "Optional" in names
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
            return UNTYPED          # a type that says nothing gets the spread
    made = _made(ann, names, producers)
    for n in names:
        if n.lower() in CORPUS:
            vals = list(CORPUS[n.lower()]) + made
            return vals + ["None"] if optional else vals
    real = [n for n in names if n not in TYPING]
    if not real:
        return None
    # Optional[Signer] is a Signer. Prefer a class this repository defines.
    head = next((n for n in real if ctors and n in ctors), real[-1])
    if made:
        vals = made
    elif ctors is not None and head in ctors:
        vals = _ctor_exprs(head, ctors)
    else:
        # An imported or unknown type. Try the no-argument constructor: the name
        # is resolved in the target module's own namespace, and if it cannot be
        # built the failure is identical on both sides.
        vals = [f"{head}()"]
    return vals + ["None"] if optional else vals


def make_probes(change: Change, limit: int, seed: int = 0):
    """One probe is the constructor arguments followed by the call arguments."""
    cols = []
    inst = change.instances
    if inst:
        cols.append(inst)
    for pname, ann in [*([] if inst else change.ctor_params), *change.params]:
        v = _values(ann, change.ctors, change.producers)
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
        "n_ctor": 1 if change.instances else len(change.ctor_params),
        "built": bool(change.instances),
        "lines": change.lines.get(side, []),
        "probes": probes,
    }, timeout, tmp)
    if data is None:
        return None, None, err
    return data["results"], data.get("reached") or [], None


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

    kept = hits = flaky = 0
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
            found.append(Delta(ch.file, ch.qualname, args, bs[0], hs[0], ch.kind,
                               1 if ch.instances else len(ch.ctor_params),
                               bool(ch.instances)))
    return (kept, hits, flaky, found), None


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

                probes, why = make_probes(ch, limit, seed)
                if probes is None:
                    rep.skipped.append((ch.qualname, why))
                    continue

                res, err = _sweep(ch, probes, bw, hw, repeats, timeout, td)
                if res is None:
                    rep.skipped.append((ch.qualname, err))
                    continue

                kept, hits, flaky, found = res
                # Cost is counted whatever the verdict: these probes ran, and the
                # flake filter really did fire. Only checked, reached and the
                # deltas are a claim about what the sweep established.
                rep.probes += kept
                rep.flaky += flaky

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
    return rep


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def cluster(deltas: list[Delta]) -> list[list[Delta]]:
    """One root cause is one finding. Group by the shape of the difference, not
    the arguments that happened to expose it."""
    groups: dict[tuple, list[Delta]] = {}
    for d in deltas:
        key = (
            d.file, d.qualname,
            d.base["kind"], d.head["kind"],
            d.base["type"], d.head["type"],
            d.base["value"] == d.head["value"],      # differs only in mutation
        )
        groups.setdefault(key, []).append(d)
    return list(groups.values())
