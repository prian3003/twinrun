# twinrun

Run the old and the new version of a changed function on identical inputs, and
report where the outputs differ.

The old code is the oracle. You don't write a specification, you don't write
assertions, and there is nothing to configure — if the behaviour of a function
changed and you didn't mean it to, twinrun shows you the exact call that proves it.

```
$ twinrun . --base HEAD~1 --head HEAD
twinrun HEAD~1..HEAD

  DELTA  billing/calc.py :: discount(0, 7)
         base  return       0
         head  return       0.0

1 delta · 3 functions checked · 8 probes · 4 flaky dropped
```

Exits 1 when there is a delta, so it drops into CI as-is.

## How it works

1. **Blast radius** — diff the two revisions, parse both sides, keep the
   module-level functions whose AST actually changed. Comment and formatting
   changes never reach step 2.
2. **Probes** — build inputs from each parameter's type annotation, using a small
   corpus of edge values (`0`, `-1`, `2**31`, `''`, `float('nan')`, `[]`, …).
3. **Twin run** — check out both revisions as git worktrees and call the function
   in a subprocess on each side with the same inputs. Return value, exception and
   stdout are all recorded.
4. **Flake filter** — every probe runs twice per side. If a side disagrees with
   itself, the probe is non-deterministic, and it is dropped rather than reported.
   Clocks, RNG, hash ordering and network calls come out here.
5. **Diff** — what survives and differs between base and head is a delta.

## Install

```
pip install -e .
```

No dependencies. Python 3.10+.

## Options

| flag | default | meaning |
|---|---|---|
| `--base` | `HEAD~1` | revision treated as the oracle |
| `--head` | `HEAD` | revision under test |
| `--limit` | `24` | max probes per function |
| `--timeout` | `20` | seconds per side, per function |
| `--seed` | `0` | probe sampling seed |

## Limits today

Skipped with a reason printed: methods, `async def`, decorated functions,
`*args`/`**kwargs`, and parameters annotated with a type it can't build a value
for. Untyped parameters get a generic spread, which mostly lands on `TypeError`
on both sides and reports nothing.

Probes run your code for real. There is no sandbox yet, so a function that writes
files or hits the network will do that, twice per side. Point it at a repo you
would run the test suite of.

Deltas are not yet grouped — one root cause can produce one line per probe.

```
python3 test_twinrun.py
```
