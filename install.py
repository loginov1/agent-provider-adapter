#!/usr/bin/env python3
"""Install one reviewed agent-provider runtime version on the jobs host.

The runtime lives outside every automatically updated checkout:
  /usr/local/libexec/agent-provider/versions/<stamp>-<commit>/  (immutable copy)
  /usr/local/libexec/agent-provider/current -> versions/...     (atomic switch)
  /usr/local/libexec/agent-provider/jobs/<job>/agent-exec       (per-job entry)
  /usr/local/bin/agent-jobs                                     (operator CLI)
  /etc/agent-provider/{policy,claude-settings}.json             (root-owned policy)

Installing changes no provider: drop-ins are added or removed only by
`agent-jobs provider JOB claude|codex`. `--rollback MANIFEST` restores every
file this installer replaced. Job state, checkpoints and reports are never
touched by either direction.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE
BASE = Path("/usr/local/libexec/agent-provider")
INSTALL_STATE = Path("/var/lib/agent-provider")
# Site configuration: the jobs that get an agent-exec entry, and the only
# hostname this installer runs on (a guard against installing elsewhere).
JOBS = ("backlog", "review")
INSTALL_HOST = "jobs-host"
RUNTIME_FILES = {"agent_exec.py": 0o755, "agent_jobs.py": 0o755, "probe_job.sh": 0o755,
                 "probe.schema.json": 0o644, "policy.json": 0o644, "claude-settings.json": 0o644,
                 "README.md": 0o644}
MANAGED = [Path("/etc/agent-provider/policy.json"), Path("/etc/agent-provider/claude-settings.json"),
           Path("/usr/local/bin/agent-jobs"), BASE / "current"]


def git(*args):
    # Run git as the checkout's owner so it accepts the repository under sudo.
    owner = pwd.getpwuid(REPO.stat().st_uid).pw_name
    return subprocess.check_output(["sudo", "-u", owner, "git", "-C", str(REPO), *args], text=True).strip()


def capture(path, backup, index):
    entry = {"path": str(path), "existed": path.exists() or path.is_symlink()}
    if path.is_symlink():
        entry["symlink"] = os.readlink(path)
    elif path.exists():
        saved = backup / f"{index}-{path.name}"
        shutil.copy2(path, saved)
        info = path.stat()
        entry.update(backup=str(saved), mode=info.st_mode & 0o7777, uid=info.st_uid, gid=info.st_gid)
    return entry


def restore(entries):
    for entry in reversed(entries):
        path = Path(entry["path"])
        if path.is_symlink() or path.is_file():
            path.unlink()
        if "symlink" in entry:
            path.symlink_to(entry["symlink"])
        elif "backup" in entry:
            shutil.copy2(entry["backup"], path)
            os.chown(path, entry["uid"], entry["gid"])
            path.chmod(entry["mode"])


def install_file(source, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.new")
    shutil.copyfile(source, temporary)
    os.chown(temporary, 0, 0)
    temporary.chmod(mode)
    os.replace(temporary, target)


def symlink(target, link):
    temporary = link.with_name(f".{link.name}.new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def install_operator_cli():
    if git("status", "--porcelain", "--", "."):
        raise SystemExit("Commit the operator CLI sources first")
    current = BASE / "current"
    if not current.is_symlink():
        raise SystemExit("Expected an existing versioned provider runtime")
    commit = git("rev-parse", "--short=12", "HEAD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = INSTALL_STATE / f"install-{stamp}"
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"scope": "operator_cli", "installed_at": stamp, "commit": commit,
                "files": [capture(current, backup, 0)]}
    version = BASE / "versions" / f"{stamp}-{commit}-cli"
    try:
        shutil.copytree(current.resolve(), version, symlinks=True)
        install_file(SOURCE / "agent_jobs.py", version / "agent_jobs.py", 0o755)
        compile((version / "agent_jobs.py").read_text(), str(version / "agent_jobs.py"), "exec")
        (version / "INSTALL.json").write_text(json.dumps({"scope": "operator_cli", "commit": commit}, indent=2) + "\n")
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        symlink(version, current)
    except BaseException:
        restore(manifest["files"])
        raise
    print(f"Installed operator CLI only; policies, providers and job runtimes preserved. Rollback: {backup / 'manifest.json'}")


def install():
    if git("status", "--porcelain", "--", "."):
        raise SystemExit("Commit the agent-provider sources first; installs are recorded by commit")
    commit = git("rev-parse", "--short=12", "HEAD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = INSTALL_STATE / f"install-{stamp}"
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"installed_at": stamp, "commit": commit, "branch": git("branch", "--show-current"),
                "source": str(SOURCE), "files": [capture(p, backup, i) for i, p in enumerate(MANAGED)]}
    version = BASE / "versions" / f"{stamp}-{commit}"
    try:
        (version / "systemd").mkdir(parents=True, mode=0o755)
        for name, mode in RUNTIME_FILES.items():
            install_file(SOURCE / name, version / name, mode)
        for conf in sorted((SOURCE / "systemd").glob("*.conf")):
            install_file(conf, version / "systemd" / conf.name, 0o644)
        (version / "INSTALL.json").write_text(json.dumps(
            {k: manifest[k] for k in ("installed_at", "commit", "branch", "source")}, indent=2) + "\n")
        for path in [BASE, BASE / "versions", version, version / "systemd"]:
            path.chmod(0o755)
        for job in JOBS:
            entry = BASE / "jobs" / job / "agent-exec"
            entry.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            source = backup / f"agent-exec-{job}"
            source.write_text("#!/bin/sh\n# Generated by install.py\n"
                              f"exec /usr/bin/python3 -I {BASE}/current/agent_exec.py --job {job} \"$@\"\n")
            install_file(source, entry, 0o755)
        for name in ("agent_exec.py", "agent_jobs.py"):
            compile((version / name).read_text(), str(version / name), "exec")  # no .pyc in the runtime
        Path("/etc/agent-provider").mkdir(mode=0o755, exist_ok=True)
        install_file(SOURCE / "policy.json", Path("/etc/agent-provider/policy.json"), 0o644)
        install_file(SOURCE / "claude-settings.json", Path("/etc/agent-provider/claude-settings.json"), 0o644)
        symlink(version, BASE / "current")
        symlink(BASE / "current" / "agent_jobs.py", Path("/usr/local/bin/agent-jobs"))
    except BaseException:
        restore(manifest["files"])
        raise
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Installed {version}\nRollback manifest: {backup / 'manifest.json'}\n"
          "No provider changed. Use `agent-jobs provider JOB claude` after `agent-jobs probe JOB`.")


def rollback(path):
    manifest = json.loads(Path(path).read_text())
    for job in (() if manifest.get("scope") == "operator_cli" else ("backlog", "review")):
        unit = {"backlog": "agent-backlog.service", "review": "agent-review@.service"}[job]
        if Path(f"/etc/systemd/system/{unit}.d/70-agent-provider.conf").exists():
            raise SystemExit(f"{job} still uses Claude; run `agent-jobs provider {job} codex --wait 180` first")
    restore(manifest["files"])
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Restored the files replaced by that install. Versioned runtimes and job state are kept.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rollback", metavar="MANIFEST")
    mode.add_argument("--operator-cli-only", action="store_true", help="Update only agent-jobs, preserving provider/job configuration")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().nodename != INSTALL_HOST:
        raise SystemExit(f"Run as root on {INSTALL_HOST}")
    if args.rollback:
        rollback(args.rollback)
    elif args.operator_cli_only:
        install_operator_cli()
    else:
        install()


if __name__ == "__main__":
    main()
