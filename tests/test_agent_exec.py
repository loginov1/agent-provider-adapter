"""Contract tests for the Codex-exec-compatible Claude adapter (Linux only).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ADAPTER = ROOT / "agent_exec.py"
FAKE = HERE / "fake_claude.py"
MCP_SCRIPT = "/opt/agent-jobs/checkpoint_mcp.py"
# A job with a pinned MCP checkpoint server, and a disabled copy of it; the
# example policy has neither, so the tests add them to their own copy.
MCP_JOB = {
    "enabled": True, "models": ["claude-opus-5-5"], "default_model": "claude-opus-5-5",
    "sandboxes": {"read-only": "read"}, "network": False, "json_events": True,
    "accept_config": ["approvals_reviewer"],
    "mcp_servers": {"unit_save": {"command": "/usr/bin/python3", "script": MCP_SCRIPT,
                                  "tools": ["save_topic", "save_proposal"], "required": True}},
}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False, "required": ["status", "items"],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "items": {"type": "array", "maxItems": 2, "items": {"type": "string", "minLength": 1}},
        "count": {"type": "integer", "minimum": 0},
    },
}


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agent-exec-test-")).resolve()
        os.chmod(self.dir, 0o700)
        fake = self.dir / "claude"
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        fake.chmod(0o755)
        token = self.dir / "token"
        token.write_text("test-oauth-token\n")
        token.chmod(0o600)
        policy = json.loads((ROOT / "policy.json").read_text())
        policy.update(claude_bin=str(fake), oauth_token_file=str(token),
                      claude_config_dir=str(self.dir / "config"),
                      settings_file=str(ROOT / "claude-settings.json"))
        policy["jobs"]["mcpfixture"] = MCP_JOB
        policy["jobs"]["parked"] = dict(MCP_JOB, enabled=False,
                                        reason="stays on Codex until its connector has an equivalent")
        self.policy = self.dir / "policy.json"
        self.policy.write_text(json.dumps(policy))
        self.schema = self.dir / "schema.json"
        self.schema.write_text(json.dumps(SCHEMA))
        self.output = self.dir / "out.json"
        self.record = self.dir / "record.json"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.dir)], check=False)

    def env(self, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.dir), "AGENT_PROVIDER_POLICY": str(self.policy),
               "FAKE_RECORD": str(self.record), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def args(self, *extra, job="backlog", sandbox="danger-full-access", model="claude-opus-5-5"):
        return [sys.executable, str(ADAPTER), "--job", job, "exec", "--ephemeral", "--ignore-user-config",
                "--disable", "apps", "--disable", "plugins", "--sandbox", sandbox, "--model", model,
                "-c", 'model_reasoning_effort="high"', "-c", 'approval_policy="never"',
                "--cd", str(self.dir), "--output-schema", str(self.schema),
                "--output-last-message", str(self.output), *extra, "-"]

    def run_adapter(self, args, prompt="do the work", **env):
        return subprocess.run(args, input=prompt, capture_output=True, text=True,
                              env=self.env(**env), timeout=60)

    def recorded(self):
        return json.loads(self.record.read_text())

    def test_success_writes_validated_object_atomically(self):
        out = {"status": "PASS", "items": ["a"], "count": 2}
        result = self.run_adapter(self.args(), FAKE_OUTPUT=json.dumps(out), ANTHROPIC_API_KEY="x",
                                  SERVICE_AUTOMATION_TOKEN="secret", CLAUDE_CONFIG_DIR="/elsewhere")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text()), out)
        self.assertEqual(json.loads(result.stdout), out)
        self.assertEqual(list(self.dir.glob(".out.json.*")), [])
        call = self.recorded()
        argv = call["argv"]
        self.assertEqual(call["prompt"], "do the work")
        self.assertEqual(call["token"], "test-oauth-token")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("$schema", json.loads(argv[argv.index("--json-schema") + 1]))
        self.assertEqual(argv[argv.index("--tools") + 1].split(","),
                         ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill"])
        env = call["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("SERVICE_AUTOMATION_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.dir / "config"))
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], "5400000")
        self.assertEqual(call["cwd"], str(self.dir))

    def test_read_only_sandbox_gets_read_tools_only(self):
        result = self.run_adapter(self.args(job="review", sandbox="read-only"),
                                  FAKE_OUTPUT='{"status":"PASS","items":[]}')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.recorded()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")

    def assert_usage_error(self, args, text, **env):
        result = self.run_adapter(args, **env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(text, result.stderr)
        self.assertFalse(self.record.exists(), "Claude must not start on a usage error")
        self.assertFalse(self.output.exists())

    def test_unknown_option_is_refused(self):
        self.assert_usage_error(self.args("--full-auto"), "unsupported codex exec option")

    def test_gpt_model_names_are_refused(self):
        self.assert_usage_error(self.args(model="gpt-6-sol"), "not an allowed Claude model")

    def test_sandbox_outside_policy_is_refused(self):
        self.assert_usage_error(self.args(job="review"), "not permitted for review")

    def test_app_connectors_are_refused(self):
        self.assert_usage_error(self.args("--enable", "apps"), "no Claude equivalent")
        self.assert_usage_error(self.args("-c", "apps._default.enabled=false"), "no Claude equivalent")

    def test_unsupported_effort_is_refused(self):
        args = self.args()
        args[args.index('model_reasoning_effort="high"')] = 'model_reasoning_effort="minimal"'
        self.assert_usage_error(args, "no Claude effort level")

    def test_unsupported_schema_keyword_is_refused(self):
        self.schema.write_text(json.dumps({"type": "object", "if": {"type": "object"}}))
        self.assert_usage_error(self.args(), "not supported")

    def test_disabled_job_names_its_reason(self):
        self.assert_usage_error(self.args(job="parked", sandbox="read-only"), "not migrated to Claude")

    def test_unknown_job_is_refused(self):
        self.assert_usage_error(self.args(job="nonexistent"), "not defined in the policy")

    def test_schema_violation_fails_without_output(self):
        self.output.write_text("previous")
        result = self.run_adapter(self.args(), FAKE_OUTPUT='{"status":"MAYBE","items":[]}')
        self.assertEqual(result.returncode, 1)
        self.assertIn("not one of", result.stderr)
        self.assertEqual(self.output.read_text(), "previous")

    def test_extra_property_fails(self):
        result = self.run_adapter(self.args(), FAKE_OUTPUT='{"status":"PASS","items":[],"x":1}')
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected property", result.stderr)

    def test_model_error_result_reports_rate_limit(self):
        result = self.run_adapter(self.args(), FAKE_MODE="error")
        self.assertEqual(result.returncode, 1)
        self.assertIn("rate limit", result.stderr)
        self.assertFalse(self.output.exists())

    def test_crash_without_result_fails(self):
        result = self.run_adapter(self.args(), FAKE_MODE="crash")
        self.assertEqual(result.returncode, 1)
        self.assertIn("without a result event", result.stderr)

    def test_permission_mode_drift_aborts(self):
        result = self.run_adapter(self.args(), FAKE_MODE="wrong-permission")
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected dontAsk", result.stderr)
        self.assertFalse(self.output.exists())

    def test_unexpected_tool_aborts(self):
        result = self.run_adapter(self.args(), FAKE_MODE="extra-tool")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tool set mismatch", result.stderr)

    def test_sigterm_stops_claude_and_its_children(self):
        process = subprocess.Popen(self.args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=self.env(FAKE_MODE="sleep"))
        process.stdin.write("long task")
        process.stdin.close()
        marker = Path(str(self.record) + ".grandchild")
        deadline = time.time() + 20
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.2)
        grandchild = int(marker.read_text())
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=30), 128 + signal.SIGTERM)
        process.stdout.close()
        process.stderr.close()
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)
        self.assertFalse(self.output.exists())

    def test_mcp_checkpoint_server_is_translated(self):
        args = self.args("--json", "-c", 'approvals_reviewer="auto_review"',
                         "-c", "mcp_servers.newsletter.enabled=false",
                         "-c", 'mcp_servers.unit_save.command="/usr/bin/python3"',
                         "-c", "mcp_servers.unit_save.args=" + json.dumps(
                             [MCP_SCRIPT, "--target", "/x/checkpoint.json", "--bundle", "/x/bundle.json"]),
                         job="mcpfixture", sandbox="read-only")
        result = self.run_adapter(args, FAKE_OUTPUT='{"status":"PASS","items":[]}')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.recorded()["argv"]
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("mcp__unit_save__save_topic", allowed)
        self.assertIn("mcp__unit_save__save_proposal", allowed)
        self.assertTrue(result.stdout.strip().splitlines()[0].startswith('{"type": "system"'))

    def test_mcp_command_outside_policy_is_refused(self):
        args = self.args("-c", 'mcp_servers.unit_save.command="/bin/sh"',
                         "-c", 'mcp_servers.unit_save.args=["-c","id"]',
                         job="mcpfixture", sandbox="read-only")
        self.assert_usage_error(args, "does not match the policy")

    def test_version_satisfies_existing_runner_checks(self):
        # Runners read the second field of `--version` and require Codex 0.156.1 or newer.
        result = subprocess.run([sys.executable, str(ADAPTER), "--job", "review", "--version"],
                                capture_output=True, text=True, env=self.env())
        self.assertEqual(result.returncode, 0)
        version = result.stdout.split()[1]
        ordered = sorted(["0.156.1", version], key=lambda v: [int(p) for p in v.split(".")])
        self.assertEqual(ordered[0], "0.156.1")
        self.assertIn("job=review", result.stdout)

    def test_new_empty_placeholders_are_removed_but_existing_files_kept(self):
        subprocess.run(["git", "init", "-q", str(self.dir)], check=True)
        (self.dir / "yarn.lock").write_text("")  # existed before the session: kept
        result = self.run_adapter(self.args(), FAKE_MODE="placeholders",
                                  FAKE_OUTPUT='{"status":"PASS","items":[]}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / ".npmrc").exists())
        self.assertFalse((self.dir / ".env.test").exists())
        self.assertTrue((self.dir / "yarn.lock").exists())
        self.assertTrue((self.dir / "notes.txt").exists())
        self.assertFalse((self.dir / "node_modules").exists())
        self.assertIn("appeared during the session", result.stderr)

    def test_group_readable_token_is_refused(self):
        (self.dir / "token").chmod(0o640)
        self.assert_usage_error(self.args(), "must not be readable")


if __name__ == "__main__":
    unittest.main()
