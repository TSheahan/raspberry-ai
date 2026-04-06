#!/usr/bin/env python3
"""
One-shot Cursor CLI smoke test: same spawn shape as master.py / CursorAgentSession
(sudo + AGENT_BIN + stream-json argv), without Pipecat, STT, or recorder.

Typical Pi (as voice), from repo root:

  set -a && . /home/voice/.env && set +a && \\
    python3 agent-artifacts/scripts/smoke-wrapped-agent.py "Say hello in five words."

Or with an explicit env file (simple KEY=value lines; # comments ok):

  python3 agent-artifacts/scripts/smoke-wrapped-agent.py \\
    --env-file /home/voice/.env \\
    "Say hello in five words."

Environment (same names as master.py):
  AGENT_USER   — if set, prefix: sudo -u <user> -H --
  AGENT_BIN    — default ~/.local/bin/agent
  AGENT_WORKSPACE
  AGENT_MODEL  — default claude-4.6-sonnet-medium
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path


def _load_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            os.environ[key] = val


def _build_cmd(
    agent_bin: Path,
    workspace: Path,
    model: str,
    resume: str | None,
) -> list[str]:
    args: list[str] = [
        str(agent_bin),
        "-p",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--force",
        "--yolo",
        "--trust",
        "--workspace",
        str(workspace),
        "--model",
        model,
    ]
    if resume:
        args.extend(["--resume", resume])
    agent_user = os.environ.get("AGENT_USER", "").strip()
    if agent_user:
        return ["sudo", "-u", agent_user, "-H", "--", *args]
    return args


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test AGENT_BIN (e.g. cursor-agent-wrapper) with one stdin prompt."
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello in one short sentence.",
        help="Transcript sent on stdin (then stdin is closed).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Load KEY=value pairs into the environment before reading AGENT_*.",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Pass --resume to the CLI (session continuity smoke).",
    )
    args = parser.parse_args()

    if args.env_file:
        if not args.env_file.is_file():
            print(f"smoke-wrapped-agent: env file not found: {args.env_file}", file=sys.stderr)
            return 2
        _load_env_file(args.env_file)

    home = Path.home()
    agent_bin = Path(os.environ.get("AGENT_BIN", str(home / ".local/bin/agent")))
    workspace = Path(os.environ.get("AGENT_WORKSPACE", str(home / "raspberry-ai")))
    model = os.environ.get("AGENT_MODEL", "claude-4.6-sonnet-medium")

    if not workspace.is_dir():
        print(f"smoke-wrapped-agent: AGENT_WORKSPACE is not a directory: {workspace}", file=sys.stderr)
        return 2

    cmd = _build_cmd(agent_bin, workspace, model, args.resume)
    print("smoke-wrapped-agent: cmd[0:6] =", cmd[:6], "…", file=sys.stderr)
    print("smoke-wrapped-agent: streaming stdout (stream-json lines)…", file=sys.stderr)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    stop = threading.Event()

    def _drain_stderr() -> None:
        err = proc.stderr
        if err is None:
            return
        try:
            for line in iter(err.readline, ""):
                if stop.is_set():
                    break
                line = line.rstrip()
                if line:
                    print(line, file=sys.stderr)
        except Exception:
            pass

    t = threading.Thread(target=_drain_stderr, name="smoke-stderr", daemon=True)
    t.start()

    try:
        assert proc.stdin is not None
        proc.stdin.write(args.prompt + "\n")
        proc.stdin.close()
    except BrokenPipeError:
        stop.set()
        proc.wait(timeout=5)
        print("smoke-wrapped-agent: stdin broken (process exited early?)", file=sys.stderr)
        return 1

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    finally:
        stop.set()
        t.join(timeout=2)

    proc.wait()
    print(f"\nsmoke-wrapped-agent: exit code {proc.returncode}", file=sys.stderr)
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
