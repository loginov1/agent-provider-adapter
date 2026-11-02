#!/usr/bin/env python3
"""Operate scheduled agent jobs while Codex and Claude run side by side.

Provider selection lives in one systemd drop-in per job
(`<unit>.d/70-agent-provider.conf`). Present: the job calls the Claude adapter.
Absent: the unit's original Codex configuration applies unchanged. Timers,
schedules, locks, budgets and checkpoints are never edited here.

Read-only commands run as any user; changes re-run themselves through sudo.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RUNTIME = Path(__file__).resolve().parent
UNIT_DIR = Path("/etc/systemd/system")
DROPIN = "70-agent-provider.conf"
VAR = Path("/var/lib/agent-provider")
POLICY = Path("/etc/agent-provider/policy.json")

# Site configuration: edit the values below for your host (README, "Adapting
# it to your host"). They describe the two example jobs.
BASE = Path("/usr/local/libexec/agent-provider")  # runtime installed by install.py
CODEX = Path("/srv/agent/.local/bin/codex")  # the rollback provider; doctor checks it
STATE = Path("/srv/agent/.local/state/agent-jobs")  # runners' events and reports
# Instances of the templated example unit agent-review@.service. Commands that
# need some instance of a templated unit use the first one.
TASKS = ("code", "docs")
DEFAULT_TASK = TASKS[0]
# Unit environment variables that name a job's model, shown by `status`.
MODEL_ENV = {"BACKLOG_MODEL", "REVIEW_MODEL"}

JOBS = {
    "backlog": {
        "unit": "agent-backlog.service", "services": ["agent-backlog.service"],
        "timers": ["agent-backlog.timer"], "events": STATE / "backlog/events.jsonl",
        "reports": STATE / "backlog/reports", "migratable": True},
    "review": {
        "unit": "agent-review@.service", "services": [f"agent-review@{t}.service" for t in TASKS],
        "timers": [f"agent-review-{t}.timer" for t in TASKS], "events": STATE / "review/events.jsonl",
        "reports": STATE / "review/reports", "migratable": True},
    # A job that must stay on one CLI (or uses no model) can still be listed,
    # paused and resumed: "migratable": False, "provider": "codex" (shown by
    # status; default "none") and a "note" explaining why.
}


def sh(*args, check=False):
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def show(unit, *props):
    out = sh("systemctl", "show", unit, *[f"--property={p}" for p in props]).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def require_root(argv):
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-n", sys.executable, "-I", str(Path(__file__).resolve()), *argv])


def dropin_path(job):
    return UNIT_DIR / f"{JOBS[job]['unit']}.d" / DROPIN


def template_path(job):
    return RUNTIME / "systemd" / f"{job}.{DROPIN}"


def provider(job):
    if not JOBS[job]["migratable"]:
        return JOBS[job].get("provider", "none")
    return "claude" if dropin_path(job).exists() else "codex"


def last_event(path):
    if not path or not Path(path).exists():
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            lines = handle.read().decode(errors="replace").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, json.JSONDecodeError, IndexError):
        return None


def job_status(job):
    spec = JOBS[job]
    timers = []
    for timer in spec["timers"]:
        props = show(timer, "UnitFileState", "ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec")
        timers.append({"timer": timer, "enabled": props.get("UnitFileState"),
                       "active": props.get("ActiveState"), "next": props.get("NextElapseUSecRealtime"),
                       "last": props.get("LastTriggerUSec")})
    services = {svc: show(svc, "ActiveState").get("ActiveState") for svc in spec["services"]}
    unit_props = show(spec["services"][0], "NeedDaemonReload", "Environment")
    env = unit_props.get("Environment", "")
    model = next((part.split("=", 1)[1] for part in env.split()
                  if part.split("=", 1)[0] in MODEL_ENV), None)
    event = last_event(spec["events"])
    return {"job": job, "provider": provider(job), "model": model or "(runner default)",
            "timers": timers, "services": services, "needs_daemon_reload": unit_props.get("NeedDaemonReload") == "yes",
            "last_event": event, "reports": str(spec["reports"]), "note": spec.get("note")}


def cmd_status(args):
    rows = [job_status(job) for job in (args.job or JOBS)]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for row in rows:
        running = [s for s, state in row["services"].items() if state not in ("inactive", "failed", None)]
        print(f"{row['job']:<12} provider={row['provider']:<6} model={row['model']}"
              f"{'  RUNNING: ' + ','.join(running) if running else ''}"
              f"{'  (daemon-reload pending)' if row['needs_daemon_reload'] else ''}")
        for timer in row["timers"]:
            print(f"    {timer['timer']:<34} {timer['enabled']:<9} {timer['active']:<8} next={timer['next'] or '-'}")
        if row["last_event"]:
            event = row["last_event"]
            stamp = event.get("createdAt", "")
            what = event.get("status") or event.get("phase") or event.get("kind")
            print(f"    last: {stamp} {event.get('kind', '')}/{what}: {str(event.get('message', ''))[:110]}")
        print(f"    reports: {row['reports']}")
        if row["note"]:
            print(f"    note: {row['note']}")
    return 0


def log_change(record):
    VAR.mkdir(mode=0o700, exist_ok=True)
    record["at"] = datetime.now(timezone.utc).isoformat()
    record["by"] = os.environ.get("SUDO_USER", "root")
    with open(VAR / "changes.jsonl", "a") as out:
        out.write(json.dumps(record) + "\n")


def wait_idle(job, wait_minutes):
    deadline = datetime.now() + timedelta(minutes=wait_minutes)
    while True:
        busy = [s for s in JOBS[job]["services"]
                if show(s, "ActiveState").get("ActiveState") not in ("inactive", "failed")]
        if not busy:
            return
        if datetime.now() >= deadline:
            raise SystemExit(f"{job}: still running ({', '.join(busy)}); retry later or pass --wait MINUTES")
        print(f"waiting for {', '.join(busy)} to finish...", flush=True)
        subprocess.run(["sleep", "30"])


def cmd_provider(args):
    require_root(sys.argv[1:])
    job, target = args.job, args.provider
    if not JOBS[job]["migratable"]:
        raise SystemExit(f"{job} cannot change provider: {JOBS[job]['note']}")
    if provider(job) == target:
        print(f"{job} already uses {target}")
        return 0
    timers = JOBS[job]["timers"]
    active = [t for t in timers if show(t, "ActiveState").get("ActiveState") == "active"]
    # Stop only this job's timers for the switch, so a tick cannot start the
    # service between the idle check and the drop-in change.
    for timer in active:
        sh("systemctl", "stop", timer, check=True)
    try:
        wait_idle(job, args.wait)
        path = dropin_path(job)
        if target == "claude":
            policy = json.loads(POLICY.read_text())
            if not policy["jobs"][job].get("enabled"):
                raise SystemExit(f"policy does not enable {job} for Claude")
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_suffix(".tmp")
            shutil.copyfile(template_path(job), temporary)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)
        unit = JOBS[job]["unit"].replace("@.service", f"@{DEFAULT_TASK}.service")
        verify = sh("systemd-analyze", "verify", str(UNIT_DIR / JOBS[job]["unit"]))
        if verify.returncode != 0 and "Failed" in verify.stderr:
            print(verify.stderr, file=sys.stderr)
        sh("systemctl", "daemon-reload", check=True)
        env = show(unit, "Environment").get("Environment", "")
        uses_adapter = f"{BASE}/jobs/" in env
        if uses_adapter != (target == "claude"):
            raise SystemExit(f"{unit} environment does not reflect provider {target}; inspect {path}")
        log_change({"action": "provider", "job": job, "provider": target})
        print(f"{job}: provider is now {target}; takes effect at the next scheduled start")
    finally:
        for timer in active:
            sh("systemctl", "start", timer, check=True)
    return 0


def paused_state():
    path = VAR / "paused.json"
    return json.loads(path.read_text()) if path.exists() else {}


def cmd_pause(args):
    require_root(sys.argv[1:])
    jobs = list(JOBS) if args.job == "all" else [args.job]
    paused = paused_state()
    for job in jobs:
        for timer in JOBS[job]["timers"]:
            if timer in paused:
                continue
            props = show(timer, "UnitFileState", "ActiveState")
            paused[timer] = {"enabled": props.get("UnitFileState"), "active": props.get("ActiveState")}
            sh("systemctl", "disable", "--now", timer, check=True)
            print(f"paused {timer} (was {props.get('UnitFileState')}/{props.get('ActiveState')}); "
                  "a run already in progress continues to its normal end")
    VAR.mkdir(mode=0o700, exist_ok=True)
    (VAR / "paused.json").write_text(json.dumps(paused, indent=2))
    log_change({"action": "pause", "jobs": jobs})
    return 0


def cmd_resume(args):
    require_root(sys.argv[1:])
    jobs = list(JOBS) if args.job == "all" else [args.job]
    paused = paused_state()
    for job in jobs:
        for timer in JOBS[job]["timers"]:
            prior = paused.pop(timer, None)
            if prior is None:
                continue
            if prior["enabled"] == "enabled":
                sh("systemctl", "enable", timer, check=True)
            if prior["active"] == "active":
                sh("systemctl", "start", timer, check=True)
            print(f"resumed {timer} to {prior['enabled']}/{prior['active']}")
    (VAR / "paused.json").write_text(json.dumps(paused, indent=2))
    log_change({"action": "resume", "jobs": jobs})
    return 0


def cmd_run(args):
    require_root(sys.argv[1:])
    services = JOBS[args.job]["services"]
    if JOBS[args.job]["unit"].endswith("@.service"):
        if not args.task:
            raise SystemExit(f"{args.job} needs --task " + "|".join(TASKS))
        services = [JOBS[args.job]["unit"].replace("@.service", f"@{args.task}.service")]
    for service in services:
        sh("systemctl", "start", "--no-block", service, check=True)
        print(f"started {service}; follow with: agent-jobs logs {args.job}")
    return 0


def cmd_logs(args):
    units = []
    for service in JOBS[args.job]["services"]:
        units += ["-u", service]
    return subprocess.call(["journalctl", "--no-pager", "-n", str(args.lines), *units])


def probe_unit_text(job, task):
    """A transient copy of the job's real unit, with only ExecStart replaced."""
    spec = JOBS[job]
    source = UNIT_DIR / spec["unit"]
    name = "agent-provider-probe-" + job + ("@.service" if spec["unit"].endswith("@.service") else ".service")
    return source, name


