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


def _decorators(node) -> set[str]:
    return {ast.unparse(d).split("(")[0].split(".")[-1] for d in node.decorator_list}


def _sig_params(node, drop_first: bool):
    args = [*node.args.posonlyargs, *node.args.args]
    if drop_first:
        args = args[1:]
    return [(a.arg, ast.unparse(a.annotation) if a.annotation else "") for a in args]


def _bad_sig(node) -> bool:
    a = node.args
    return bool(a.vararg or a.kwarg or a.kwonlyargs)


def _describe(file: str, qualname: str, node, cls_node) -> Change:
    name = qualname.rsplit(".", 1)[-1]
    if isinstance(node, ast.AsyncFunctionDef):
        return Change(file, qualname, skip="async")
    decs = _decorators(node)

    if cls_node is None:
        if decs:
            return Change(file, qualname, skip=f"decorated ({', '.join(sorted(decs))})")
        if _bad_sig(node):
            return Change(file, qualname, skip="*args/**kwargs/keyword-only")
        return Change(file, qualname, params=_sig_params(node, False))

    if name in ("__init__", "__new__"):
        # Observed indirectly: a changed constructor shows up in every method's
        # recorded instance state.
        return Change(file, qualname, skip="constructor, observed through its methods")
    unknown = decs - DECOR_OK
    if unknown:
        return Change(file, qualname, skip=f"decorated ({', '.join(sorted(unknown))})")
    if _bad_sig(node):
        return Change(file, qualname, skip="*args/**kwargs/keyword-only")
    if "staticmethod" in decs:
        return Change(file, qualname, kind="static", params=_sig_params(node, False))
    if "classmethod" in decs:
        return Change(file, qualname, kind="classmethod", params=_sig_params(node, True))

    init = next(
        (m for m in cls_node.body
         if isinstance(m, ast.FunctionDef) and m.name == "__init__"),
        None,
    )
    if init is not None and _bad_sig(init):
        return Change(file, qualname, skip=f"{cls_node.name}.__init__ takes *args/**kwargs")
    ctor = _sig_params(init, True) if init is not None else []
    return Change(file, qualname, kind="instance",
                  params=_sig_params(node, True), ctor_params=ctor)


def changed_functions(repo, base, head) -> list[Change]:
    """Callables present in both revisions whose AST differs.

    Present-in-both is the requirement, not an approximation: a callable with no
    twin has nothing to be compared against.
    """
    out = []
    names = git(repo, "diff", "--name-only", base, head).split("\n")
    for f in [n for n in names if n.endswith(".py")]:
        b = git(repo, "show", f"{base}:{f}", check=False)
        h = git(repo, "show", f"{head}:{f}", check=False)
        if not b or not h:
            continue
        bt, ht = _targets(b), _targets(h)
        for qual in sorted(set(bt) & set(ht)):
            if ast.dump(bt[qual][0]) != ast.dump(ht[qual][0]):
                out.append(_describe(f, qual, *ht[qual]))
    return out


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def _values(ann: str) -> list[str] | None:
    if not ann:
        return UNTYPED
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ann)
    vals = None
    for n in names:
        if n.lower() in CORPUS:
            vals = list(CORPUS[n.lower()])
            break
    if vals is None:
        return None
    if "None" in names or "Optional" in names:
        vals.append("None")
    return vals


def make_probes(change: Change, limit: int, seed: int = 0):
    """One probe is the constructor arguments followed by the call arguments."""
    cols = []
    for pname, ann in (*change.ctor_params, *change.params):
        v = _values(ann)
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
    rng = random.Random(seed)
    seen, out = set(), []
    for _ in range(limit * 30):
        cand = tuple(rng.choice(c) for c in cols)
        if cand in seen:
            continue
        seen.add(cand)
        out.append(list(cand))
        if len(out) >= limit:
            break
    return out, None


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def run_side(worktree: Path, change: Change, probes, timeout: float, tmp: Path):
    out = tmp / "res.json"
    out.unlink(missing_ok=True)
    payload = {
        "root": str(worktree),
        "file": change.file,
        "qualname": change.qualname,
        "kind": change.kind,
        "n_ctor": len(change.ctor_params),
        "probes": probes,
        "out": str(out),
    }
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
    return data["results"], None


def verify(repo, base, head, limit=24, timeout=20.0, seed=0, repeats=2) -> Report:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repository")
    base, head = resolve(repo, base), resolve(repo, head)
    rep = Report()
    changes = changed_functions(repo, base, head)
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
