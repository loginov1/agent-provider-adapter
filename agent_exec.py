#!/usr/bin/env python3
"""Run one bounded Claude Code session behind the `codex exec` interface.

Scheduled runners start `$CODEX_BIN exec ... -`, pipe the prompt on stdin
and read the JSON object that `--output-last-message` writes. This
adapter accepts exactly the `codex exec` options those runners use, maps each
one to an explicit Claude Code equivalent from the root-owned job policy, and
exits 2 on anything it cannot honour. Claude's `structured_output` is checked
against the runner's original schema before it is written atomically.

Exit codes: 0 success; 1 model or runtime failure (Codex's generic failure);
2 usage, policy or schema error; 128+N after forwarding signal N to Claude.
The policy path can only be overridden for tests (AGENT_PROVIDER_POLICY).
"""
import ctypes
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import tempfile

ADAPTER_VERSION = "1.0.0"
DEFAULT_POLICY = "/etc/agent-provider/policy.json"
EFFORTS = {"low", "medium", "high", "xhigh"}
CLAUDE_PREFIX = "claude-"
GRACE_SECONDS = 10
# Codex removes these names from model shells by default; keep that boundary.
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN", re.I)
# Never let an inherited variable switch Claude to API billing or another
# provider, or point it at a different configuration directory.
PROVIDER_ENV = re.compile(r"^(ANTHROPIC_|CLAUDE_CODE_USE_|CLAUDE_CONFIG_DIR$|CLAUDECODE$|"
                          r"CLAUDE_CODE_ENTRYPOINT$|CLAUDE_CODE_OAUTH_TOKEN|AWS_BEARER_TOKEN_BEDROCK)")
# Empty files with these names, plus an empty node_modules/.bin, appeared once
# in a session's working directory (2026-10-11, apparently Bash-sandbox mount
# points; not reproducible since). Untracked, empty, newly created copies
# would make a nightly checkout dirty and block its next run, so they are
# removed after every session and the removal is logged.
PLACEHOLDERS = (".env.development", ".env.production", ".env.test", ".gitmodules", ".npmrc",
                ".yarnrc", ".yarnrc.yml", "bunfig.toml", "pnpm-lock.yaml", "yarn.lock")
ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description", "default", "examples",
               "format", "deprecated", "readOnly", "writeOnly"}


class UsageError(Exception):
    """Unsupported option, disallowed setting or invalid policy: exit 2."""


class RunError(Exception):
    """Claude ran but produced no valid result: exit 1."""


# --------------------------------------------------------------------------
# JSON Schema subset. Unknown validation keywords are refused up front so an
# unsupported constraint can never be silently skipped.
# --------------------------------------------------------------------------
KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
            "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "minimum", "maximum",
            "exclusiveMinimum", "exclusiveMaximum", "pattern", "anyOf", "oneOf", "allOf", "$ref",
            "$defs", "definitions"} | ANNOTATIONS
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def check_schema(node, where="#"):
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise UsageError(f"schema node {where} is not an object")
    unknown = set(node) - KEYWORDS
    if unknown:
        raise UsageError(f"schema keyword(s) {sorted(unknown)} at {where} are not supported")
    for key in ("properties", "$defs", "definitions"):
        for name, child in node.get(key, {}).items():
            check_schema(child, f"{where}/{key}/{name}")
    if isinstance(node.get("items"), list):
        raise UsageError(f"tuple-form items at {where} are not supported")
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            check_schema(node[key], f"{where}/{key}")
    for key in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(node.get(key, [])):
            check_schema(child, f"{where}/{key}/{index}")
    if "$ref" in node and not str(node["$ref"]).startswith("#/"):
        raise UsageError(f"only local $ref values are supported ({where})")


def is_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name not in TYPES:
        raise UsageError(f"unsupported schema type {name!r}")
    return isinstance(value, TYPES[name])


