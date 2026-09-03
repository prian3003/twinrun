"""Score twinrun against regressions a real repository already admitted to.

`sandbox.py` builds a repo whose answers are known. This one takes a repo whose
answers were written down by the people who maintained it, and asks the only
question that matters: run at the commit that introduced a bug, does twinrun
report the callable someone later had to fix?

Two kinds of pair, and they are not worth the same.

A **revert** names its target outright -- "This reverts commit abc123" -- and
reverting is the maintainers saying the whole change was unwanted. Nothing is
inferred. These are the pairs to quote.

A **blame** pair starts from a commit whose message says it fixed something,
takes the lines it removed, and blames them at its parent. The commit that last
touched a line is often just the last refactor, so the label is weak, and it
gets weaker the more of a repo's history is formatting and typing passes. Fix
commits that are themselves cosmetic are dropped, which helps and does not
cure it. Read these as coverage, not as a hit rate.

    python3 regressions.py ~/src/itsdangerous
    python3 regressions.py ~/src/click --since 2019 --blame

Anything the interpreter cannot import -- a Python 2 revision, a dependency
whose modern release dropped the name being imported -- is reported as
unrunnable and kept out of the denominator. It is not a miss; it never ran.
"""

import argparse
import ast
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun.core import verify

FIX = re.compile(r"\b(fix|fixes|fixed|fixing|regression|revert|broke|broken|bug)\b", re.I)
# A commit that says "fix" about a type stub or a misspelling is not evidence
# that the line it touched ever behaved wrong. These pairs are the miner's own
# noise, and on a repo that runs a formatter they are most of them.
COSMETIC = re.compile(
    r"\b(typo|typos|spelling|lint|flake8|mypy|pyright|ruff|pyupgrade|black|isort|"
    r"type[ -]?hints?|annotations?|docstrings?|docs?|changelog|formatting|style|"
    r"whitespace|comment|comments|import|imports)\b", re.I)
SKIP_PATH = re.compile(r"(^|/)(tests?|docs?|examples?|setup)[/.]")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).stdout


def is_src(path):
    return path.endswith(".py") and not SKIP_PATH.search(path)


def parses(repo, sha):
    """Every source file the commit touched, as Python this interpreter reads."""
    files = [p for p in git(repo, "show", "--name-only", "--format=", sha).split()
             if is_src(p)]
    for p in files:
        try:
            ast.parse(git(repo, "show", f"{sha}:{p}") or "")
        except SyntaxError:
            return False
    return bool(files)


def has_parent(repo, sha):
    return bool(git(repo, "rev-parse", "--verify", f"{sha}^").strip())


def pair(repo, sha, kind, fix_subject, callables=()):
    return {"intro": sha, "kind": kind,
            "date": git(repo, "log", "--format=%as", "-n1", sha).strip(),
            "subject": git(repo, "log", "--format=%s", "-n1", sha).strip(),
            "fix": fix_subject, "callables": sorted(set(callables))}


