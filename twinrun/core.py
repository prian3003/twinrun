"""Twin-run verification.

Run the base and head versions of a changed function on identical inputs and
diff the outputs. The old code is the oracle, so no spec is needed.

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
# which costs a probe and reports nothing -- fine. LLM probe synthesis is phase 1.
UNTYPED = ["0", "1", "-1", "''", "'a'", "[]", "{}", "None", "True", "-0.5"]


# --------------------------------------------------------------------------
# blast radius: which functions actually changed behaviour-relevant source
# --------------------------------------------------------------------------

@dataclass
class Change:
    file: str
    qualname: str
    params: list[tuple[str, str]] = field(default_factory=list)
    skip: str | None = None


@dataclass
class Delta:
    file: str
    qualname: str
    args: list[str]
    base: dict
    head: dict


@dataclass
class Report:
    deltas: list[Delta] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (qualname, reason)
    checked: int = 0        # functions actually twin-run
    probes: int = 0         # probes compared after the flake filter
    flaky: int = 0          # probes dropped as non-deterministic


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def _module_functions(src: str) -> dict[str, ast.AST]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _describe(file: str, name: str, node) -> Change:
    a = node.args
    if isinstance(node, ast.AsyncFunctionDef):
        return Change(file, name, skip="async")
    if a.vararg or a.kwarg or a.kwonlyargs:
        return Change(file, name, skip="*args/**kwargs/keyword-only")
    if node.decorator_list:
        return Change(file, name, skip="decorated")
    params = [
        (arg.arg, ast.unparse(arg.annotation) if arg.annotation else "")
        for arg in (*a.posonlyargs, *a.args)
    ]
    return Change(file, name, params)


def changed_functions(repo, base, head) -> list[Change]:
    """Module-level functions present in both revisions whose AST differs.

    Present-in-both is the requirement, not an approximation: a function with no
    twin has nothing to be compared against.
    """
    out = []
    names = git(repo, "diff", "--name-only", base, head).split("\n")
    for f in [n for n in names if n.endswith(".py")]:
        b = git(repo, "show", f"{base}:{f}", check=False)
        h = git(repo, "show", f"{head}:{f}", check=False)
        if not b or not h:
            continue
        bf, hf = _module_functions(b), _module_functions(h)
        for name in sorted(set(bf) & set(hf)):
            if ast.dump(bf[name]) != ast.dump(hf[name]):
                out.append(_describe(f, name, hf[name]))
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
    cols = []
    for pname, ann in change.params:
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


def verify(repo, base, head, limit=24, timeout=20.0, seed=0) -> Report:
    repo = Path(repo).resolve()
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
                    for i in (0, 1):
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
                    b1, b2 = runs["base", 0][i], runs["base", 1][i]
                    h1, h2 = runs["head", 0][i], runs["head", 1][i]
                    if b1 != b2 or h1 != h2:
                        rep.flaky += 1
                        continue
                    rep.probes += 1
                    if b1 != h1:
                        rep.deltas.append(Delta(ch.file, ch.qualname, args, b1, h1))
        finally:
            git(repo, "worktree", "remove", "--force", str(bw), check=False)
            git(repo, "worktree", "remove", "--force", str(hw), check=False)
    return rep