def resolve_ref(root, ref):
    node = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def validate(value, node, root, where="$"):
    if node is True:
        return
    if node is False:
        raise RunError(f"{where}: no value is allowed here")
    if "$ref" in node:
        validate(value, resolve_ref(root, node["$ref"]), root, where)
    if "type" in node:
        names = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(is_type(value, name) for name in names):
            raise RunError(f"{where}: expected {'/'.join(names)}")
    if "enum" in node and value not in node["enum"]:
        raise RunError(f"{where}: value is not one of {node['enum']}")
    if "const" in node and value != node["const"]:
        raise RunError(f"{where}: value differs from the required constant")
    if isinstance(value, str):
        if len(value) < node.get("minLength", 0):
            raise RunError(f"{where}: shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            raise RunError(f"{where}: longer than {node['maxLength']}")
        if "pattern" in node and not re.search(node["pattern"], value):
            raise RunError(f"{where}: does not match {node['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            raise RunError(f"{where}: below {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            raise RunError(f"{where}: above {node['maximum']}")
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            raise RunError(f"{where}: not above {node['exclusiveMinimum']}")
        if "exclusiveMaximum" in node and value >= node["exclusiveMaximum"]:
            raise RunError(f"{where}: not below {node['exclusiveMaximum']}")
    if isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            raise RunError(f"{where}: fewer than {node['minItems']} items")
        if "maxItems" in node and len(value) > node["maxItems"]:
            raise RunError(f"{where}: more than {node['maxItems']} items")
        if node.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise RunError(f"{where}: items are not unique")
        if isinstance(node.get("items"), (dict, bool)):
            for index, item in enumerate(value):
                validate(item, node["items"], root, f"{where}[{index}]")
    if isinstance(value, dict):
        for name in node.get("required", []):
            if name not in value:
                raise RunError(f"{where}: missing required property {name!r}")
        properties = node.get("properties", {})
        extra = node.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                validate(item, properties[name], root, f"{where}.{name}")
            elif extra is False:
                raise RunError(f"{where}: unexpected property {name!r}")
            elif isinstance(extra, dict):
                validate(item, extra, root, f"{where}.{name}")
    for key in ("allOf",):
        for child in node.get(key, []):
            validate(value, child, root, where)
    for key in ("anyOf", "oneOf"):
        if key in node:
            passed = 0
            for child in node[key]:
                try:
                    validate(value, child, root, where)
                    passed += 1
                except RunError:
                    pass
            if passed == 0 or (key == "oneOf" and passed != 1):
                raise RunError(f"{where}: does not satisfy {key}")


def claude_schema(schema):
    """Claude's validator rejects draft-2020-12 meta-schema references."""
    stripped = {key: value for key, value in schema.items() if key not in {"$schema", "$id"}}
    return json.dumps(stripped, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# Codex argument translation
# --------------------------------------------------------------------------
VALUE_OPTIONS = {"--model": "model", "-m": "model", "--sandbox": "sandbox", "-s": "sandbox",
                 "--cd": "cd", "-C": "cd", "--output-schema": "schema",
                 "--output-last-message": "output", "-o": "output"}
REPEATED = {"-c": "config", "--config": "config", "--add-dir": "add_dir",
            "--disable": "disable", "--enable": "enable"}
FLAGS = {"--json": "json", "--ephemeral": "ephemeral", "--ignore-user-config": "ignore_user_config",
         "--skip-git-repo-check": "skip_git_repo_check"}


def parse_codex(argv):
    if not argv or argv[0] != "exec":
        raise UsageError("only `exec` is supported (Codex app-server and other subcommands stay on Codex)")
    opts = {"config": [], "add_dir": [], "disable": [], "enable": [], "prompt": None}
    rest = argv[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        name, value = (token.split("=", 1) + [None])[:2] if token.startswith("--") and "=" in token else (token, None)
        if name in VALUE_OPTIONS or name in REPEATED:
            if value is None:
                index += 1
                if index >= len(rest):
                    raise UsageError(f"{name} needs a value")
                value = rest[index]
            if name in VALUE_OPTIONS:
                key = VALUE_OPTIONS[name]
                if opts.get(key) is not None:
                    raise UsageError(f"{name} was given twice")
                opts[key] = value
            else:
                opts[REPEATED[name]].append(value)
        elif name in FLAGS and value is None:
            opts[FLAGS[name]] = True
        elif token == "-" or not token.startswith("-"):
            if opts["prompt"] is not None:
                raise UsageError("more than one prompt argument")
            opts["prompt"] = token
        else:
            raise UsageError(f"unsupported codex exec option for the Claude adapter: {token}")
        index += 1
    return opts


def toml_value(raw):
    """The runners pass JSON-compatible TOML scalars and arrays."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()


def translate(opts, job_name, policy):
    jobs = policy.get("jobs", {})
    if job_name not in jobs:
        raise UsageError(f"job {job_name!r} is not defined in the policy")
    job = jobs[job_name]
    if not job.get("enabled", False):
        raise UsageError(f"job {job_name!r} is not migrated to Claude: {job.get('reason', 'disabled in policy')}")
    plan = {"model": opts.get("model") or job["default_model"], "effort": None, "mcp": {},
            "network": None, "sandbox": opts.get("sandbox") or job.get("default_sandbox", "read-only")}
    if plan["model"] not in job["models"] or not plan["model"].startswith(CLAUDE_PREFIX):
        raise UsageError(f"model {plan['model']!r} is not an allowed Claude model for {job_name} "
                         f"(allowed: {', '.join(job['models'])})")
    if plan["sandbox"] not in job["sandboxes"]:
        raise UsageError(f"sandbox {plan['sandbox']!r} is not permitted for {job_name}")
    profile = policy["profiles"][job["sandboxes"][plan["sandbox"]]]
    plan["tools"] = list(profile["tools"]) + list(job.get("extra_tools", []))
    plan["allowed"] = list(plan["tools"])
    for feature in opts["disable"]:
        if feature not in {"apps", "plugins"}:
            raise UsageError(f"--disable {feature} has no Claude equivalent")
    if opts["enable"]:
        raise UsageError(f"--enable {', '.join(opts['enable'])}: Codex app connectors have no Claude equivalent")
    if opts.get("json") and not job.get("json_events", False):
        raise UsageError("--json event output is not permitted for this job (Claude events differ from Codex)")
    mcp_commands = {}
    for item in opts["config"]:
        if "=" not in item:
            raise UsageError(f"-c {item!r} is not KEY=VALUE")
        key, raw = item.split("=", 1)
        key = key.strip()
        value = toml_value(raw)
        if key == "model_reasoning_effort":
            if value not in EFFORTS:
                raise UsageError(f"reasoning effort {value!r} has no Claude effort level")
            plan["effort"] = value
        elif key == "approval_policy":
            if value != "never":
                raise UsageError("only approval_policy=\"never\" is supported (no human prompts)")
        elif key == "sandbox_workspace_write.network_access":
            if value is not True or plan["sandbox"] != "workspace-write" or not job.get("network", False):
                raise UsageError("network access setting is not permitted for this job")
            plan["network"] = True
        elif key == "approvals_reviewer":
            if "approvals_reviewer" not in job.get("accept_config", []):
                raise UsageError("approvals_reviewer is not accepted for this job")
        elif key.startswith("mcp_servers."):
            parts = key.split(".")
            if len(parts) != 3:
                raise UsageError(f"unsupported MCP setting {key}")
            _, server, field = parts
            if field == "enabled" and value is False:
                continue  # --strict-mcp-config already excludes every other server
            if field not in {"command", "args"}:
                raise UsageError(f"unsupported MCP setting {key}")
            mcp_commands.setdefault(server, {})[field] = value
        else:
            raise UsageError(f"-c {key} has no Claude equivalent in this adapter")
    allowed_mcp = job.get("mcp_servers", {})
    for server, spec in mcp_commands.items():
        rule = allowed_mcp.get(server)
        if rule is None:
            raise UsageError(f"MCP server {server!r} is not allowed for {job_name}")
        args = spec.get("args", [])
        if spec.get("command") != rule["command"] or not isinstance(args, list) or not args \
                or args[0] != rule["script"] or not all(isinstance(a, str) for a in args):
            raise UsageError(f"MCP server {server!r} command does not match the policy")
        plan["mcp"][server] = {"type": "stdio", "command": spec["command"], "args": args}
        plan["allowed"] += [f"mcp__{server}__{tool}" for tool in rule["tools"]]
    for server in allowed_mcp:
        if allowed_mcp[server].get("required") and server not in plan["mcp"]:
            raise UsageError(f"job {job_name} requires MCP server {server}")
    return plan


# --------------------------------------------------------------------------
# Process handling
# --------------------------------------------------------------------------
def set_parent_death_signal():
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def descendants(root):
    children = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    found, frontier = set(), [root]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


class Child:
    def __init__(self):
        self.process = None
        self.signal = None

    def kill(self, sig):
        if not self.process or self.process.poll() is not None:
            return
        for pid in descendants(self.process.pid):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    def on_signal(self, signum, _frame):
        if self.signal is None:
            self.signal = signum
            self.kill(signal.SIGTERM)
            signal.alarm(GRACE_SECONDS)
        else:
            self.kill(signal.SIGKILL)

    def on_alarm(self, _signum, _frame):
        self.kill(signal.SIGKILL)


def child_env(policy, token_fd):
    # The OAuth token travels through an inherited pipe, never the environment.
    # CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is deliberately unset: in this version it
    # forces the permission mode back to "default", defeating dontAsk.
    env = {}
    for key, value in os.environ.items():
        if SECRET_NAME.search(key) or PROVIDER_ENV.search(key):
            continue
        env[key] = value
    env.update({
        "CLAUDE_CONFIG_DIR": policy["claude_config_dir"],
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": str(token_fd),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    })
    # Fixed, non-secret tuning from the root-owned policy (e.g. Bash timeouts
    # long enough for commands that wait in a host-wide job queue).
    env.update({key: str(value) for key, value in policy.get("env", {}).items()})
    env.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    return env


def read_token(path):
    info = os.stat(path)
    if info.st_mode & 0o077:
        raise UsageError(f"{path} must not be readable by group or others")
    token = Path(path).read_text().strip()
    if not token:
        raise UsageError(f"{path} is empty")
    return token


def short(value, limit=240):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def log(message):
    print(f"[agent-exec] {message}", file=sys.stderr, flush=True)


def describe_event(event, expected_tools, plan):
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        tools = set(event.get("tools", []))
        unexpected = tools - set(expected_tools) - {"StructuredOutput"} - {
            t for t in tools if any(t.startswith(f"mcp__{s}__") for s in plan["mcp"])}
        missing = set(expected_tools) - tools
        servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers", [])}
        log(f"session model={event.get('model')} permission={event.get('permissionMode')} "
            f"tools={sorted(tools)} mcp={servers} skills={len(event.get('skills', []))}")
        if event.get("model") != plan["model"]:
            raise RunError(f"Claude started {event.get('model')} instead of {plan['model']}")
        if event.get("permissionMode") != "dontAsk":
            raise RunError(f"permission mode is {event.get('permissionMode')}, expected dontAsk")
        if unexpected or missing:
            raise RunError(f"tool set mismatch: unexpected={sorted(unexpected)} missing={sorted(missing)}")
        for server in plan["mcp"]:
            if servers.get(server) != "connected":
                raise RunError(f"required MCP server {server} is {servers.get(server)}")
    elif kind == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                log("assistant: " + short(block["text"]))
            elif block.get("type") == "tool_use":
                log(f"tool {block.get('name')}: {short(block.get('input', {}), 200)}")
    elif kind == "user":
        content = event.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                log("tool error: " + short(block.get("content", "")))
    elif kind == "rate_limit_event":
        log("rate limit: " + short(event.get("rate_limit_info", event), 200))


def existing_placeholders(workdir):
    names = PLACEHOLDERS + ("node_modules",)
    return {name for name in names if os.path.lexists(os.path.join(workdir, name))}


def remove_new_placeholders(workdir, before):
    for name in PLACEHOLDERS:
        path = Path(workdir) / name
        if name in before or path.is_symlink() or not path.is_file() or path.stat().st_size:
            continue
        tracked = subprocess.run(["git", "-C", str(workdir), "ls-files", "--error-unmatch", name],
                                 capture_output=True).returncode == 0
        if not tracked:
            path.unlink()
            log(f"removed empty untracked {path} that appeared during the session")
    modules = Path(workdir) / "node_modules"
    if "node_modules" not in before and modules.is_dir() and not modules.is_symlink():
        entries = list(modules.iterdir())
        stub = modules / ".bin"
        if entries == [stub] and stub.is_dir() and not stub.is_symlink() and not any(stub.iterdir()):
            stub.rmdir()
            modules.rmdir()
            log(f"removed empty {stub} stub that appeared during the session")


def write_atomic(path, data):
    target = Path(path)
    directory = target.parent
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(temporary, 0o666 & ~mask)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def build_command(policy, plan, opts, schema_json, mcp_path):
    command = [policy["claude_bin"], "-p", "--output-format", "stream-json", "--verbose",
               "--model", plan["model"], "--permission-mode", "dontAsk", "--permission-prompts", "none",
               "--tools", ",".join(plan["tools"]), "--allowedTools", ",".join(plan["allowed"]),
               "--setting-sources", policy["setting_sources"], "--settings", policy["settings_file"],
               "--strict-mcp-config", "--json-schema", schema_json]
    if plan["effort"]:
        command += ["--effort", plan["effort"]]
    if opts.get("ephemeral"):
        command.append("--no-session-persistence")
    if mcp_path:
        command += ["--mcp-config", mcp_path]
    for directory in opts["add_dir"]:
        command += ["--add-dir", directory]
    return command


def version_line(policy, job):
    try:
        cli = subprocess.run([policy["claude_bin"], "--version"], capture_output=True, text=True,
                             timeout=30).stdout.split()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        cli = "unavailable"
    model = policy.get("jobs", {}).get(job, {}).get("default_model", "-")
    return f"agent-exec {ADAPTER_VERSION} (claude-code {cli}; job={job}; model={model})"


def run(argv):
    if len(argv) < 2 or argv[0] != "--job":
        raise UsageError("usage: agent-exec --job JOB exec [codex exec options] -")
    job_name, argv = argv[1], argv[2:]
    policy = json.loads(Path(os.environ.get("AGENT_PROVIDER_POLICY", DEFAULT_POLICY)).read_text())
    if argv in (["--version"], ["-V"]):
        print(version_line(policy, job_name))
        return 0
    opts = parse_codex(argv)
    plan = translate(opts, job_name, policy)
    if not opts.get("schema") or not opts.get("output"):
        raise UsageError("--output-schema and --output-last-message are required for unattended jobs")
    schema = json.loads(Path(opts["schema"]).read_text())
    check_schema(schema)
    workdir = opts.get("cd") or os.getcwd()
    if not Path(workdir).is_dir():
        raise UsageError(f"working directory {workdir} does not exist")
    if opts["prompt"] in (None, "-"):
        prompt = sys.stdin.read()
    else:
        prompt = opts["prompt"]
    if not prompt.strip():
        raise UsageError("empty prompt")
    token = read_token(policy["oauth_token_file"])
    placeholders_before = existing_placeholders(workdir)
    child = Child()
    with tempfile.TemporaryDirectory(prefix="agent-exec-") as scratch:
        mcp_path = None
        if plan["mcp"]:
            mcp_path = os.path.join(scratch, "mcp.json")
            Path(mcp_path).write_text(json.dumps({"mcpServers": plan["mcp"]}))
        command = build_command(policy, plan, opts, claude_schema(schema), mcp_path)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, token.encode())
        os.close(write_fd)
        signal.signal(signal.SIGALRM, child.on_alarm)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, child.on_signal)
        log(f"job={job_name} model={plan['model']} effort={plan['effort'] or 'default'} "
            f"sandbox={plan['sandbox']} tools={','.join(plan['tools'])} cwd={workdir}")
        child.process = subprocess.Popen(
            command, cwd=workdir, env=child_env(policy, read_fd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8",
            pass_fds=(read_fd,), start_new_session=True, preexec_fn=set_parent_death_signal)
        os.close(read_fd)
        try:
            child.process.stdin.write(prompt)
            child.process.stdin.close()
        except BrokenPipeError:
            pass
        result, failure = None, None
        for line in child.process.stdout:
            if opts.get("json"):
                sys.stdout.write(line)
                sys.stdout.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log("non-JSON output: " + short(line))
                continue
            try:
                describe_event(event, plan["tools"], plan)
            except RunError as error:
                failure = error
                child.kill(signal.SIGTERM)
                signal.alarm(GRACE_SECONDS)
            if event.get("type") == "result":
                result = event
        code = child.process.wait()
        signal.alarm(0)
    remove_new_placeholders(workdir, placeholders_before)
    if child.signal is not None:
        log(f"terminated by signal {child.signal}; Claude was stopped")
        return 128 + child.signal
    if failure:
        raise failure
    if result is None:
        raise RunError(f"Claude exited {code} without a result event")
    for denial in result.get("permission_denials") or []:
        log("permission denied: " + short(denial, 200))
    if result.get("is_error") or result.get("subtype") != "success":
        detail = short(result.get("result") or result.get("errors") or result.get("subtype"), 400)
        status = result.get("api_error_status")
        hint = " (rate limit / usage limit)" if status == 429 or re.search(
            r"rate.?limit|usage limit|quota", detail, re.I) else ""
        raise RunError(f"Claude result {result.get('subtype')} status={status}{hint}: {detail}")
    if "structured_output" not in result:
        raise RunError("Claude returned no structured_output")
    output = result["structured_output"]
    validate(output, schema, schema)
    text = json.dumps(output, ensure_ascii=False)
    write_atomic(opts["output"], text)
    log(f"done: turns={result.get('num_turns')} duration_ms={result.get('duration_ms')} "
        f"cost_equivalent_usd={result.get('total_cost_usd')} output={opts['output']}")
    if not opts.get("json"):
        print(text, flush=True)
    if code != 0:
        log(f"Claude exited {code} after a valid result; treating the validated result as final")
    return 0


def main():
    try:
        return run(sys.argv[1:])
    except UsageError as error:
        log(f"usage error: {error}")
        return 2
    except RunError as error:
        log(f"run failed: {error}")
        return 1
    except (OSError, json.JSONDecodeError) as error:
        log(f"run failed: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
