#!/usr/bin/env python3
"""Test double for `claude -p --output-format stream-json`. Behaviour comes
from FAKE_MODE; every invocation records argv, env and stdin for assertions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

record = Path(os.environ["FAKE_RECORD"])
argv = sys.argv[1:]
prompt = sys.stdin.read()
token_fd = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
token = os.read(int(token_fd), 4096).decode() if token_fd else None
record.write_text(json.dumps({"argv": argv, "env": dict(os.environ), "prompt": prompt,
                              "token": token, "cwd": os.getcwd()}))
mode = os.environ.get("FAKE_MODE", "success")


def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def emit(event):
    print(json.dumps(event), flush=True)


model = value("--model")
tools = [t for t in (value("--tools") or "").split(",") if t]
mcp = []
if value("--mcp-config"):
    servers = json.loads(Path(value("--mcp-config")).read_text())["mcpServers"]
    mcp = [{"name": name, "status": "connected"} for name in servers]
    tools += [rule for rule in value("--allowedTools").split(",") if rule.startswith("mcp__")]
init = {"type": "system", "subtype": "init", "model": model, "permissionMode": "dontAsk",
        "tools": tools + ["StructuredOutput"], "mcp_servers": mcp, "skills": ["example-skill"]}
if mode == "wrong-permission":
    init["permissionMode"] = "default"
if mode == "extra-tool":
    init["tools"].append("Agent")
emit(init)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
if mode == "placeholders":
    for name in (".npmrc", "yarn.lock", ".env.test"):
        Path(name).touch()
    Path("notes.txt").write_text("")
    Path("node_modules/.bin").mkdir(parents=True)
if mode == "sleep":
    grandchild = subprocess.Popen(["sleep", "300"])
    Path(os.environ["FAKE_RECORD"] + ".grandchild").write_text(str(grandchild.pid))
    time.sleep(300)
elif mode == "crash":
    sys.exit(3)
elif mode == "error":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "api_error_status": 429, "result": "API Error: rate limit reached"})
else:
    output = json.loads(os.environ.get("FAKE_OUTPUT", '{"status": "PASS"}'))
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
          "result": json.dumps(output), "structured_output": output,
          "permission_denials": []})
