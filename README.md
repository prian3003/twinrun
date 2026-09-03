# twinrun

Run the old and the new version of changed code on identical inputs, and report
where the outputs differ.

The old code is the oracle. You don't write a specification, you don't write
assertions, and there is nothing to configure — if the behaviour of something
changed and you didn't mean it to, twinrun shows you the exact call that proves it.

```
$ twinrun . --base main --head HEAD
twinrun main..HEAD

  DELTA  billing/cart.py :: Cart.total   +7 more calls
         Cart(0).total()
         base  return       int        0
               after  self=[('items', []), ('rate', 0)]
         head  return       float      0.0
               after  self=[('items', []), ('rate', 0)]

  DELTA  billing/cart.py :: Cart.add     +21 more calls
         Cart(0).add(1)
         base  return       NoneType   None
               after  self=[('items', [1]), ('rate', 0)]
         head  return       NoneType   None
               after  self=[('items', [2]), ('rate', 0)]

2 findings from 30 calls · 6 callables checked · 72 probes · 8 flaky dropped
```

Exits 1 when there is a finding, 2 on a usage error, so it drops into CI as-is.

## How it works

1. **Blast radius** — diff the two revisions, parse both sides, keep the callables
   whose AST actually changed, plus one level of their callers in the same file.
   Extracting a helper leaves its callers byte-identical while their behaviour
   moves underneath them, and the caller is the name anyone actually calls.
   Comments, formatting, docstrings, annotations and parameter names never reach
   step 2. Each of them is a node, so editing one makes the AST differ, and none
   of them is something an output comparison can see. Dropping them takes the
   itsdangerous sweep below from 4262 probes to 2847 and removes five commits
   from it entirely, one of which was spending 264 probes on a docs split.
   Annotations are dropped from parameters and return types only: the one on a
   class-level assignment stays, because a dataclass field annotation is not a
   comment on the behaviour, it is the behaviour. Parameter names go into
   positional slots, signature and body together, because a twin run only ever
   calls positionally — renaming by position rather than by identity is what
   makes that safe, since `f(a, b) -> a` and `f(b, a) -> b` really do return the
   same thing for `f(1, 2)`. Half the sweep's "signature changed" skips turned
   out to be renames.
2. **Signatures** — ask the interpreter, in each worktree, what the target and its
   constructor actually take. The parse tree cannot see a constructor inherited
   from a base class in another module, cannot expand a type alias, and cannot
   resolve a string annotation; all three are ordinary in real code.
3. **Producers** — a corpus of edge values cannot build a signed token or a parsed
   config, but the module that consumes one usually contains the function that
   makes one. Module functions and instance methods with a return annotation are
   called to fill parameters of that type, including from the sibling modules
   this file imports from -- `timed.py` takes what `signer.py` signs. Type
   aliases are expanded, an inherited `__init__` is resolved to the base class
   that defines it, and the repository's own tests are read for inputs: nobody's
   edge-value corpus produces a validly signed payload or guesses a separator a
   constructor will accept, and the test suite has both. Long string and bytes
   literals are taken as values, and a call whose arguments are all literals is
   taken as a construction -- `Signer("secret-key")`, or the same thing written
   as a `partial` in a pytest fixture. Two literals rank ahead of the producers
   and the rest behind, so the input someone wrote down competes with the input
   a round trip builds instead of always losing to it. The literals are ranked
   among themselves first, because a suite's long strings are a mix of two things
   and only one of them is an input: `'[42].-9cNi0CxsSB3hZPNCe9a2eEs1ZM'` is a
   payload, a separator and a digest, while `'not supported'` is prose quoted
   from an assertion about an error message. Whitespace says prose, a separator
   says structure. Producers are read from
   the base revision only. A function that exists solely in head would raise
   `NameError` on one side and return a value on the other, which is a delta on
   every callable that consumes its type. Anything that changed is excluded, directly or by
   reference: a producer whose own behaviour moved would hand the two sides
   different inputs, and then nothing being compared means anything.
