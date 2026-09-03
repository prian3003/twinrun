import argparse
import sys

from .core import cluster, verify

BOLD, DIM, RED, GREEN, YELLOW, OFF = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m",
)


def paint(s, code):
    return s if not sys.stdout.isatty() else f"{code}{s}{OFF}"


def render_call(d):
    args = ", ".join(d.args[d.n_ctor:])
    if d.kind == "ctor":
        return f"{d.qualname.rsplit('.', 1)[0]}({args})"   # nobody calls __init__
    if d.kind == "property":
        cls, attr = d.qualname.split(".", 1)
        recv = d.args[0] if d.built else f"{cls}({', '.join(d.args[:d.n_ctor])})"
        return f"{recv}.{attr}"
    if d.kind == "instance":
        cls, meth = d.qualname.split(".", 1)
        recv = d.args[0] if d.built else f"{cls}({', '.join(d.args[:d.n_ctor])})"
        return f"{recv}.{meth}({args})"
    return f"{d.qualname}({args})"


def show_side(label, r):
    print(f"         {paint(label, DIM)}  {r['kind']:<12} {paint(r['type'], DIM):<10} {r['value']}")
    if r["mutated"]:
        print(f"               {paint('after', DIM)}  {r['mutated']}")
    if r["stdout"]:
        print(f"              {paint('stdout', DIM)}  {r['stdout']!r}")


def show(rep, base, head):
    print(f"{paint('twinrun', BOLD)} {base}..{head}\n")

    groups = cluster(rep.deltas)
    for g in groups:
        d = g[0]
        more = f"   {paint(f'+{len(g) - 1} more calls', DIM)}" if len(g) > 1 else ""
        print(f"  {paint('DELTA', RED)}  {d.file} :: {d.qualname}{more}")
        print(f"         {render_call(d)}")
        show_side("base", d.base)
        show_side("head", d.head)
        print()

    n = len(groups)
    calls = len(rep.deltas)
    tail = " from %d calls" % calls if calls != n else ""
    findings = "%d finding%s%s" % (n, "" if n == 1 else "s", tail)
    checked = "%d callable%s checked" % (rep.checked, "" if rep.checked == 1 else "s")
    print(
        f"{paint(findings, RED if n else GREEN)} · {checked}"
        f" · {rep.probes} probes ({rep.reached} reached the change)"
        f" · {rep.flaky} flaky dropped"
        + (f" · {rep.refused} the old code refused" if rep.refused else "")
    )
    for name, why in rep.skipped:
        print(paint(f"  skipped {name}: {why}", YELLOW))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="twinrun",
        description="Run base and head on identical inputs; report where behaviour differs.",
    )
    ap.add_argument("repo", help="path to a git repository")
    ap.add_argument("--base", default="HEAD~1", help="revision to treat as the oracle")
    ap.add_argument("--head", default="HEAD", help="revision under test")
    ap.add_argument("--limit", type=int, default=24, help="max probes per callable")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per side per callable")
    ap.add_argument("--seed", type=int, default=0, help="probe sampling seed")
    ap.add_argument("--include-tests", action="store_true",
                    help="also probe test files, which means running the suite")
    ap.add_argument("--repeats", type=int, default=2,
                    help="runs per side; raise it when a target has few possible outputs")
    a = ap.parse_args(argv)

    try:
        rep = verify(a.repo, a.base, a.head, limit=a.limit, timeout=a.timeout, seed=a.seed,
                     repeats=a.repeats, include_tests=a.include_tests)
    except RuntimeError as e:
        print(f"twinrun: {e}", file=sys.stderr)
        return 2
    show(rep, a.base, a.head)
    return 1 if rep.deltas else 0


if __name__ == "__main__":
    sys.exit(main())
