"""Ask a model for inputs when the corpus cannot build one.

Every value twinrun probes with is synthesised from an annotation, a guard
literal, or a construction lifted out of the test suite. On click that leaves
648 callables unprobed: 352 where nothing could be built at all, and 296 where
something was built and no probe ever ran a line the commit moved. Both are the
same failure -- a type the corpus does not model, or a value too specific to
guess -- and both are what a model is good at.

What it is allowed to do is bounded on purpose. It proposes expressions. It
never sees the two revisions, never compares them, and never says whether a
difference matters: the old revision is still the only oracle. A bad suggestion
costs a probe that raises identically on both sides, which the run already
tolerates by the thousand. A good one costs nothing and buys a callable.

Off unless `--llm` is passed and ANTHROPIC_API_KEY is set.
"""
import ast
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("TWINRUN_MODEL", "claude-sonnet-5")
# Calls per run. A sweep is hundreds of commits and the ceiling is what keeps a
# measurement affordable; a single commit never comes near it.
BUDGET = 40
PER_PARAM = 6           # expressions kept per parameter, so one column stays small
TIMEOUT = 30

_spent = [0]


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _cache_dir() -> Path:
    d = Path(os.environ.get("TWINRUN_CACHE")
             or Path.home() / ".cache" / "twinrun")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _post(prompt: str) -> str | None:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None     # no inputs is what the caller already had; it stays there
    return "".join(b.get("text", "") for b in data.get("content", []))


def _source(root: Path, file: str, qualname: str, lines: set) -> str | None:
    """The callable as it reads in the base revision, with moved lines marked."""
    try:
        text = (root / file).read_text()
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None
    want = qualname.split(".")
    node, body = None, tree.body
    for i, name in enumerate(want):
        node = next((n for n in body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)) and n.name == name), None)
        if node is None:
            return None
        body = node.body
    src = ast.get_source_segment(text, node)
    if src is None:
        return None
    out = []
    for off, line in enumerate(src.splitlines()):
        # The commit is the whole reason this callable is being probed, so the
        # model is told which lines it has to reach rather than left to guess.
        out.append(("  >>" if node.lineno + off in lines else "    ") + line)
    return "\n".join(out)


PROMPT = """\
This function is under differential test: it is run at two revisions on the \
same inputs and the outputs are compared. I need inputs that reach the lines \
marked `>>`, which are the lines the commit changed.

File: {file}
Callable: {qualname}

```python
{source}
```

Give me Python expressions for these parameters:
{params}

Rules:
- One JSON object, nothing else. Keys are the parameter names above, values are \
lists of up to {per} expression strings.
- Each expression is evaluated on its own in the module `{file}`, so it may use \
names that module defines or imports, and may not use the other parameters.
- Prefer values that reach the marked lines, and include values that take \
different branches through them.
- No expression may read the network, the clock, or randomness: the same \
expression is evaluated several times and anything that answers differently \
each time is discarded.
- If a parameter takes an object this module defines, construct it.
"""


def propose(root: Path, ch, params: list) -> dict:
    """Expressions per parameter name, or {} if there are none to be had.

    Cached on disk by the prompt, because a sweep re-runs the same commits and
    an A/B that pays twice for the same answer is an A/B nobody runs.
    """
    if _spent[0] >= BUDGET or not params:
        return {}
    lines = set(ch.lines.get("base", ()) or ())
    src = _source(root, ch.file, ch.qualname, lines)
    if not src:
        return {}
    prompt = PROMPT.format(
        file=ch.file, qualname=ch.qualname, source=src, per=PER_PARAM,
        params="\n".join(f"- {n}: {a or 'no annotation'}" for n, a in params))

    key = _cache_dir() / (hashlib.sha256(
        (MODEL + prompt).encode()).hexdigest()[:32] + ".json")
    try:
        return json.loads(key.read_text())
    except (OSError, ValueError):
        pass

    _spent[0] += 1
    text = _post(prompt)
    out = {} if text is None else _parse(text, [n for n, _ in params])
    if text is not None:            # a transport failure is not an answer to keep
        try:
            key.write_text(json.dumps(out))
        except OSError:
            pass
    return out


def _parse(text: str, names: list) -> dict:
    """Keep what is a parameter name mapped to expressions that actually parse."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name in names:
        vals = raw.get(name)
        if not isinstance(vals, list):
            continue
        good = []
        for v in vals:
            if not isinstance(v, str) or len(v) > 200:
                continue
            # An expression, not a statement. This is the difference between a
            # probe and prose, and it is also why nothing here can `import`.
            try:
                ast.parse(v, mode="eval")
            except (SyntaxError, ValueError):
                continue
            if v not in good:
                good.append(v)
        if good:
            out[name] = good[:PER_PARAM]
    return out
