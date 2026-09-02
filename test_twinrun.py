"""Self-check. Builds a throwaway repo with three kinds of change and asserts
twinrun tells them apart: a real behaviour change, an equivalent rewrite, and a
non-deterministic function.

Run: python3 test_twinrun.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun.core import verify

BASE = '''
def discount(price: int, pct: int) -> int:
    return price - price * pct // 100


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.randint(0, 5)
'''

HEAD = '''
def discount(price: int, pct: int) -> int:
    return price - price * pct / 100          # behaviour change: int -> float


def slug(name: str) -> str:
    s = name.strip().lower()                  # equivalent rewrite
    return s.replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.randint(0, 6)           # non-deterministic, unverifiable
'''


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def fixture(root: Path):
    git_init = ["git", "init", "-q", "-b", "main", str(root)]
    subprocess.run(git_init, check=True, capture_output=True)
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
    assert "discount" in hit, f"missed the real behaviour change; deltas={hit}"
    assert "slug" not in hit, "equivalent rewrite reported as a delta"
    assert "jitter" not in hit, "non-deterministic function reported as a delta"
    assert rep.flaky > 0, "flake filter never fired on a random() function"
    assert rep.checked == 3, f"expected 3 functions twin-run, got {rep.checked}"

    d = next(d for d in rep.deltas if d.qualname == "discount")
    assert d.base["value"] != d.head["value"]

    print(f"ok  {len(rep.deltas)} deltas, {rep.probes} probes, {rep.flaky} flaky, "
          f"{rep.checked} checked")
    print(f"    e.g. discount({', '.join(d.args)})  {d.base['value']} -> {d.head['value']}")


if __name__ == "__main__":
    main()
