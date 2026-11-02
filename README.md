# agent-provider-adapter

Run scheduled coding-agent jobs that were written for `codex exec` on Claude Code instead, without changing the jobs, under an explicit per-job policy. Built for unattended systemd jobs on one Linux host, switchable per job and reversible at any time.

This is an extraction from a private production setup where several scheduled agent jobs (backlog implementation, SEO audits, data curation, translation review) moved from Codex to Claude Code one job at a time, with instant rollback. The code paths are the ones that ran there; only names, paths and examples were generalised. The job names, units and paths in this repository are examples.

## The problem

The scheduled runners were written against `codex exec`: they pass Codex flags, pipe the prompt on stdin and read the JSON object written by `--output-last-message`. Replacing the binary name is not enough. Claude Code uses different flags, rejects draft-2020-12 `$schema` references in `--json-schema`, returns the object as `structured_output` inside a result event, and has its own permission model.

Unattended runs also need guarantees that an interactive session does not: no human prompts, exactly the expected tools and model, no inherited credentials, no user configuration leaking in, clean shutdown when systemd stops the job, and an output file that is either valid or absent.

## What it does

`agent_exec.py` accepts exactly the `codex exec` subset the runners use and maps each option to an explicit Claude Code equivalent taken from a root-owned job policy. Anything it does not understand exits 2 with the reason, before Claude starts.

Around the adapter:

- `agent_jobs.py` (installed as `agent-jobs`): an operator CLI that switches one job between Codex and Claude Code by adding or removing a single systemd drop-in at an idle boundary, and shows status, logs, pause and resume.
- `probe_job.sh`: runs the adapter inside a transient copy of a job's real systemd unit (same user, sandbox, slice and environment) before a switch.
- `install.py`: a versioned, root-owned install with an atomic `current` symlink and a manifest-based rollback.
- `tests/fake_claude.py` and the test suite: contract tests against a fake `claude` binary, with no network and no real CLI.

Python standard library only, about 1,800 lines including tests and configuration.

```
systemd timer
  |
  job unit: User=, ProtectSystem=strict, ReadWritePaths=...   (the outer sandbox)
  |
  runner script, unchanged:   "$CODEX_BIN" exec ... -   (prompt on stdin)
  |
  +-- no drop-in:  CODEX_BIN points at codex           -> Codex CLI, as before
  |
  +-- drop-in 70-agent-provider.conf:
        CODEX_BIN=/usr/local/libexec/agent-provider/jobs/<job>/agent-exec
          -> agent_exec.py --job <job>
               policy lookup, option mapping, refusal of anything unknown (exit 2)
               -> claude -p --permission-mode dontAsk --tools ... --allowedTools ...
               <- stream-json: init event checked for drift, result validated
               -> atomic write of the --output-last-message file
```

## Codex to Claude Code mapping

| Codex option | Claude Code equivalent |
| --- | --- |
| `exec ... -` (stdin prompt) | `claude -p` with the prompt on stdin (never argv: `--tools` is variadic) |
| `--model M`, `-m M` | `--model M`; only Claude IDs listed in the job policy, GPT names are refused |
| `-c model_reasoning_effort="low/medium/high/xhigh"` | `--effort` with the same level; `minimal` is refused |
| `--sandbox read-only` | the `read` profile: tools `Read,Glob,Grep` only |
| `--sandbox workspace-write/danger-full-access` | the job profile's tools; the outer systemd sandbox stays authoritative, as it was for Codex |
| `-c approval_policy="never"` | `--permission-mode dontAsk --permission-prompts none` plus an explicit `--allowedTools` list; any other approval policy is refused |
| `-c sandbox_workspace_write.network_access=true` | accepted only for `workspace-write` on a job whose policy sets `"network": true`; no Claude flag, network reach is the unit's decision |
| `--ephemeral` | `--no-session-persistence` |
| `--ignore-user-config`, `--disable apps`, `--disable plugins` | always applied: `--setting-sources project`, `--strict-mcp-config`, a dedicated `CLAUDE_CONFIG_DIR`, claude.ai connectors off |
| `--skip-git-repo-check` | accepted; Claude Code has no equivalent check |
| `--output-schema F` | `--json-schema` (without `$schema`/`$id`); the result is re-validated against the original `F` |
| `-o`, `--output-last-message F` | atomic write (temp file, fsync, rename) of the validated object, only on success |
| `--cd`, `-C`, `--add-dir` | working directory, `--add-dir` |
| `--json` | Claude's own stream-json events are passed through unchanged, only for jobs with `"json_events": true` |
| `-c mcp_servers.NAME.command/args` | `--mcp-config` for servers allowed by the policy (command and script pinned) |
| `-c mcp_servers.NAME.enabled=false` | accepted: `--strict-mcp-config` already excludes every server the policy does not add |
| `-c approvals_reviewer=...` | accepted only for jobs that list it in `accept_config` |
| `--enable apps`, `-c apps.*` | refused: Codex app connectors have no Claude equivalent |
| `--version` | `agent-exec 1.0.0 (claude-code X; job=J; model=M)`; runners that parse the second field see a version above their Codex minimum |
| any other subcommand or option | refused with exit 2 |

