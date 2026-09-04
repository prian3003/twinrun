"""Self-check. Builds a throwaway repo containing every kind of change twinrun
has to tell apart, and asserts it tells them apart.

Run: python3 test_twinrun.py
"""

import ast
import builtins
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from twinrun._child import GEN_CAP, Sibling, cap
from twinrun._child import call as child_call
from twinrun.core import (_ctor_exprs, _ctor_map, _own, _targets, _typekey, _values,
                          cluster, read_invariants, verify, write_verdicts)

BASE = '''
from abc import abstractmethod
from contextlib import contextmanager
from functools import cached_property


def discount(price: int, pct: int) -> int:
    return price - price * pct // 100


def issue(name: str) -> str:
    return "v1:" + name


def read_name(t: str) -> str:
    if not t.startswith("v1:"):
        raise ValueError("bad token")
    return t[3:]


def mint():
    return b"id-77"


def owner(t: bytes) -> str:
    if not t.startswith(b"id-"):
        raise ValueError("bad id")
    return t[3:].decode()


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.random()


class Money:
    def __init__(self, cents: int):
        self.cents = cents


class Box:
    def __init__(self, size: int):
        self.size = size

    def volume(self) -> int:
        return self.size ** 3


class Crate:
    def __init__(self, n: int):
        self.n = n

    def empty(self) -> bool:
        return not self.n


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


def parse_tag(v):
    if v.startswith("tag:"):
        return v[4:]
    return ""


def area(w: int) -> int:
    return w * w


def scale(x: int) -> int:
    return x * 2


def shorten(text: str) -> str:
    return text.replace(" ", "-")


def clamp(lo: int, hi: int) -> int:
    return lo


class Gauge:
    def __init__(self, n: int, deep: bool = False):
        self.n, self.deep = n, deep

    def read(self) -> int:
        if self.deep:
            return self.n * 10
        return self.n


class Ticket:
    def __init__(self, tag: str = ""):
        if not tag:
            raise ValueError("a ticket needs a tag")
        self.tag = tag

    def label(self) -> str:
        return self.tag.upper()


class Feed:
    def __init__(self):
        raise NotImplementedError

    @abstractmethod
    def heading(self) -> str:
        return "feed:" + self.tag


class RssFeed(Feed):
    def __init__(self, tag: str):
        self.tag = tag


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


def tag(f):                                       # a marker decorator: records
    f.tagged = True                               # something and hands the
    return f                                      # function straight back


@tag
def weight(n: int) -> int:
    return n * 2


class Shelf:
    def __init__(self, n: int):
        self.n = n

    @property
    def load(self) -> int:
        return self.n * 3

    @cached_property
    def depth(self) -> int:
        return self.n + 1


def strict(v):
    return v + 1                                  # TypeError on a string


async def fetch(n: int) -> int:
    return n * 2


@contextmanager
def borrow(n: int):
    yield n * 2
'''

