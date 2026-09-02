"""A sandbox repository with known answers.

Real libraries make poor test subjects: every commit in them is deliberate, so
"did twinrun find the change nobody meant to make" never gets asked. This builds
a small shop package and walks it through commits that a reviewer would wave
through, half of which quietly change behaviour. Every commit declares its own
answer, so the sweep scores itself.

    python3 sandbox.py            # build in a temp dir, sweep, score
    python3 sandbox.py /tmp/shop  # keep it around to poke at
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun.core import verify

SEED = {
    "shop/__init__.py": "",
    "shop/pricing.py": '''
RATES = {"std": 10, "eco": 5, "bulk": 20}


def line_total(price: int, qty: int) -> int:
    return price * qty


def apply_rate(amount: int, kind: str) -> int:
    if kind == "std":
        pct = RATES["std"]
    elif kind == "eco":
        pct = RATES["eco"]
    else:
        pct = 0
    return amount + amount * pct // 100


def clean_code(code: str) -> str:
    code = code.strip()
    if code.startswith("SKU"):
        code = code[3:]
    return code.upper()


def discount(amount: int, pct: int) -> int:
    return amount - amount * pct // 100
''',
    "shop/cart.py": '''
from .pricing import line_total


class Cart:
    def __init__(self, rate: int):
        self.rate = rate
        self.lines = []

    def add(self, price: int, qty: int) -> None:
        self.lines.append(line_total(price, qty))

    def total(self) -> int:
        if not self.lines:
            return 0
        return sum(self.lines) + self.rate
''',
}

# (message, expectation, [(file, anchor, replacement), ...])
# "find"  -- behaviour changed; silence here is a miss.
# "quiet" -- behaviour identical; a finding here is a false positive.
STEPS = [
    ("refactor: pull the percentage math into a helper", "find", [
        ("shop/pricing.py",
         "def discount(amount: int, pct: int) -> int:\n"
         "    return amount - amount * pct // 100\n",
         "def pct_of(amount: int, pct: int) -> int:\n"
         "    return round(amount * pct / 100)\n"          # round, not floor
         "\n\n"
         "def discount(amount: int, pct: int) -> int:\n"
         "    return amount - pct_of(amount, pct)\n"),
    ]),
    ("style: rename locals in apply_rate for clarity", "quiet", [
        ("shop/pricing.py", "        pct = RATES[\"std\"]", "        rate_pct = RATES[\"std\"]"),
        ("shop/pricing.py", "        pct = RATES[\"eco\"]", "        rate_pct = RATES[\"eco\"]"),
        ("shop/pricing.py", "        pct = 0", "        rate_pct = 0"),
        ("shop/pricing.py", "    return amount + amount * pct // 100",
         "    return amount + amount * rate_pct // 100"),
    ]),
    ("perf: look the rate up in the table instead of branching", "find", [
        ("shop/pricing.py",
         '    if kind == "std":\n'
         '        rate_pct = RATES["std"]\n'
         '    elif kind == "eco":\n'
         '        rate_pct = RATES["eco"]\n'
         "    else:\n"
         "        rate_pct = 0\n",
         "    rate_pct = RATES[kind]\n"),                   # unknown kind now raises
    ]),
    ("docs: document line_total", "quiet", [
        ("shop/pricing.py",
         "def line_total(price: int, qty: int) -> int:\n    return price * qty\n",
         "def line_total(price: int, qty: int) -> int:\n"
         '    """Cost of one order line."""\n'
         "    return price * qty\n"),
    ]),
    ("refactor: strip the SKU prefix with lstrip", "find", [
        ("shop/pricing.py",
         '    if code.startswith("SKU"):\n        code = code[3:]\n',
         '    code = code.lstrip("SKU")\n'),               # strips a character set
    ]),
    ("cleanup: tidy the empty-cart check", "find", [
        ("shop/cart.py", "        if not self.lines:", "        if self.lines is None:"),
    ]),
    ("chore: reformat Cart.add", "quiet", [
        ("shop/cart.py",
         "        self.lines.append(line_total(price, qty))",
         "        amount = line_total(price, qty)\n        self.lines.append(amount)"),
    ]),
    ("feat: charge the cart rate as a percentage", "find", [
        ("shop/cart.py",
         "        return sum(self.lines) + self.rate",
         "        return sum(self.lines) * (100 + self.rate) // 100"),
    ]),
    ("refactor: sum the lines with a comprehension", "quiet", [
        ("shop/cart.py",
         "        return sum(self.lines) * (100 + self.rate) // 100",
         "        return sum(x for x in self.lines) * (100 + self.rate) // 100"),
    ]),
]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def build(root: Path):
    """Write the seed, then apply one step per commit."""
    (root / "shop").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "dev")
    for name, src in SEED.items():
        (root / name).write_text(src)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add the shop package")

    for msg, _, edits in STEPS:
        for name, anchor, new in edits:
            p = root / name
            src = p.read_text()
            assert anchor in src, f"{msg}: anchor not found in {name}:\n{anchor}"
            p.write_text(src.replace(anchor, new, 1))
        git(root, "add", "-A")
        git(root, "commit", "-qm", msg)


def sweep(root: Path):
    n = len(STEPS)
    print(f"sandbox  {n} commits\n")
    hits = misses = falses = 0

    for i, (msg, expect, _) in enumerate(STEPS):
        back = n - i
        rep = verify(root, f"HEAD~{back}", f"HEAD~{back - 1}" if back > 1 else "HEAD")
        found = bool(rep.deltas)
        ok = found == (expect == "find")
        if ok:
            hits += 1
        elif expect == "find":
            misses += 1
        else:
            falses += 1

        names = ",".join(sorted({d.qualname for d in rep.deltas})) or "-"
        print(f"  {'ok ' if ok else 'FAIL'}  {msg[:44]:<44} {expect:<5} "
              f"{names:<22} {rep.checked} checked · {rep.probes} probes · {rep.flaky} flaky")

    want = sum(1 for _, e, _ in STEPS if e == "find")
    print(f"\n{hits}/{n} correct · {misses} missed of {want} real changes"
          f" · {falses} false positives")
    return misses + falses


def main():
    if len(sys.argv) > 1:
        root = Path(sys.argv[1]).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        build(root)
        bad = sweep(root)
        print(f"\nkept at {root}")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "shop-repo"
            build(root)
            bad = sweep(root)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
