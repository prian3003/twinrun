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
   Comment and formatting changes never reach step 2.
2. **Signatures** — ask the interpreter, in each worktree, what the target and its
   constructor actually take. The parse tree cannot see a constructor inherited
   from a base class in another module, cannot expand a type alias, and cannot
   resolve a string annotation; all three are ordinary in real code.
3. **Probes** — build inputs from each parameter's type, using a small
   corpus of edge values (`0`, `-1`, `2**31`, `''`, `float('nan')`, `[]`, …). For a
   method, the constructor's parameters are probed in the same sweep, so the
   instance is part of the input.

   A parameter annotated with one of your own classes gets a real instance built
   for it, by probing that class's `__init__` one level deep. A constructor's
   optional parameters are left at their defaults, since inventing values for them
   mostly fails to build anything and the default is what real calls use. Anything else that
   is a bare name — an imported type, something it has never heard of — is tried
   as a no-argument constructor, resolved in the target module's own namespace.
   When that cannot be built the failure is identical on both sides, so a type it
   cannot model costs a probe and reports nothing. If nothing usable could be
   built at all, the callable is listed as skipped rather than counted as checked.
4. **Twin run** — check out both revisions as git worktrees and call the target in
   a subprocess on each side with the same inputs. Return value, type, exception,
   stdout, argument mutation and instance state are all recorded. A method that
   returns `None` and quietly changes `self` is the common case, not an edge case.
5. **Normalise** — the two revisions are checked out at different paths, so
   anything that surfaces its own location (a cwd, a `__file__`, a path inside an
   error message) would differ for a reason that has nothing to do with the
   change. Both checkout roots collapse to `<repo>` before anything is compared,
   and the address inside a default `repr` collapses with them — left in, it makes
   every object-valued result look non-deterministic.
6. **Flake filter** — every probe runs twice per side. If a side disagrees with
   itself the probe is non-deterministic, and it is dropped rather than reported.
   Clocks, RNG, hash ordering and network calls come out here.

   Two runs catch noise with a wide range of outcomes. A target that returns one
   of only a handful of values can still agree with itself by chance — roughly a
   one-in-six coin flip stays quiet about 17% of the time — so raise `--repeats`
   when you are verifying something like that.
5. **Cluster** — one root cause is one finding. Thirty calls that all differ
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
changed in a way that leaves no identical-input comparison, and callables for which
no usable input could be built. Untyped parameters get a generic spread, which
usually lands on `TypeError` identically on both sides and reports nothing.

## Limits today

Probes run your code for real. There is no sandbox yet, so a function that writes
files or hits the network will do that, twice per side. Point it at a repo whose
test suite you would already run.

The probe corpus is fixed. It finds changes that a boring edge value reaches, and
misses changes that need a specific structured input. Constructor synthesis stops
at one level: a class whose `__init__` wants another project type falls back to a
no-argument call. Caller propagation stops at one level too, and matches by name
within a file rather than resolving the call graph.

`sandbox.py` builds a repository whose answers are known — fifteen commits a
reviewer would wave through, some of which quietly change behaviour — and scores
a sweep against them, including the cases that are meant to stay silent:

    make sandbox              # build, sweep, score
    make sandbox DIR=/tmp/x   # keep the repo to poke at