HEAD = '''
from abc import abstractmethod
from contextlib import contextmanager
from functools import cached_property


def discount(price: int, pct: int) -> int:
    return price - price * pct / 100              # int -> float


def issue(name: str) -> str:
    return "v2:" + name                           # the producer moved too


def read_name(t: str) -> str:
    if not t.startswith("v1:"):
        raise ValueError("bad token")
    return t[3:].upper()


def mint():                                       # never annotated, never moved
    return b"id-77"


def owner(t: bytes) -> str:
    if not t.startswith(b"id-"):
        raise ValueError("bad id")
    return t[3:].decode().zfill(4)


def slug(name: str) -> str:
    s = name.strip().lower()                      # equivalent rewrite
    return s.replace(" ", "-")


def jitter(n: int) -> int:
    import random
    return n + random.random() * 2               # non-deterministic


class Money:
    def __init__(self, cents: int):
        self.cents = cents


class Box:
    def __init__(self, size: int):
        self.size = size * 2                      # only the constructor moved

    def volume(self) -> int:
        return self.size ** 3


class Crate:
    def __init__(self, n: int):
        self.n = int(n)                           # rewritten, same behaviour

    def empty(self) -> bool:
        return not self.n


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


def parse_tag(v):                                 # unannotated: the corpus spread
    if v.startswith("tag:"):                      # never gets past this guard
        return v[4:].upper()
    return ""


def area(w: int, h: int = 2) -> int:              # appended an optional parameter
    return w * h                                  # ...and the one-arg answer moved


def scale(x: int, factor: int) -> int:            # appended a required parameter
    return x * factor


def shorten(label: str) -> str:                      # renamed, which a positional
    return label.replace(" ", "_")                # probe cannot see


def clamp(*bounds) -> int:                        # one signature written twice:
    return bounds[1]                              # three positional reach both


class Gauge:
    def __init__(self, n: int, deep: bool = False):
        self.n, self.deep = n, deep

    def read(self) -> int:
        if self.deep:                             # `deep` defaults to the value
            return self.n * 11                    # that never runs this line
        return self.n


class Ticket:                                     # every parameter optional, and
    def __init__(self, tag: str = ""):            # Ticket() raises all the same:
        if not tag:                               # the receiver has to be built
            raise ValueError("a ticket needs a tag")   # from an optional one
        self.tag = tag

    def label(self) -> str:
        return self.tag.title()                   # UPPER -> Title


class Feed:                                       # abstract: nothing builds one,
    def __init__(self):                           # so the receiver has to be a
        raise NotImplementedError                 # subclass the tests construct

    @abstractmethod
    def heading(self) -> str:
        return "feed/" + self.tag                 # : -> /


class RssFeed(Feed):
    def __init__(self, tag: str):
        self.tag = tag


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


def tag(f):
    f.tagged = True
    return f


@tag
def weight(n: int) -> int:
    return n * 3                                  # changed behind a marker


class Shelf:
    def __init__(self, n: int):
        self.n = n

    @property
    def load(self) -> float:
        return self.n * 3.0                       # a property's value changed

    @cached_property
    def depth(self) -> int:
        return self.n + 2                         # a property with the answer kept


def strict(v):
    if isinstance(v, str):                        # the old code refused a
        return v + "1"                            # string; only the inputs it
    return v + 1                                  # never accepted differ


async def fetch(n: int) -> int:
    return n * 3                                  # awaited, and it changed


@contextmanager
def borrow(n: int):                            # calling it runs no line of
    yield n * 3                                   # the body; entering it does
'''


# The suite is read for inputs, so it carries the one string that gets past
# parse_tag's guard -- nobody's edge-value corpus writes "tag:" and then content.
TESTS = '''
def check() -> int:
    return %d


def test_parse_tag():
    assert parse_tag("tag:0123456789abcdef") == "0123456789abcdef"


def test_heading():
    assert RssFeed("weekly").heading().endswith("weekly")
'''


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def fixture(root: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "dev")
    (root / "calc.py").write_text(BASE)
    (root / "test_calc.py").write_text(TESTS % 1)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add calc")
    (root / "calc.py").write_text(HEAD)
    (root / "test_calc.py").write_text(TESTS % 2)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tweak calc")


