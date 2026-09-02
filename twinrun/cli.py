import argparse
import sys

from .core import verify

BOLD, DIM, RED, GREEN, YELLOW, OFF = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"


def _plain():
    return not sys.stdout.isatty()


def paint(s, code):
    return s if _plain() else f"{code}{s}{OFF}"


def show(rep, base, head):
    print(f"{paint('twinrun', BOLD)} {base}..{head}\n")

    for d in rep.deltas:
        call = f"{d.qualname}({', '.join(d.args)})"
        print(f"  {paint('DELTA', RED)}  {d.file} :: {call}")
        for label, r in (("base", d.base), ("head", d.head)):
            print(f"         {paint(label, DIM)}  {r['kind']:<12} {r['value']}")
            if r["stdout"]:
                print(f"         {paint('    stdout', DIM)}  {r['stdout']!r}")
        print()

    n = len(rep.deltas)
    head_line = paint(f"{n} delta{'' if n == 1 else 's'}", RED if n else GREEN)
    print(
        f"{head_line} · {rep.checked} function{'' if rep.checked == 1 else 's'} checked"
        f" · {rep.probes} probes · {rep.flaky} flaky dropped"
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
    ap.add_argument("--limit", type=int, default=24, help="max probes per function")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per side per function")
    ap.add_argument("--seed", type=int, default=0, help="probe sampling seed")
    a = ap.parse_args(argv)

    rep = verify(a.repo, a.base, a.head, limit=a.limit, timeout=a.timeout, seed=a.seed)
    show(rep, a.base, a.head)
    return 1 if rep.deltas else 0


if __name__ == "__main__":
    sys.exit(main())