Exit codes: 0 success; 1 model or runtime failure (Codex's generic failure; a 429 or usage limit is named in stderr); 2 usage, policy or schema error; 128+N after forwarding signal N to Claude. Runner semantics (statuses, deferrals, checkpoints, retries) stay unchanged because the runners themselves stay unchanged.

## Safety model

The outer systemd sandbox stays authoritative. The adapter is not a sandbox. Whatever the unit allows (`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=`, `RestrictAddressFamilies=`, the user it runs as) is what the model's shell can reach, exactly as it was under Codex. The provider drop-in adds one writable path at most: the dedicated Claude configuration directory.

Explicit tool allowlists. Each job maps each permitted sandbox to a named tool profile. The adapter passes that list as `--tools` and the same list (plus pinned MCP tools) as `--allowedTools`, with `--permission-mode dontAsk --permission-prompts none`, so anything outside the list is denied rather than prompted. It never uses `bypassPermissions` or `--dangerously-skip-permissions`, and the settings file disables bypass mode.

Drift is fatal. The session's init event is checked for the model, permission mode `dontAsk`, the exact tool set and every required MCP server being connected. Any mismatch stops Claude and the run fails without output. This matters because scheduled jobs follow whatever Claude Code version `claude_bin` points to.

Unknown input is refused, not ignored. Unknown `codex exec` options, `-c` keys, models, sandboxes, effort levels and MCP commands exit 2 before Claude starts. JSON Schema keywords outside the supported subset are refused up front so a constraint can never be silently skipped. The structured result is validated again against the runner's original schema before anything is written.

Credentials stay out of the model's reach. The automation token travels through an inherited pipe (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`), never the environment, and the token file must grant no permissions to group or others. Like Codex's default shell policy, variables whose names contain `KEY`, `SECRET` or `TOKEN` are removed from the child environment, as are `ANTHROPIC_*`, provider-switch variables and any inherited `CLAUDE_CONFIG_DIR`, so a stray API key can never switch a job to API billing. The root-owned settings file denies the Read tool on credential files and denies local deploy commands.

Isolation from interactive use. Sessions use a dedicated `CLAUDE_CONFIG_DIR`, project settings only (`--setting-sources project`), strict MCP configuration, no session persistence, no auto-memory, no auto-update and no claude.ai connectors. The project's own `CLAUDE.md` (which can import `AGENTS.md`, so both CLIs read the same rules) and its project hooks still load, and the probe proves that they do.

Process contract. SIGTERM, SIGINT and SIGHUP are forwarded to Claude's process group and every descendant, followed by SIGKILL after 10 seconds; the adapter then exits 128+signal and writes no output. Claude also receives SIGTERM if the adapter itself dies (`PR_SET_PDEATHSIG`).

Workspace hygiene. Empty, untracked placeholder files (for example `.npmrc`, `yarn.lock`) and an empty `node_modules/.bin` that were not present before the session are removed afterwards and the removal is logged, because they once appeared in a working directory and would have made the next scheduled run see a dirty checkout. Files that existed before, are tracked, or have content are left alone.

## Files

| Path | Purpose |
| --- | --- |
| `agent_exec.py` | the adapter (single file, stdlib only) |
| `policy.json` | per-job models, sandboxes, tool profiles, MCP allowlist; installed to `/etc/agent-provider/` |
| `claude-settings.json` | automation settings passed with `--settings`; installed to `/etc/agent-provider/` |
| `systemd/<job>.70-agent-provider.conf` | provider drop-ins installed by `agent-jobs provider` |
| `agent_jobs.py` | operator CLI, installed as `/usr/local/bin/agent-jobs` |
| `probe_job.sh`, `probe.schema.json` | sandbox probes run by `agent-jobs probe` |
| `install.py` | versioned install and rollback |
| `tests/` | contract tests and the fake `claude` binary |

## Requirements

- Linux with systemd (the adapter uses `prctl` and `/proc` for process handling; the operator CLI drives `systemctl` and `journalctl`).
- Python 3.9 or newer, standard library only.
- Claude Code installed for the job user, and a long-lived token from `claude setup-token` (requires a Claude subscription).
- Codex CLI stays installed: it remains the provider for every job without a drop-in, and the rollback target.
- Your own job units, timers and runners (see "What you provide").

## Example host layout

| Path | Purpose |
| --- | --- |
| `/srv/agent` | home of the job user `agent` in the examples |
| `/srv/agent/.local/bin/claude` | Claude Code native install (`claude_bin`) |
| `/srv/agent/.local/state/agent-provider/claude-config` | dedicated `CLAUDE_CONFIG_DIR` for scheduled sessions |
| `/srv/agent/.local/state/agent-provider/claude-oauth-token` | automation token, mode 0600, owned by the job user |
| `/srv/agent/.local/state/agent-jobs/<job>/` | runner events and reports, read by `agent-jobs status` |
| `/etc/agent-provider/` | root-owned `policy.json` and `claude-settings.json` |
| `/usr/local/libexec/agent-provider/` | `versions/`, the `current` symlink and per-job `jobs/<job>/agent-exec` entries |
| `/usr/local/bin/agent-jobs` | operator CLI |
| `/var/lib/agent-provider/` | install manifests and backups, `changes.jsonl`, `paused.json` |

## Adapting it to your host

Site configuration lives in a few files, as it did in the original deployment. Job names appear in several of them; that duplication was kept so the extraction stays faithful to the code that ran.

| File | What to change |
| --- | --- |
| `policy.json` | `claude_bin`, `claude_config_dir`, `oauth_token_file`, `settings_file`; tool profiles; one entry per job (see "Policy reference") |
| `claude-settings.json` | deny rules for your credential paths and your deploy commands |
| `systemd/<job>.70-agent-provider.conf` | one per job: the variable your runner reads for the CLI path (`CODEX_BIN` here), its model variable, and `ReadWritePaths=` for the Claude config directory if the unit makes HOME read-only |
| `agent_jobs.py` | the site block at the top: `BASE`, `CODEX`, `STATE`, `TASKS`, `MODEL_ENV` and the `JOBS` table (unit, services, timers, events and reports per job) |
| `install.py` | `JOBS`, `INSTALL_HOST` (the installer refuses to run on any other hostname) and the job-to-unit map in `rollback()` |
| `probe_job.sh` | `INSTRUCTIONS_QUESTION` and `INSTRUCTIONS_ANSWER`, `HOOK_BLOCKED_COMMAND`, the per-job case (CLI and model variables, checkout, output schemas) and any canary or dry-run entry points |

The two example jobs are `backlog`, a write job on a plain unit (`agent-backlog.service`), and `review`, a read-only job on a templated unit (`agent-review@.service` with instances `code` and `docs`).

## Install

1. Create the job user, its units and timers, and its runners. Run them on Codex first; nothing here changes until a drop-in exists.
2. Install Claude Code for the job user and create the automation token in the dedicated config directory, then store it without echoing it: `sudo -u agent env CLAUDE_CONFIG_DIR=/srv/agent/.local/state/agent-provider/claude-config /srv/agent/.local/bin/claude setup-token`, then `sudo -u agent install -m 600 /dev/stdin /srv/agent/.local/state/agent-provider/claude-oauth-token`, paste the token and press Ctrl-D.
3. Edit the site configuration, run the tests and commit. The installer refuses a dirty checkout because each install is recorded by commit.
4. `sudo python3 install.py`. This installs the runtime and the per-job entries and changes no provider.
5. `agent-jobs doctor`, then `sudo agent-jobs probe JOB` for each job you intend to switch. It must end with exit 0.
6. `sudo agent-jobs provider JOB claude` (add `--wait MINUTES` if the job is running).

Roll back by switching every job back with `sudo agent-jobs provider JOB codex`, then `sudo python3 install.py --rollback /var/lib/agent-provider/install-<stamp>/manifest.json` (the path is printed by the install). Rollback restores programs and configuration only; job state, checkpoints and reports are never touched in either direction.

To update only the operator CLI after testing and committing: `sudo python3 install.py --operator-cli-only`. It clones the active runtime version, replaces only `agent_jobs.py` and switches the `current` pointer atomically; the adapter, policy, settings, secrets and drop-ins stay untouched, and the printed manifest rolls the pointer back.

## Operating

```sh
agent-jobs status                        # provider, model, timers, last result per job
agent-jobs doctor                        # CLI, token expiry, policy, drop-in drift
sudo agent-jobs probe review             # adapter inside a copy of the real unit sandbox
sudo agent-jobs probe backlog --mode canary
sudo agent-jobs probe review --mode dry-run
sudo agent-jobs provider backlog claude  # or codex; waits for a safe boundary with --wait N
sudo agent-jobs pause all                # disable --now timers only; running jobs finish
sudo agent-jobs resume all               # restores each timer's previous state
sudo agent-jobs run review --task docs   # start now; the job still honours its own locks and windows
agent-jobs logs backlog -n 200
```

A provider switch stops only that job's timers, waits until the job is idle, installs or removes the drop-in, reloads systemd, confirms that the unit environment now points at the right CLI and restores the timers. It never edits schedules or state, so a rollback keeps all completed work and checkpoints. Every change is appended to `/var/lib/agent-provider/changes.jsonl`. Read-only commands run as any user; changing commands re-run themselves through `sudo -n`.

## Probes

`sudo agent-jobs probe JOB` copies the job's real unit and its drop-ins into `/run/systemd/system` as `agent-provider-probe-<job>`, adds the Claude provider drop-in, clears `ExecCondition=`, `ExecStartPre=` and `ExecStartPost=` (so pre-checks and heartbeats do not fire), replaces `ExecStart=` with `probe_job.sh`, runs it with `systemctl start --wait`, prints its journal and removes the transient unit. The probe therefore runs as the same user, with the same sandbox, slice and environment as the real job.

In the default `probe` mode it makes harmless model calls and checks that:

- the project's own instructions loaded (the model must state a fact that only they contain);
- on write jobs, a project PreToolUse hook refused a known command;
- on read-only jobs, no Bash tool is exposed;
- each of the job's real output schemas is accepted by Claude and the result validates against it.

`--mode canary` and `--mode dry-run` exec a runner's own canary or dry-run entry point inside the same copied unit, for an end-to-end check with real prompts.

## What you provide

The adapter replaces one binary; everything around it stays yours.

Runners. A runner reads the CLI path from a variable its unit sets (`CODEX_BIN` in the examples) and calls it with the prompt on stdin, `--output-schema` and `--output-last-message` (both required by the adapter for unattended jobs). Exit 0 means the output file holds an object that is valid against the schema; 1 is a model or runtime failure; 2 means the adapter refused the call; 128+N means the run was stopped by a signal. For example:

```sh
"${CODEX_BIN}" exec --ephemeral --ignore-user-config --disable apps --disable plugins \
  --sandbox workspace-write -c 'approval_policy="never"' \
  --model "${BACKLOG_MODEL}" -c 'model_reasoning_effort="high"' \
  --cd "${CHECKOUT}" --output-schema "${SCHEMA}" --output-last-message "${RESULT}" - < prompt.txt
```

Units. One service per job (or a templated service with instances) and its timers, running as the job user, with the outer sandbox set in the unit, the CLI variable defaulting to Codex, and a model variable. A minimal sketch:

```ini
[Service]
Type=oneshot
User=agent
Environment=CODEX_BIN=/srv/agent/.local/bin/codex
Environment=BACKLOG_MODEL=<codex model>
ExecStart=/usr/local/libexec/backlog-runner/runner.py --scheduled
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/agent/work/backlog /srv/agent/.local/state/agent-jobs/backlog /srv/agent/.codex
```

Events and reports. `agent-jobs status` shows the last line of the job's `events.jsonl` if it exists (fields `createdAt`, `kind`, `status` or `phase`, `message`) and the reports path from the `JOBS` table. Both are optional.

Probe hooks. A fact that appears only in the project's instructions (by default the probe asks for a "probe marker", so a line such as `Probe marker: agent-probe-ok` in `CLAUDE.md`, or in an `AGENTS.md` it imports, works), a PreToolUse hook that refuses `HOOK_BLOCKED_COMMAND`, and optionally canary or dry-run entry points in the runners.

Host-specific pieces of the original deployment were dropped: the shared heavy-task queue, heartbeat reporting, one runner that the original installer also deployed, and the jobs that stayed on Codex.

## Policy reference

Top level:

| Key | Meaning |
| --- | --- |
| `claude_bin` | Claude Code executable; point it at a versioned binary to pin a version |
| `claude_config_dir` | dedicated `CLAUDE_CONFIG_DIR` for scheduled sessions |
| `oauth_token_file` | automation token; must not be readable by group or others |
| `settings_file` | settings passed with `--settings` |
| `setting_sources` | passed to `--setting-sources`; `project` keeps user settings out |
| `env` | fixed, non-secret variables for the session (here: Bash tool timeouts of 30 and 90 minutes) |
| `profiles` | named tool lists |
| `jobs` | one entry per job name, matching the `--job` argument of each `agent-exec` entry |

Per job:

| Key | Meaning |
| --- | --- |
| `enabled` | `false` refuses every call with exit 2 and names `reason` |
| `models`, `default_model` | allowed Claude model IDs, and the model used when the runner passes none |
| `sandboxes` | Codex sandbox name to tool profile; a sandbox not listed is refused |
| `default_sandbox` | used when the runner passes no `--sandbox` (default `read-only`) |
| `extra_tools` | tools added to the profile for this job |
| `network` | allows `sandbox_workspace_write.network_access=true` |
| `json_events` | allows `--json` (Claude stream-json events, passed through unchanged) |
| `accept_config` | extra `-c` keys accepted and ignored (only `approvals_reviewer` is recognised) |
| `mcp_servers` | MCP servers the runner may start, each with pinned `command`, `script` (first argument), allowed `tools` and `required` |

A job with a pinned MCP server (from the tests):

```json
"triage": {
  "enabled": true,
  "models": ["claude-opus-5-5"],
  "default_model": "claude-opus-5-5",
  "sandboxes": {"read-only": "read"},
  "json_events": true,
  "mcp_servers": {
    "unit_save": {
      "command": "/usr/bin/python3",
      "script": "/opt/agent-jobs/checkpoint_mcp.py",
      "tools": ["save_topic", "save_proposal"],
      "required": true
    }
  }
}
```

## Running the tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The suite starts the real adapter as a subprocess against `tests/fake_claude.py`, which records argv, environment, stdin and the token it received, and can simulate success, model errors, crashes, permission or tool drift, a long run with a grandchild process, and placeholder files. Nothing touches the network or a real CLI. The installer test exercises the operator-only update in a temporary directory.

CI runs the suite on Ubuntu with Python 3.10, 3.12 and 3.13 (`.github/workflows/tests.yml`). It also passes locally on macOS with Python 3.9, 3.13 and 3.14, although the process handling is written for Linux.

## Versions

The mapping was written against Claude Code 2.1.287 and Codex CLI 0.156.1 (the minimum the runners required) in October 2026.

On 2026-11-02 it was re-checked against the `--help` output of Claude Code 2.1.316 and Codex CLI 0.166.0-alpha.5.1. Every flag the adapter passes to `claude` and every `codex exec` option it accepts is still present, and the environment variable names and settings keys it relies on still appear in the Claude Code 2.1.316 binary. Differences that do not change the adapter's behaviour: `claude --effort` also offers `max` (not mapped, since Codex has no such level), `--mcp-config` now takes several values (the adapter passes one), and the `--permission-mode` choices are `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk` and `plan` (the adapter only passes and expects `dontAsk`).

## Limitations

- Linux and systemd only.
- Only the `codex exec` subset in the table is supported. Other subcommands (`app-server`, `resume`, `review`), images, profiles and Codex app connectors are refused.
- JSON Schema support is a subset: `type`, `properties`, `required`, `additionalProperties`, `items` (not tuple form), `enum`, `const`, string, number and array bounds, `pattern`, `anyOf`, `oneOf`, `allOf`, local `$ref` with `$defs` or `definitions`, plus annotations. Anything else is refused.
- `--json` passes Claude's events through, not Codex's; a runner that parses Codex events needs its own handling, which is why it is opt-in per job.
- Scheduled and interactive use share one subscription's usage limits. A limit stops a run with exit 1 and "rate limit" in stderr; if the organisation disables API overage, it never turns into billing.
- Scheduled jobs follow whatever `claude_bin` points to. The init-event check catches drift in model, permissions and tools, not every behaviour change; pin a version by pointing `claude_bin` at a versioned binary.
- Claude Code's Bash tool refuses a standalone long `sleep`; prompts that wait must poll, for example `until <check>; do sleep 10; done`.
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is deliberately left unset because in Claude Code 2.1.287 it forced the permission mode away from `dontAsk`. This was not re-tested on later versions.
- `doctor` estimates token expiry as one year after the token file's modification time.

## What this is not

- Not a general Codex-compatible CLI, and not for interactive use.
- Not a sandbox: the systemd unit is the security boundary.
- Not a scheduler: it does not create units, timers or runners.
- Not a prompt translator: prompts and runner logic stay as they are, and the two providers' models will not behave identically.
- Not a way around usage limits, billing or the terms of either product.
- Not affiliated with Anthropic or OpenAI. Claude Code and Codex are their respective products.

Built with Claude Code and Codex.

## License

MIT, see [LICENSE](LICENSE).
