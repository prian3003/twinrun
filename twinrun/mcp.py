"""twinrun as an MCP tool, so the thing that wrote the code can ask what it broke.

An agent that just edited a file has the same problem a reviewer has and less to
go on: it knows what it meant to change and nothing about what else moved. The
Action covers the pull request, which is late -- by then the change is written,
pushed and waiting on somebody. This covers the edit.

Speaks JSON-RPC over stdin and stdout directly rather than through the `mcp`
package, because twinrun imports the code it is measuring into a subprocess and
every dependency it carries is one that can collide with the repository under
test. The protocol here is a read loop and three methods.
"""
import io
import json
import sys
from contextlib import redirect_stdout

from .cli import render_call, show
from .core import cluster, verify

VERSION = "2025-06-18"      # echoed back to the client when it asks for another

TOOL = {
    "name": "verify",
    "description":
        "Run two revisions of every callable a commit touched on identical "
        "inputs and report where their behaviour differs. The old revision is "
        "the oracle, so this needs no tests, types or specification: a "
        "difference is reported because the code used to do something else. "
        "Use it after editing to find the changes that were not intended.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "repo": {"type": "string",
                     "description": "path to the git repository to check"},
            "base": {"type": "string", "default": "HEAD~1",
                     "description": "revision to treat as the oracle"},
            "head": {"type": "string", "default": "HEAD",
                     "description": "revision under test; use HEAD after committing"},
            "limit": {"type": "integer", "default": 24,
                      "description": "max probes per callable"},
            "timeout": {"type": "number", "default": 20.0,
                        "description": "seconds per side per callable"},
        },
        "required": ["repo"],
    },
}


def run(args: dict) -> dict:
    """The verify tool. Returns MCP content plus the same answer as data."""
    rep = verify(args["repo"], args.get("base", "HEAD~1"), args.get("head", "HEAD"),
                 limit=int(args.get("limit", 24)),
                 timeout=float(args.get("timeout", 20.0)))
    # show() is the report a person reads, and an agent should read the same one
    # rather than a second rendering of it that can drift. Nothing may reach the
    # real stdout: it is carrying the protocol.
    buf = io.StringIO()
    with redirect_stdout(buf):
        show(rep, args.get("base", "HEAD~1"), args.get("head", "HEAD"))

    groups = cluster(rep.deltas)
    return {
        "content": [{"type": "text", "text": buf.getvalue()}],
        "structuredContent": {
            "findings": [{
                "file": g[0].file, "qualname": g[0].qualname,
                "call": render_call(g[0]), "calls": len(g),
                "base": g[0].base["value"], "head": g[0].head["value"],
                # A store finding is not "the commit moved this". It is "an
                # answer somebody signed off on is no longer the answer".
                "source": g[0].source,
            } for g in groups],
            "checked": rep.checked, "probes": rep.probes, "reached": rep.reached,
            "flaky": rep.flaky, "rechecked": rep.rechecked,
        },
    }


def handle(req: dict):
    """One request in, one response out, or None for a notification."""
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        return None             # a notification is not answered, even in error

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        return ok({
            "protocolVersion": req.get("params", {}).get("protocolVersion", VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "twinrun", "version": "0.1.0"},
        })
    if method == "tools/list":
        # Only verify. --accept is deliberately not here: a verdict is a person
        # saying a change was meant, and an agent blessing its own findings would
        # write the regression down as the answer.
        return ok({"tools": [TOOL]})
    if method == "tools/call":
        params = req.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602,
                              "message": f"no tool named {params.get('name')!r}"}}
        try:
            return ok(run(params.get("arguments") or {}))
        except Exception as e:
            # A bad revision or an unimportable repository is the caller's
            # problem to fix, so it comes back as a tool error it can read and
            # retry, not as a protocol error that kills the session.
            return ok({"content": [{"type": "text", "text": f"twinrun: {e}"}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue            # not framing we can answer, and no id to answer to
        resp = handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