def cmd_probe(args):
    require_root(sys.argv[1:])
    job = args.job
    if not JOBS[job]["migratable"]:
        raise SystemExit(f"{job} has no Claude probe: {JOBS[job]['note']}")
    source, name = probe_unit_text(job, args.task)
    run = Path("/run/systemd/system")
    unit = run / name
    dropins = run / f"{name}.d"
    shutil.rmtree(dropins, ignore_errors=True)
    shutil.copyfile(source, unit)
    dropins.mkdir()
    real_dropins = UNIT_DIR / f"{source.name}.d"
    for conf in sorted(real_dropins.glob("*.conf")) if real_dropins.is_dir() else []:
        if conf.name != DROPIN:
            shutil.copyfile(conf, dropins / conf.name)
    shutil.copyfile(template_path(job), dropins / DROPIN)
    command = [str(RUNTIME / "probe_job.sh"), job]
    if args.mode != "probe":
        command = [str(RUNTIME / "probe_job.sh"), job, args.mode]
    (dropins / "99-probe.conf").write_text(
        "[Unit]\nDescription=agent-jobs sandbox probe for " + job + "\n"
        "[Service]\nType=oneshot\nRestart=no\nRemainAfterExit=no\nExecCondition=\nExecStartPre=\n"
        "ExecStartPost=\nExecStart=\nExecStart=" + " ".join(command) + "\n"
        "StandardOutput=journal\nStandardError=journal\nTimeoutStartSec=" + str(args.minutes) + "min\n")
    instance = name.replace("@.service", f"@{args.task or DEFAULT_TASK}.service")
    try:
        sh("systemctl", "daemon-reload", check=True)
        since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        code = subprocess.call(["systemctl", "start", "--wait", instance])
        subprocess.call(["journalctl", "--no-pager", "-o", "cat", "-u", instance, "--since", since])
        result = show(instance, "Result", "ExecMainStatus")
        print(f"probe {job}: systemctl exit={code} result={result.get('Result')} "
              f"status={result.get('ExecMainStatus')}")
        log_change({"action": "probe", "job": job, "mode": args.mode, "result": result})
        return 0 if code == 0 else 1
    finally:
        unit.unlink(missing_ok=True)
        shutil.rmtree(dropins, ignore_errors=True)
        sh("systemctl", "daemon-reload")


