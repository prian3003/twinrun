"""Twin-run verification.

Run the base and head versions of a changed callable on identical inputs and
diff the outputs. The old code is the oracle, so no specification is needed.

Every probe runs twice per side. A probe whose own side disagrees with itself is
non-deterministic and is dropped, not reported. Noise is the thing that gets a
tool like this muted, so the filter is not optional.
"""

from __future__ import annotations

import ast
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
    skip: str | None = None


@dataclass
class Delta:
    file: str
    qualname: str
    args: list[str]
    base: dict
    head: dict
    kind: str = "function"
    n_ctor: int = 0


@dataclass
class Report:
    deltas: list[Delta] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (qualname, reason)
    checked: int = 0        # callables actually twin-run
    probes: int = 0         # probes compared after the flake filter
    flaky: int = 0          # probes dropped as non-deterministic


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


def _fixtures(repo, head, wanted: set[str], per_type: int = 6,
              min_len: int = 12, max_len: int = 400) -> dict[str, list[str]]:
    """Long string and bytes literals from the repository's own tests.

    No corpus of edge values will produce "value.TgPVoaGhoQ.AGBfQ6G6cr07byTRt0z"
    -- a signed payload whose timestamp is years old. The test suite is full of
    inputs like it, written by someone who knew what a valid one looks like, and
    they are literals, so both sides get the identical value by construction.

    Only tests that mention something that changed are read.
    """
    out = {}
    for f in git(repo, "ls-tree", "-r", "--name-only", head).split("\n"):
        if not f.endswith(".py") or not is_test_path(f):
            continue
        src = git(repo, "show", f"{head}:{f}", check=False)
        if not src or not (wanted & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", src))):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Constant) or not isinstance(n.value, (str, bytes)):
                continue
            if not min_len <= len(n.value) <= max_len:
                continue
            key = "str" if isinstance(n.value, str) else "bytes"
            got = out.setdefault(key, [])
            src_text = repr(n.value)
            if src_text not in got and len(got) < per_type:
                got.append(src_text)
    return out


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
        hit = [q for q in pairs if ast.dump(bt[q][0]) != ast.dump(ht[q][0])]
        seen[f] = (h, bt, ht, pairs, hit)
        moved |= set(hit) | {q.rsplit(".", 1)[-1] for q in hit}

    if not moved:
        return out
    fixtures = _fixtures(repo, head, moved)
    tree_cache = {}

    for f, (h, bt, ht, pairs, hit) in seen.items():
        if not hit:
            continue
        sibs = _siblings(repo, head, f, tree_cache)
        ctors = _ctor_map([h] + [x for x in sibs if x != h])

        # Extracting a helper leaves its callers byte-identical while their
        # behaviour moves underneath them. Probe one level of those too: the
        # helper's own finding names a private function, the caller's names the
        # thing anyone actually calls.
        tails = {q.rsplit(".", 1)[-1] for q in hit}
        callers = [q for q in pairs
                   if q not in hit and _refs(ht[q][0]) & tails][:CALLER_LIMIT]

        made = _producers(h, moved, ctors, fixtures)

        # The producer is often in the module the consumer imported it from:
        # timed.py takes what signer.py signs. Read the siblings this file
        # actually imports from, and keep only what it can name.
        bound = _imported(h)
        for sib in sibs:
            if sib == h:
                continue
            for ann, exprs in _producers(sib, moved, ctors, fixtures).items():
                for ident, e in exprs:
                    if _head(e) in bound:
                        _produced(made, ann, ident, e, per_type=8)

        for k, v in fixtures.items():
            made.setdefault(k, []).extend(("fixture", e) for e in v)
        made = {k: [e for _, e in v] for k, v in made.items()}
        for qual in hit + callers:
            ch = _describe(f, qual, *ht[qual], *bt[qual])
            ch.ctors, ch.producers = ctors, made
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

def _ctor_exprs(name: str, ctors: dict, limit: int = 3, aliases: dict | None = None) -> list[str]:
    """Source expressions that build an instance of a local class. One level
    deep: a constructor that itself wants a project type falls back to a
    no-argument call, which fails identically on both sides and so reports
    nothing."""
    params = ctors.get(name)
    if not params:
        return [f"{name}()"]
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
    return list(dict.fromkeys(out))


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
               fixtures: dict | None = None, per_type: int = 4) -> dict[str, list[str]]:
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
            insts = _ctor_exprs(n.name, ctors, limit=2, aliases=al)
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


def _values(ann: str, ctors: dict | None = None,
            producers: dict | None = None) -> list[str] | None:
    if not ann:
        return UNTYPED
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", QUALIFIER.sub("", ann))
    optional = "None" in names or "Optional" in names
    if "Any" in names:
        return UNTYPED              # a type that says nothing gets the spread
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
    for pname, ann in (*change.ctor_params, *change.params):
        v = _values(ann, change.ctors, change.producers)
        if v is None:
            return None, f"unmodelled type {ann!r} on {pname}"
        cols.append(v)
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


def run_side(worktree: Path, change: Change, probes, timeout: float, tmp: Path):
    data, err = _invoke(worktree, {
        "file": change.file,
        "qualname": change.qualname,
        "kind": change.kind,
        "n_ctor": len(change.ctor_params),
        "probes": probes,
    }, timeout, tmp)
    return (None, err) if data is None else (data["results"], None)


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

                runs = {}
                for side, wt in (("base", bw), ("head", hw)):
                    for i in range(repeats):
                        r, err = run_side(wt, ch, probes, timeout, td)
                        if r is None:
                            rep.skipped.append((ch.qualname, f"{side}: {err}"))
                            runs = None
                            break
                        runs[side, i] = r
                    if runs is None:
                        break
                if runs is None:
                    continue

                # Attempting to build an input and failing is not coverage. Say so
                # rather than counting the callable as verified.
                dead = {"probe-error", "setup-raise"}
                if all(r["kind"] in dead for r in runs["base", 0]):
                    rep.skipped.append((ch.qualname, "no usable inputs could be built"))
                    continue

                rep.checked += 1
                for i, args in enumerate(probes):
                    bs = [runs["base", k][i] for k in range(repeats)]
                    hs = [runs["head", k][i] for k in range(repeats)]
                    # a side that disagrees with itself is unverifiable, not a finding
                    if any(x != bs[0] for x in bs) or any(x != hs[0] for x in hs):
                        rep.flaky += 1
                        continue
                    rep.probes += 1
                    if bs[0] != hs[0]:
                        rep.deltas.append(Delta(ch.file, ch.qualname, args, bs[0], hs[0],
                                            ch.kind, len(ch.ctor_params)))
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