def main():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        fixture(repo)
        rep = verify(repo, "HEAD~1", "HEAD", limit=24)
        # Every finding ruled intended, then the same run again: a check that
        # reports the same accepted change forever is one people turn off.
        accepted = write_verdicts(repo, rep.deltas, "self-check")
        again = verify(repo, "HEAD~1", "HEAD", limit=24)

    assert accepted == len(cluster(rep.deltas)), \
        f"accepted {accepted} of {len(cluster(rep.deltas))} findings"
    assert not again.deltas, f"{len(again.deltas)} accepted findings reported again"
    assert len(cluster(again.known)) == accepted, "accepted findings went missing"

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

    # a marker decorator hands the function back unchanged, so the name is still
    # the plain function and there is nothing about it a probe cannot call
    assert "weight" in hit, f"missed a change behind a marker decorator; found {hit}"
    assert "weight" not in skipped, f"got {skipped.get('weight')!r}"

    # a property is called by reading it, and what it computes is behaviour
    assert "Shelf.load" in hit, f"missed a property's changed value; found {hit}"

    # the old code raised TypeError on every input the two sides disagree about:
    # it never accepted them, so there is no behaviour there to have changed
    assert "strict" not in hit, "a delta on an input the old code refused"
    assert rep.refused > 0, "the refused filter never fired"

    # an async def is an ordinary callable whose answer arrives through a loop
    assert "fetch" in hit, f"missed a change in an async def; found {hit}"
    assert "fetch" not in skipped, f"got {skipped.get('fetch')!r}"

    # things that must stay quiet
    assert "slug" not in hit, "equivalent rewrite reported as a delta"
    assert "jitter" not in hit, "non-deterministic function reported as a delta"
    assert "where" not in hit, "the two checkout paths reported as a behaviour delta"
    assert "check" not in hit, "a test file was probed by default"
    assert "test file" in skipped.get("test_calc.py", ""), \
        f"test file should be skipped by name, got {skipped.get('test_calc.py')!r}"
    assert rep.flaky > 0, "flake filter never fired on a random() function"
    # A constructor answers with the instance it built: `[]` and `list()` leave
    # the same one, so there is nothing to report and nothing to skip either.
    assert "Cart.__init__" not in hit, "an equivalent constructor rewrite reported as a delta"
    assert "Cart.__init__" not in skipped, f"got {skipped.get('Cart.__init__')!r}"

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

    # an unannotated parameter still gets the producers and the fixtures: the
    # value that gets past a guard is the one the test suite wrote down
    assert "parse_tag" in hit, f"missed a change behind an unannotated parameter; found {hit}"

    # a change behind `if n == 987654321`: no corpus guesses that, but it is a
    # literal in the source, so the guard miner hands it over as a probe value
    assert "gate" in hit, f"missed a change behind a mined guard literal; found {hit}"

    # the same shape with the constant behind arithmetic: nothing to mine, so it
    # stays unreached -- running the function is not verifying the edit
    assert "warp" not in hit, "an unreachable branch reported as a behaviour delta"
    assert "no probe reached" in skipped.get("warp", ""), \
        f"unreached edit should not count as checked, got {skipped.get('warp')!r}"

    # `issue` moved, so it used to be dropped as a producer and `read_name` was
    # left with corpus strings that all miss its prefix. Frozen in base it is a
    # value, not code: both sides get the token the old revision issued, which
    # is the only way the delta inside `read_name` is visible at all.
    assert "read_name" in hit, f"missed a change behind a moved producer; found {hit}"
    rn = next(d for d in rep.deltas if d.qualname == "read_name")
    assert rn.base["kind"] == rn.head["kind"] == "return", \
        f"the two sides got different tokens: {rn.base} -> {rn.head}"

    # `mint` declares no return type, so the source says nothing about what it
    # makes and it was skipped as a producer. Calling it once in base says
    # `bytes`, which is the only way `owner` gets past its own prefix check.
    assert "owner" in hit, f"missed a change behind an unannotated producer; found {hit}"

    # Only Box.__init__ moved. Its own body is skipped -- a constructor returns
    # nothing to compare -- and no method body names it, so the methods have to
    # be taken by class or the commit is never probed at all. The moved line
    # runs while the instance is built, before the call, so the trace has to
    # cover the setup too or the finding is dropped as unreached.
    assert "Box.volume" in hit, f"missed a constructor-only change; found {hit}"
    assert "Box.volume" not in skipped, \
        f"the constructor ran before the trace started: {skipped.get('Box.volume')!r}"
    assert "Box.__init__" in hit, "the constructor's own change was not reported"

    # Crate's constructor was rewritten and its methods answer the same either
    # way, so there is no delta to fall back on: coverage of the edit is the
    # only thing that can say the commit was verified rather than merely run.
    assert "Crate.empty" not in hit, "an equivalent constructor rewrite reported as a delta"
    assert "Crate.empty" not in skipped, \
        f"the constructor ran outside the trace: {skipped.get('Crate.empty')!r}"

    assert rep.checked == 30, f"expected 30 callables twin-run, got {rep.checked}"
    # Nothing builds a Feed, so the receiver has to be the RssFeed the suite
    # constructs. Without that the whole class is skipped for want of an input.
    assert any(d.qualname == "Feed.heading" for d in rep.deltas), \
        "an abstract receiver went unprobed"
    # A probe passes values by position, so a renamed parameter is the same
    # call written differently and the two sides are still comparable.
    assert any(d.qualname == "shorten" for d in rep.deltas), \
        "a renamed parameter was read as a signature change"
    assert any(d.qualname == "clamp" for d in rep.deltas), \
        "a *args signature was read as a signature change"
    assert any(d.qualname == "Gauge.read" for d in rep.deltas), \
        "a moved line behind an optional constructor flag went unreached"
    # @cached_property is @property with the answer kept, and @abstractmethod is
    # a marker: neither is a reason to skip, and Feed.heading below carries one.
    assert any(d.qualname == "Shelf.depth" for d in rep.deltas), \
        "a cached property went unread"
    # Calling a @contextmanager runs no line of the body, so without entering it
    # the probe compares two identical manager objects and reaches nothing.
    assert any(d.qualname == "borrow" for d in rep.deltas), \
        "a context manager was compared without being entered"
    # Ticket() raises, so the receiver has to be built from a parameter that
    # carries a default. Without the retry the whole class is skipped for want
    # of an input and its changed label() is never compared.
    assert any(d.qualname == "Ticket.label" for d in rep.deltas), \
        "a receiver built only from optional parameters went unprobed"

    # `Any` inside a subscript is a type argument, not the type: a dict whose
    # values are Any is still a dict, and a file annotated IO[Any] is still a
    # file. Reading it as untyped probes both of them with `0`.
    assert _values("dict[str, t.Any]")[0] == "{}", "a type argument discarded the container"
    assert "BytesIO" in _values("t.IO[t.Any]")[0], "a file parameter is not modelled"
    assert _values("t.Any") == _values(""), "a bare Any should still get the spread"

    # `none` is a corpus type, so the None that only ever meant "optional" was
    # answering for the whole annotation: the search walks the names in order,
    # misses a type it does not model, and settles on the None behind it. Every
    # optional parameter of a project type was probed with None and nothing
    # else -- which is why click's ProgressBar, whose iterable is
    # `Iterable[V] | None`, raised in setup for all thirteen of its methods.
    ctx = {"Ctx": [("n", "str")]}
    assert any(v.startswith("Ctx(") for v in _values("Ctx | None", ctx)), \
        f"the None of an optional answered for the type: {_values('Ctx | None', ctx)}"
    # and the element type is not the container: Iterable[V] is a list, not a
    # TypeVar to call
    assert _values("cabc.Iterable[V] | None")[0] == "[]", \
        f"a container resolved to its type argument: {_values('cabc.Iterable[V] | None')}"

    # A container of a type the corpus does model is filled from that type,
    # test-suite literals first: `Option(['a', 'b'])` raises before click's own
    # constructor starts, and `['--flag-one']` does not.
    seq = _values("cabc.Sequence[str] | None", producers={"str": ["'--flag-one'"]})
    assert seq[:2] == ["['--flag-one']", "['']"], f"element type unused: {seq}"
    # An annotation nothing models still says whether None is allowed, and for
    # click's `type: ParamType | Any | None` that is the value every real call
    # passes.
    assert _values("ParamType | t.Any | None")[0] == "None", \
        f"an optional the corpus cannot model lost its None: {_values('ParamType | t.Any | None')}"

    # A str column drops a hint the corpus already holds; an Any column keeps
    # every one. So the two fall out of step, and click's zsh formatter -- which
    # escapes a colon in the value if and only if the help is not the sentinel
    # -- is separated by no probe the diagonal builds: index 2 puts the colon in
    # the value and the empty string in the help, and an empty help *is* the
    # sentinel. The one construction that answers the question has to be built
    # on purpose.
    item = {"CompletionItem": [("value", "t.Any"), ("type", "str"), ("help", "str")]}
    built = _ctor_exprs("CompletionItem", item, hints=["'_'", "'\\n'", "':'"])
    assert "CompletionItem(':', ':', ':')" in built, \
        f"no construction holds the changed line's literal in every column: {built}"

    # A construction is synthesised from every module's constructors and
    # evaluated in the namespace of one of them: click's termui can name five
    # of the fifty-nine classes in that map. The name is one import away, and
    # both revisions resolve it the same way, so lending it cannot invent a
    # difference -- it only lets the probe run.
    env = Sibling(sys.modules["twinrun.core"])
    assert env["Delta"].__name__ == "Delta", "a module cannot name its own"
    assert "Sibling" not in vars(sys.modules["twinrun.core"]), "pick a name core lacks"
    assert env["Sibling"] is Sibling, "a sibling's name went unresolved"
    try:
        env["no_such_name_anywhere"]
        raise AssertionError("an absent name has to stay absent")
    except KeyError:
        pass

    # A class takes its __init__ from the ancestor that defines it, and that is
    # the only constructor whose change can invalidate the constructions the
    # test suite performs for it. The disqualifying test used to ask `moved` for
    # a bare "__init__", and `moved` carries the tail of every changed method
    # alongside its qualified name -- so one touched constructor anywhere took
    # the harvested constructions away from every class in the package.
    owners = {}
    _ctor_map(["""
class Signer:
    def __init__(self, key): pass
class Timed(Signer): pass
class Other:
    def __init__(self, n): pass
"""], owners)
    assert owners["Signer"] == "Signer", owners
    assert owners["Timed"] == "Signer", "an inherited __init__ names its definer"
    assert owners["Other"] == "Other", owners
    moved = {"Signer.__init__", "__init__"}
    kept = [c for c in ("Signer", "Timed", "Other")
            if f"{owners.get(c, c)}.__init__" not in moved]
    assert kept == ["Other"], f"the wrong classes were disqualified: {kept}"

    # A hunk names lines in a file, and a callable is asked whether a probe ran
    # one of its own. Probing a method builds the instance first, so a commit
    # that touches __init__ alongside a method had every method on the class
    # reporting coverage of a change its own body never executed.
    src = """
class Box:
    def __init__(self, n):
        self.n = n
    def size(self):
        return self.n
"""
    t = _targets(src)
    lines = list(range(1, 7))
    assert _own(t["Box.__init__"][0], lines) == [3, 4], _own(t["Box.__init__"][0], lines)
    assert _own(t["Box.size"][0], lines) == [5, 6], _own(t["Box.size"][0], lines)

    # A generator function returns without running a line of its own body: the
    # probe compared two <generator object> reprs, which match on every commit,
    # and no moved line was ever traced. Draining a bounded prefix runs the
    # body and makes the yields the answer.
    def counted(n):
        for i in range(n):
            yield i * 2

    def endless():
        i = 0
        while True:
            yield i
            i += 1

    env = {"__builtins__": builtins}
    out, _ = child_call(types.SimpleNamespace(g=counted), env,
                        {"kind": "function", "qualname": "g", "n_ctor": 0}, ["3"])
    assert out["value"] == "[0, 2, 4]", out
    out, _ = child_call(types.SimpleNamespace(g=endless), env,
                        {"kind": "function", "qualname": "g", "n_ctor": 0}, [])
    assert len(ast.literal_eval(out["value"])) == GEN_CAP, "an endless one still stops"

    # A type variable is not a type. The interpreter renders one as "~V", and
    # taking that for a class the module defines made the probe `V()`, which
    # raises "'typing.TypeVar' object is not callable" and takes every probe
    # for that callable with it.
    # A temporary name is different every run, so a function that makes one
    # answers differently on both sides and its probe is dropped as flaky.
    scratch = str(Path(tempfile.gettempdir()) / "tmpq1w2e3" / "note.txt")
    assert cap(scratch) == "<tmp>/note.txt", cap(scratch)
    warn = "<repo>/src/pkg/types.py:744: RuntimeWarning: bool used as a fd"
    assert cap(warn).startswith("<repo>/src/pkg/types.py:<line>:"), cap(warn)
    assert _values("~V") == _values(""), "an unbound type variable says what Any says"
    assert _values("t.Iterable[~V]")[0] == "[]", _values("t.Iterable[~V]")

    # A producer is keyed by what the parse tree said; a parameter asks by what
    # the interpreter resolved. Two spellings of one type have to meet, or a
    # module's own `dumps` stays invisible to its own `loads`.
    assert _typekey("t.Union[str, bytes]") == _typekey("str | bytes")
    assert not _typekey("t.Iterator[Signer]") & _typekey("Signer"), \
        "a producer of an iterator was offered for the thing it iterates"

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