4. **Probes** — build inputs from each parameter's type, using a small
   corpus of edge values (`0`, `-1`, `2**31`, `''`, `float('nan')`, `[]`, …). For a
   method, the constructor's parameters are probed in the same sweep, so the
   instance is part of the input.

   A construction harvested from the tests replaces that sweep when there is one:
   a constructor with six parameters spends six probe columns getting itself
   built, and one the test suite already wrote spends one and is known to work.
   Any commit that touches an `__init__` gives them up, since `__init__` resolves
   through inheritance and the one that moved is not always the one named.

   An unannotated parameter gets the producers and the fixtures too, not just
   the spread. A codebase with no type hints is exactly the one where the
   harvest is all there is, and the value that gets past a signature check is
   the token the test suite wrote down, never `0`.

   A change behind `if version == 0x8f` is not something an edge-value corpus
   guesses, so the constant is read off the branch that encloses the moved lines
   and put at the front of that parameter's column, where the first probe picks
   up every column's front at once — which is what a guard reading `a == 1 and
   b == 2` needs. Only `==` and `in` are mined, against a parameter or a `self`
   attribute; a `<` names a direction rather than a value, and the corpus
   already carries both ends of the range.

   `Any` says nothing on its own, but `dict[str, Any]` and `IO[Any]` say plenty:
   the type argument is not the type. Reading the whole annotation as untyped is
   how a file parameter ends up probed with `0`, raising `'int' object has no
   attribute 'read'` on the first line of every probe, so `Any` only decides the
   answer when nothing modelled is left underneath it.

   Every value in every column is covered before anything is sampled, with the
   columns advancing together, because a change that only shows up when two
   parameters are both interesting is invisible to a sweep that holds one of
   them at zero. What budget is left after that goes one factor at a time —
   vary one column, hold the others at their first value — since advancing them
   together also pairs a signed token with a garbage max age, and the call dies
   before the signature is ever checked.

   A parameter annotated with one of your own classes gets a real instance built
   for it, by probing that class's `__init__` one level deep. A constructor's
   optional parameters are left at their defaults, since inventing values for them
   mostly fails to build anything and the default is what real calls use. Anything else that
   is a bare name — an imported type, something it has never heard of — is tried
   as a no-argument constructor, resolved in the target module's own namespace.
   When that cannot be built the failure is identical on both sides, so a type it
   cannot model costs a probe and reports nothing. If nothing usable could be
   built at all, the callable is listed as skipped rather than counted as checked.
5. **Twin run** — check out both revisions as git worktrees and call the target in
   a subprocess on each side with the same inputs. Return value, type, exception,
   stdout, argument mutation and instance state are all recorded. A method that
   returns `None` and quietly changes `self` is the common case, not an edge case.

   A line trace scoped to the target file records which probes executed a line
   the commit actually moved, because calling a changed callable is not the same
   as reaching the change inside it. Across 41 real itsdangerous commits, 1349 of
   2847 probes reach it; the rest run the function around the edit. A
   callable that nothing reached and that produced no delta is reported as
   skipped rather than counted as checked — on that sweep, 39 of 126 callables
   were being called verified without a probe ever touching the diff. A delta
   overrides the reach test: a moved default argument or class attribute is
   evaluated at import, before the trace starts, and differs anyway.
6. **Normalise** — the two revisions are checked out at different paths, so
   anything that surfaces its own location (a cwd, a `__file__`, a path inside an
   error message) would differ for a reason that has nothing to do with the
   change. Both checkout roots collapse to `<repo>` before anything is compared,
   and the address inside a default `repr` collapses with them — left in, it makes
   every object-valued result look non-deterministic.
7. **Flake filter** — every probe runs twice per side. If a side disagrees with
   itself the probe is non-deterministic, and it is dropped rather than reported.
   Clocks, RNG, hash ordering and network calls come out here.

   The runs interleave the two sides rather than finishing one side and then the
   other, which is what lets the filter see wall-clock drift. A producer that
   embeds a timestamp gives the same answer twice in a row and a different one a
   second later, so back-to-back base runs agree with each other, head's agree
   with each other, and the two sides differ for a reason that has nothing to do
   with the commit. Interleaved, both sides straddle the same window and the
   drift surfaces as a side disagreeing with itself. Two runs of the itsdangerous
   sweep now report the same 55 findings; before, the count moved between runs.

   Two runs catch noise with a wide range of outcomes. A target that returns one
   of only a handful of values can still agree with itself by chance — roughly a
   one-in-six coin flip stays quiet about 17% of the time — so raise `--repeats`
   when you are verifying something like that.
8. **Cluster** — one root cause is one finding. Thirty calls that all differ
   `int → float` are reported once, with a count.

## Install

```
make install        # pip install -e .
```

No dependencies. Python 3.10+.

## Use

```
make all                                  # self-check, then twinrun on its own last commit
make demo REPO=~/work/api BASE=main HEAD=my-branch
twinrun ~/work/api --base main --head my-branch
```

| flag | default | meaning |
|---|---|---|
| `--base` | `HEAD~1` | revision treated as the oracle |
| `--head` | `HEAD` | revision under test |
| `--limit` | `24` | max probes per callable |
| `--timeout` | `20` | seconds per side, per callable |
| `--seed` | `0` | probe sampling seed |
| `--repeats` | `2` | runs per side used to detect non-determinism |

CI runs the self-check on 3.10/3.12/3.13, and on every pull request twinrun
verifies that pull request against its own base branch.

## What it covers

Module-level functions, instance methods, `@staticmethod` and `@classmethod`.

`*args`, `**kwargs`, and keyword-only parameters that have defaults: none of them
needs a value, so the callable is probed on its positional parameters.

Skipped, with the reason printed: `async def`, other decorators, a keyword-only
parameter with no default, `__init__` (observed through the instance state its methods report), signatures that
changed in a way that leaves no identical-input comparison, callables for which
no usable input could be built, and callables no probe reached. Untyped parameters get a generic spread, which
usually lands on `TypeError` identically on both sides and reports nothing.

## Prior art

Most tools that look adjacent are solving a different problem.

**Diff readers** — Copilot code review, CodeRabbit, Greptile, Qodo, Cursor
BugBot. An LLM reads the patch and writes comments. Nothing runs, so nothing is
measured: they can suspect that a division changed type, and twinrun can tell you
that `Cart(0).total()` returned `0` before the commit and `0.0` after.

**Static analysis** — Semgrep, CodeQL, Sonar. Rules against one revision. They
find the bug classes someone wrote a rule for, which is the complement of what a
differential run finds: the bug nobody anticipated, in code specific to you.

**Test generators** — Pynguin, EvoSuite, Diffblue. Also synthesise inputs, but
against one revision, and then have to invent assertions. twinrun never invents
an assertion. The old code is the assertion.

**API surface diff** — `cargo-semver-checks`, `japicmp`, `apidiff`, libabigail.
Compare signatures across versions. A function that keeps its signature and
changes its answer is exactly what they are built not to notice.

**Production traffic diffing** — GitHub Scientist, Twitter's and Meta's Diffy.
The same idea — run both, compare — at request level, on real traffic, after you
have shipped. twinrun is the pre-merge, function-level form, and needs no traffic.

**CrossHair `diffbehavior`** is the real overlap: same premise, opposite engine.
It explores the two functions symbolically with z3 rather than running them.
Measured against it on a 2-parameter integer function with an `int`/`float`
division bug, it wins on completeness — a solver proves there is no differing
input, where a corpus can only fail to find one — and took 30.2s. Measured
against itsdangerous `37f0997`, where `TimestampSigner.unsign` routes through
`hmac` and `hashlib`, it ran 20m01s and 7391 iterations and reported no
difference; z3 has no theory for SHA-1, so it cannot construct a signature that
verifies, and every path past that check stays unreachable. twinrun does that
commit in 1.5s — 4 callables, 84 probes, 3 findings — because concrete execution
does not care what a function is made of, and because it finds the four callables
itself from the git range instead of being handed a pair of names.

## Limits today

Probes run your code for real. There is no sandbox yet, so a function that writes
files or hits the network will do that, twice per side. Point it at a repo whose
test suite you would already run.

The probe corpus is fixed, beyond what the module's own producers add. It finds
changes that a boring edge value, a freshly produced one, or something the test
suite wrote down reaches. It misses changes that need a state none of those
three holds. Constructor synthesis stops
at one level: a class whose `__init__` wants another project type falls back to a
no-argument call. Caller propagation stops at one level too, and matches by name
within a file rather than resolving the call graph.

`sandbox.py` builds a repository whose answers are known — nineteen commits a
reviewer would wave through, some of which quietly change behaviour — and scores
a sweep against them, including the cases that are meant to stay silent and the
two that are only reachable through the sandbox's own test suite:

    make sandbox              # build, sweep, score
    make sandbox DIR=/tmp/x   # keep the repo to poke at