def reverts(repo):
    """Commits a later commit ran `git revert` on."""
    out = []
    for entry in git(repo, "log", "--format=%H%x00%s%x00%b%x01").split("\x01"):
        if not entry.strip():
            continue
        _, subject, body = entry.strip().split("\x00")
        for target in re.findall(r"This reverts commit ([0-9a-f]{7,40})", body):
            sha = git(repo, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
            if sha and has_parent(repo, sha) and parses(repo, sha):
                out.append(pair(repo, sha, "revert", subject))
    return out


def removed_ranges(repo, sha):
    """(old path, first line, last line) for every hunk the commit removed from."""
    out, path = [], None
    for line in git(repo, "diff", "-U0", "--find-renames", f"{sha}^", sha).splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("@@") and path and is_src(path):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? ", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count:                       # 0 means the hunk only added
                out.append((path, start, start + count - 1))
    return out


def enclosing(repo, sha, path, line):
    try:
        tree = ast.parse(git(repo, "show", f"{sha}:{path}") or "")
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node                 # the innermost one that contains it
    return best.name if best else None


def blames(repo):
    """Commits that wrote a line a later fix commit had to remove."""
    found = {}
    for line in git(repo, "log", "--format=%H\t%s").splitlines():
        sha, subject = line.split("\t", 1)
        if not FIX.search(subject) or COSMETIC.search(subject):
            continue
        if len(git(repo, "rev-list", "--parents", "-n1", sha).split()) != 2:
            continue                        # a merge has no single diff to read
        for path, lo, hi in removed_ranges(repo, sha):
            blame = git(repo, "blame", "-l", "-L", f"{lo},{hi}", f"{sha}^", "--", path)
            name = enclosing(repo, f"{sha}^", path, lo)
            for row in blame.splitlines():
                intro = row.split(" ", 1)[0].lstrip("^")
                if intro != sha and name:
                    found.setdefault(intro, (subject, set()))[1].add(name)
    out = []
    for intro, (subject, names) in found.items():
        if FIX.search(git(repo, "log", "--format=%s", "-n1", intro)):
            continue                        # a fix of a fix is not a clean pair
        if has_parent(repo, intro) and parses(repo, intro):
            out.append(pair(repo, intro, "blame", subject, names))
    return out


def score(repo, p, limit, timeout):
    """What twinrun says about the commit that introduced the bug."""
    want = set(p["callables"])
    try:
        rep = verify(repo, p["intro"] + "^", p["intro"], limit=limit, timeout=timeout)
    except Exception as e:
        return {"status": "error", "note": f"{type(e).__name__}: {e}"}
    # A verdict file in the repo hides findings from `rep.deltas`, and scoring
    # is not the place to honour one: the question here is what the tool sees,
    # not what someone has already signed off.
    got = {d.qualname.split(".")[-1] for d in rep.deltas + rep.known}
    reasons = " ".join(w for _, w in rep.skipped)
    # A revert undid the whole commit, so any delta on it is the change that
    # was unwanted. A blame pair points at named callables and has to hit one.
    if got and (p["kind"] == "revert" or want & got):
        status = "caught"
    elif got:
        status = "other"                    # flagged, but not what the fix touched
    elif "Error" in reasons and not rep.probes:
        status = "unrunnable"               # never imported; not a miss
    elif rep.probes and not rep.reached:
        status = "no-reach"
    elif rep.probes:
        status = "silent"
    else:
        status = "empty"                    # nothing executable in the radius
    return {"status": status, "checked": rep.checked, "probes": rep.probes,
            "reached": rep.reached, "refused": rep.refused,
            "note": ", ".join(sorted(got))[:60]}


RUNNABLE = ("caught", "other", "no-reach", "silent")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo")
    ap.add_argument("--since", default="0000", help="ignore commits older than this date")
    ap.add_argument("--blame", action="store_true",
                    help="also mine fix commits by blame; weaker labels, more of them")
    ap.add_argument("--limit", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    if not (repo / ".git").exists():
        print(f"regressions: {repo} is not a git repository", file=sys.stderr)
        return 2

    pairs = reverts(repo) + (blames(repo) if a.blame else [])
    pairs = [p for p in pairs if p["date"] >= a.since]
    pairs.sort(key=lambda p: (p["kind"] != "revert", p["date"]), reverse=True)
    if not pairs:
        print("regressions: no pairs found; try --blame, or a wider --since")
        return 2
    print(f"{repo.name}  {len(pairs)} pairs "
          f"({sum(p['kind'] == 'revert' for p in pairs)} reverts)\n")

    counts = Counter()
    for p in pairs:
        t = time.time()
        r = score(repo, p, a.limit, a.timeout)
        counts[r["status"]] += 1
        print(f'  {r["status"]:<10} {p["kind"]:<6} {p["date"]}  {p["intro"][:8]}  '
              f'{p["subject"][:46]:<46}  {r.get("note", "")[:40]}  '
              f'{time.time() - t:.1f}s', flush=True)

    runnable = sum(counts[k] for k in RUNNABLE)
    print("\n" + "  ".join(f"{k}={v}" for k, v in counts.most_common()))
    print(f"{counts['caught']}/{runnable} caught on runnable pairs, "
          f"{len(pairs) - runnable} never ran")
    return 0 if counts["caught"] else 1


if __name__ == "__main__":
    sys.exit(main())
