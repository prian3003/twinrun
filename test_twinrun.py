"""Self-check. Builds a throwaway repo containing every kind of change twinrun
has to tell apart, and asserts it tells them apart.

Run: python3 test_twinrun.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun.core import cluster, verify

BASE = '''
def discount(price: int, pct: int) -> int:
    return price - price * pct // 100


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.random()


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
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add calc")
    (root / "calc.py").write_text(HEAD)
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

    # things that must stay quiet
    assert "slug" not in hit, "equivalent rewrite reported as a delta"
    assert "jitter" not in hit, "non-deterministic function reported as a delta"
    assert rep.flaky > 0, "flake filter never fired on a random() function"
    assert "Cart.__init__" in skipped, "constructor should be skipped with a reason"

    assert rep.checked == 7, f"expected 7 callables twin-run, got {rep.checked}"

    groups = cluster(rep.deltas)
    assert len(groups) < len(rep.deltas), "clustering collapsed nothing"
    per_name = {}
    for g in groups:
        per_name.setdefault(g[0].qualname, 0)
        per_name[g[0].qualname] += 1
    assert per_name["discount"] == 1, f"one root cause split into {per_name['discount']} findings"

    print(f"ok  {len(groups)} findings from {len(rep.deltas)} deltas, "
          f"{rep.probes} probes, {rep.flaky} flaky, {rep.checked} checked")
    for g in groups:
        d = g[0]
        print(f"    {d.qualname:<14} {d.base['type']:>8} -> {d.head['type']:<8} ({len(g)} calls)")


if __name__ == "__main__":
    main()