def stored_invariant_gate():
    """A verdict outlives the commit it was given on.

    The blast radius is what makes twinrun cheap, and it is also the hole: a
    callable the commit does not touch is a callable the run never probes. Here
    `total` lives in a file the third commit never opens, so the diff has no
    reason to look at it and does not. The stored answer is the only thing that
    still knows what it was supposed to return.
    """
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                       check=True, capture_output=True)
        git(repo, "config", "user.email", "dev@example.com")
        git(repo, "config", "user.name", "dev")

        (repo / "util.py").write_text("def scale(x: int) -> int:\n    return x * 2\n")
        (repo / "api.py").write_text(
            "from util import scale\n\n\ndef total(n: int) -> int:\n"
            "    return scale(n) + 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "add util and api")

        # A change to `total` itself, so there is a finding to rule on.
        (repo / "api.py").write_text(
            "from util import scale\n\n\ndef total(n: int) -> int:\n"
            "    return scale(n) + 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "total adds two")

        first = verify(repo, "HEAD~1", "HEAD", limit=8)
        assert any(d.qualname == "total" for d in first.deltas), \
            "the change to total was not reported, so there is nothing to rule on"
        write_verdicts(repo, first.deltas, "intended")
        invs = read_invariants(repo)
        assert any(v["qualname"] == "total" for v in invs.values()), \
            f"accepting a finding stored no invariant for it: {invs}"

        # Now break `total` from a file the commit does touch. `total` is not in
        # the radius: it is in another file, and callers are only followed one
        # level inside the file that changed.
        (repo / "util.py").write_text("def scale(x: int) -> int:\n    return x * 3\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "scale by three")

        second = verify(repo, "HEAD~1", "HEAD", limit=8)

    from_diff = {d.qualname for d in second.deltas if d.source == "diff"}
    from_store = {d.qualname for d in second.deltas if d.source == "store"}
    assert "total" not in from_diff, \
        "total was in the blast radius, so this proves nothing about the store"
    assert "total" in from_store, \
        f"a stored invariant did not catch the regression: {from_store}"
    assert second.rechecked, "no invariant was re-checked"
    broke = next(d for d in second.deltas if d.qualname == "total")
    assert broke.base["value"] != broke.head["value"], broke
    print(f"ok  a stored invariant caught {sorted(from_store)} "
          f"that the diff passed; {second.rechecked} re-checked")


if __name__ == "__main__":
    main()
    stored_invariant_gate()
