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
from twinrun.core import cluster, verify

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


def summary(amount: int, *parts, **opts) -> str:
    return "total: %d" % amount


def fee(amount: int, *, minimum: int = 5) -> int:
    return max(amount // 10, minimum)
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


class Ledger:
    def __init__(self, tag: str):
        self.tag = tag

    def encode(self, amount: int) -> str:
        return "%s:%d" % (self.tag, amount)

    def decode(self, row: str) -> int:
        return int(row.split(":")[-1])
''',
}

TOKEN = '''
import hashlib
import random
import time

SECRET = "s3cret"
MAX_AGE = 3600


def _mac(payload: str) -> str:
    return hashlib.sha256((payload + SECRET).encode()).hexdigest()[:16]


def sign(payload: str) -> str:
    return "%s.%d.%s" % (payload, int(time.time()), _mac(payload))


def unsign(token: str) -> str:
    payload, _, rest = token.partition(".")
    ts, _, mac = rest.partition(".")
    if mac != _mac(payload):
        raise ValueError("bad signature")
    return payload


def order_id(prefix: str) -> str:
    return "%s-%d" % (prefix, int(random.random() * 1e6))
'''

# (message, expectation, [(file, anchor, replacement), ...])
# An anchor of None means the file is created rather than patched.
# "find"    -- behaviour changed; silence here is a miss.
# "quiet"   -- behaviour identical; a finding here is a false positive.
# "flaky"   -- behaviour is non-deterministic; the filter must drop it, not report it.
# "ceiling" -- behaviour changed, but only behind an input the fixed corpus cannot
#              build. Silence is the honest answer today. When probe synthesis
#              lands this row should flip to "find", and the sweep will say so.
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
    # One edit, two callables downstream of it. Each gets its own finding; neither
    # gets split into one finding per probe.
    ("refactor: clamp the percentage in one place", "find", [
        ("shop/pricing.py",
         "    return round(amount * pct / 100)",
         "    return round(amount * min(pct, 100) / 100)"),
        ("shop/pricing.py",
         "    return amount + amount * rate_pct // 100",
         "    return amount + pct_of(amount, rate_pct)"),
    ]),
    # New file: nothing to compare against, so nothing is probed.
    ("feat: add token signing and order ids", "quiet", [
        ("shop/token.py", None, TOKEN),
    ]),
    ("fix: reject tokens older than the max age", "ceiling", [
        ("shop/token.py",
         "    if mac != _mac(payload):\n"
         "        raise ValueError(\"bad signature\")\n"
         "    return payload\n",
         "    if mac != _mac(payload):\n"
         "        raise ValueError(\"bad signature\")\n"
         "    if time.time() - int(ts) > MAX_AGE:\n"
         "        raise ValueError(\"expired\")\n"
         "    return payload\n"),
    ]),
    # *args and **kwargs need nothing passed, and a keyword-only parameter with a
    # default can stay at it. None of the three is a reason to skip a callable.
    ("chore: pad the summary line", "find", [
        ("shop/pricing.py", '    return "total: %d" % amount',
         '    return "total: %5d" % amount'),
    ]),
    ("fix: apply the minimum fee as a ceiling", "find", [
        ("shop/pricing.py", "    return max(amount // 10, minimum)",
         "    return min(amount // 10, minimum)"),
    ]),
    # No corpus string contains a colon, so every edge value fails identically on
    # both sides. This is only visible to a probe built by calling encode().
    ("refactor: read the ledger field from the front", "find", [
        ("shop/cart.py", '        return int(row.split(":")[-1])',
         '        return int(row.split(":")[0])'),
    ]),
    ("chore: widen the order id", "flaky", [
        ("shop/token.py",
         "    return \"%s-%d\" % (prefix, int(random.random() * 1e6))",
         "    return \"%s-%d\" % (prefix, int(random.random() * 1e9))"),
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
            if anchor is None:
                p.write_text(new)
                continue
            src = p.read_text()
            assert anchor in src, f"{msg}: anchor not found in {name}:\n{anchor}"
            p.write_text(src.replace(anchor, new, 1))
        git(root, "add", "-A")
        git(root, "commit", "-qm", msg)


def verdict(expect, rep):
    """(ok, label). A ceiling that starts firing is progress, not a failure."""
    found = bool(rep.deltas)
    if expect == "find":
        return found, "ok " if found else "MISS"
    if expect == "quiet":
        return not found, "ok " if not found else "FALSE+"
    if expect == "flaky":
        ok = not found and rep.flaky > 0
        return ok, "ok " if ok else ("FALSE+" if found else "NOTDROPPED")
    if found:
        return True, "LIFTED"          # the corpus reached it after all
    return True, "ceil"


def sweep(root: Path):
    n = len(STEPS)
    print(f"sandbox  {n} commits\n")
    tally = {}

    for i, (msg, expect, _) in enumerate(STEPS):
        back = n - i
        rep = verify(root, f"HEAD~{back}", f"HEAD~{back - 1}" if back > 1 else "HEAD")
        ok, label = verdict(expect, rep)
        tally[label] = tally.get(label, 0) + 1

        groups = cluster(rep.deltas)
        names = ",".join(sorted({d.qualname for d in rep.deltas})) or "-"
        print(f"  {label:<10} {msg[:42]:<42} {expect:<7} {names:<26} "
              f"{len(groups)} findings · {rep.checked} checked · "
              f"{rep.probes} probes · {rep.flaky} flaky")

    bad = sum(v for k, v in tally.items() if k not in ("ok ", "ceil", "LIFTED"))
    print("\n" + " · ".join(f"{v} {k.strip().lower()}" for k, v in sorted(tally.items())))
    if tally.get("LIFTED"):
        print("a ceiling case now reports: update its expectation to \"find\"")
    return bad


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