def cmd_doctor(_args):
    problems = []
    policy = json.loads(POLICY.read_text())
    claude = Path(policy["claude_bin"])
    version = sh(str(claude), "--version").stdout.strip() if claude.exists() else "missing"
    print(f"claude: {claude} -> {version}")
    token = Path(policy["oauth_token_file"])
    if not token.exists():
        problems.append("automation token file is missing")
    else:
        info = token.stat()
        if info.st_mode & 0o077:
            problems.append("automation token is readable by group/others")
        created = datetime.fromtimestamp(info.st_mtime, timezone.utc)
        expires = created + timedelta(days=365)
        days = (expires - datetime.now(timezone.utc)).days
        print(f"automation token: created {created:%Y-%m-%d}, setup-token expiry ~{expires:%Y-%m-%d} ({days} days)")
        if days < 30:
            problems.append("automation token expires within 30 days: renew with claude setup-token")
    codex = CODEX
    print(f"codex (rollback provider): {'present' if codex.exists() else 'MISSING'}")
    current = BASE / "current"
    print(f"adapter runtime: {current.resolve() if current.exists() else 'missing'}")
    manifest = current / "INSTALL.json"
    if manifest.exists():
        info = json.loads(manifest.read_text())
        print(f"installed from {info.get('branch')} @ {info.get('commit')} ({info.get('installed_at')})")
    for job in JOBS:
        if JOBS[job]["migratable"] and provider(job) == "claude":
            if dropin_path(job).read_text() != template_path(job).read_text():
                problems.append(f"{job}: installed drop-in differs from the runtime template")
            if not policy["jobs"][job].get("enabled"):
                problems.append(f"{job}: provider is claude but the policy disables it")
    for problem in problems:
        print("PROBLEM: " + problem)
    print("doctor: ok" if not problems else f"doctor: {len(problems)} problem(s)")
    return 0 if not problems else 1


