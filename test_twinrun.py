"""Self-check. Builds a throwaway repo containing every kind of change twinrun
has to tell apart, and asserts it tells them apart.

Run: python3 test_twinrun.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun.core import _values, cluster, verify

BASE = '''
def discount(price: int, pct: int) -> int:
    return price - price * pct // 100


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.random()


class Money:
    def __init__(self, cents: int):
        self.cents = cents


def fmt(m: Money) -> str:
    return "$%s" % (m.cents // 100)


def where() -> str:
    import os
    return os.getcwd()


def label(n: int) -> str:
    """Name."""
    return "n=%d" % n


def gate(n: int) -> int:
    if n == 987654321:
        return 1
    return 0


def warp(n: int) -> int:
    if n % 7919 == 33:
        return 1
    return 0


def tally(xs):
    return sum(xs)


def norm(s: str) -> str:
    return s.strip()


def area(w: int) -> int:
    return w * w


def scale(x: int) -> int:
    return x * 2


class Cart:
    def __init__(self, rate: int):
        self.rate = rate
        self.items = []

    def add(self, price: int) -> None:
        self.items.append(price)

    def total(self) -> int:
        return sum(self.items) * (100 + self.rate) // 100

    @staticmethod
    def parse(raw: str) -> int:
        return int(raw or 0)
'''

HEAD = '''
def discount(price: int, pct: int) -> int:
    return price - price * pct / 100              # int -> float


def slug(name: str) -> str:
    s = name.strip().lower()                      # equivalent rewrite
    return s.replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.random() * 2               # non-deterministic


class Money:
    def __init__(self, cents: int):
        self.cents = cents


def fmt(m: Money) -> str:
    return "$%s" % (m.cents / 100)                 # project-typed parameter


def where() -> str:
    import os
    cwd = os.getcwd()                             # rewritten, same behaviour
    return cwd


def label(n: int) -> str:
    """The name to print for a count."""          # docstring only, never executed
    return "n=%d" % n


def gate(n: int) -> int:
    if n == 987654321:
        return 2                                  # guarded by a literal, minable
    return 0


def warp(n: int) -> int:
    if n % 7919 == 33:
        return 2                                  # guarded by arithmetic, not
    return 0


def tally(xs: list) -> int:                       # annotation only, never executed
    return sum(xs)


def norm(text: str) -> str:                       # parameter renamed, signature
    return text.strip()                           # and body, nothing observable


def area(w: int, h: int = 2) -> int:              # appended an optional parameter
    return w * h                                  # ...and the one-arg answer moved


def scale(x: int, factor: int) -> int:            # appended a required parameter
    return x * factor


class Cart:
    def __init__(self, rate: int):
        self.rate = rate
        self.items = list()                       # rewritten, same behaviour

    def add(self, price: int) -> None:
        self.items.append(price * 2)              # mutation only, returns None

    def total(self) -> int:
        return sum(self.items) * (100 + self.rate) / 100

    @staticmethod
    def parse(raw: str) -> int:
        return int(raw.strip() or 0)              # ValueError -> 0 on blank
'''


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def fixture(root: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "dev")
    (root / "calc.py").write_text(BASE)
    (root / "test_calc.py").write_text("def check() -> int:\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add calc")
    (root / "calc.py").write_text(HEAD)
    (root / "test_calc.py").write_text("def check() -> int:\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tweak calc")


def main():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        fixture(repo)
        rep = verify(repo, "HEAD~1", "HEAD", limit=24)

    hit = {d.qualname for d in rep.deltas}
    skipped = dict(rep.skipped)

    # real behaviour changes, one per shape
    assert "discount" in hit, f"missed the plain-function change; found {hit}"
    assert "Cart.total" in hit, f"missed the instance-method change; found {hit}"
    assert "Cart.parse" in hit, f"missed the staticmethod change; found {hit}"
    assert "Cart.add" in hit, f"missed the mutation-only change; found {hit}"

    # a mutation-only method returns None on both sides: the finding has to come
    # from the recorded instance state, not the return value
    add = next(d for d in rep.deltas if d.qualname == "Cart.add")
    assert add.base["value"] == add.head["value"] == "None"
    assert add.base["mutated"] != add.head["mutated"], "state change not recorded"

    # head appended an optional parameter: the old call still has an answer, and
    # the answer changed
    assert "area" in hit, f"missed a change behind an appended default; found {hit}"

    # head appended a required parameter: there is no identical-input comparison
    # left to make, and the change is already visible in the diff
    assert "scale" not in hit, "arity change reported as a behaviour delta"
    assert "signature changed" in skipped.get("scale", ""), \
        f"scale should be skipped as a signature change, got {skipped.get('scale')!r}"

    # a parameter annotated with a project type gets a real instance built for it
    assert "fmt" in hit, f"missed a change behind a project-typed parameter; found {hit}"

    # things that must stay quiet
    assert "slug" not in hit, "equivalent rewrite reported as a delta"
    assert "jitter" not in hit, "non-deterministic function reported as a delta"
    assert "where" not in hit, "the two checkout paths reported as a behaviour delta"
    assert "check" not in hit, "a test file was probed by default"
    assert "test file" in skipped.get("test_calc.py", ""), \
        f"test file should be skipped by name, got {skipped.get('test_calc.py')!r}"
    assert rep.flaky > 0, "flake filter never fired on a random() function"
    assert "Cart.__init__" in skipped, "constructor should be skipped with a reason"

    # a docstring is a node, so an edit to one makes the AST differ -- but there is
    # nothing an output comparison can see, so it never enters the blast radius
    assert "label" not in hit, "a docstring edit reported as a behaviour delta"
    assert "label" not in skipped, "a docstring edit cost a probe budget"

    # an annotation is a node too, and is either a string or evaluated once at
    # import: the twin run compares outputs, not `__annotations__`
    assert "tally" not in hit, "an annotation edit reported as a behaviour delta"
    assert "tally" not in skipped, "an annotation edit cost a probe budget"

    # only a positional call is ever made, so a parameter name is not observable
    # -- and the rename moves the body with it, which is what makes it look like
    # a change until the names go into positional slots
    assert "norm" not in hit, "a parameter rename reported as a behaviour delta"
    assert "norm" not in skipped, "a parameter rename cost a probe budget"

    # a change behind `if n == 987654321`: no corpus guesses that, but it is a
    # literal in the source, so the guard miner hands it over as a probe value
    assert "gate" in hit, f"missed a change behind a mined guard literal; found {hit}"

    # the same shape with the constant behind arithmetic: nothing to mine, so it
    # stays unreached -- running the function is not verifying the edit
    assert "warp" not in hit, "an unreachable branch reported as a behaviour delta"
    assert "no probe reached" in skipped.get("warp", ""), \
        f"unreached edit should not count as checked, got {skipped.get('warp')!r}"

    assert rep.checked == 10, f"expected 10 callables twin-run, got {rep.checked}"

    # `Any` inside a subscript is a type argument, not the type: a dict whose
    # values are Any is still a dict, and a file annotated IO[Any] is still a
    # file. Reading it as untyped probes both of them with `0`.
    assert _values("dict[str, t.Any]")[0] == "{}", "a type argument discarded the container"
    assert "BytesIO" in _values("t.IO[t.Any]")[0], "a file parameter is not modelled"
    assert _values("t.Any") == _values(""), "a bare Any should still get the spread"

    # Reaching the change is the whole chain -- hunk parse, payload, line trace,
    # tally -- and any broken link in it reports zero.
    assert 0 < rep.reached <= rep.probes, \
        f"{rep.reached} probes reached the change, out of {rep.probes}"

    groups = cluster(rep.deltas)
    assert len(groups) < len(rep.deltas), "clustering collapsed nothing"
    per_name = {}
    for g in groups:
        per_name.setdefault(g[0].qualname, 0)
        per_name[g[0].qualname] += 1
    assert per_name["discount"] == 1, f"one root cause split into {per_name['discount']} findings"

    print(f"ok  {len(groups)} findings from {len(rep.deltas)} deltas, "
          f"{rep.probes} probes ({rep.reached} reached), "
          f"{rep.flaky} flaky, {rep.checked} checked")
    for g in groups:
        d = g[0]
        print(f"    {d.qualname:<14} {d.base['type']:>8} -> {d.head['type']:<8} ({len(g)} calls)")


if __name__ == "__main__":
    main()