def main():
    parser = argparse.ArgumentParser(prog="agent-jobs", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="provider, model, timers and last result of every job")
    status.add_argument("job", nargs="*", choices=list(JOBS))
    status.add_argument("--json", action="store_true")
    change = sub.add_parser("provider", help="switch one job between codex and claude at a safe boundary")
    change.add_argument("job", choices=[j for j in JOBS if JOBS[j]["migratable"]])
    change.add_argument("provider", choices=["claude", "codex"])
    change.add_argument("--wait", type=int, default=0, metavar="MINUTES",
                        help="wait up to MINUTES for a running job to finish first")
    for name, helptext in (("pause", "disable --now the job's timers, remembering their state"),
                           ("resume", "restore timers paused by this tool")):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("job", choices=list(JOBS) + ["all"])
    run = sub.add_parser("run", help="start a job's service now (it still honours locks and windows)")
    run.add_argument("job", choices=list(JOBS))
    run.add_argument("--task", choices=TASKS)
    logs = sub.add_parser("logs", help="recent journal lines")
    logs.add_argument("job", choices=list(JOBS))
    logs.add_argument("-n", "--lines", type=int, default=80)
    probe = sub.add_parser("probe", help="run the Claude adapter inside a copy of the job's real sandbox")
    probe.add_argument("job", choices=[j for j in JOBS if JOBS[j]["migratable"]])
    probe.add_argument("--task", choices=TASKS)
    probe.add_argument("--mode", default="probe", choices=["probe", "canary", "dry-run"],
                       help="probe: one harmless model call; canary: the runner's own canary run; "
                            "dry-run: the runner's dry run with a 2-item limit (see probe_job.sh)")
    probe.add_argument("--minutes", type=int, default=20)
    sub.add_parser("doctor", help="check adapter, auth token, policy and drop-ins")
    args = parser.parse_args()
    return {"status": cmd_status, "provider": cmd_provider, "pause": cmd_pause, "resume": cmd_resume,
            "run": cmd_run, "logs": cmd_logs, "probe": cmd_probe, "doctor": cmd_doctor}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
