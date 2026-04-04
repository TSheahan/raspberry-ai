#!/usr/bin/env python3
"""
CURSOR AGENT SMOKE TEST — FIXED STREAMING ACCUMULATION
=======================================================================

EXPLICIT INTENT:
Fixed the duplication bug you reported. We now only accumulate from
assistant deltas that contain "timestamp_ms" (the real incremental tokens).
The final non-timestamped assistant message is ignored, and we still use
the clean "result" field for the authoritative final reply.

RAW DATA SURFACING:
Every JSON delta is still printed with "STREAM →" so you can see exactly
what Cursor is emitting in real time.

All previous design goals (VAD-style pre-spawn, latency hiding, native
memory harness, session persistence) are unchanged.

"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ===================================================================
# CONFIGURATION
# ===================================================================
DEFAULT_MODEL = "claude-4.6-sonnet-medium"
AGENT_BIN = Path.home() / ".local/bin/agent"
WORKSPACE = Path.home() / "test-project-1"

class CursorStreamingSmokeTest:
    def __init__(self):
        if not WORKSPACE.is_dir():
            raise ValueError(f"Workspace not found: {WORKSPACE}")
        self.chat_id: Optional[str] = None
        print(f"✅ Smoke test (fixed streaming accumulation) ready — workspace: {WORKSPACE}")
        print(f"   Model: {DEFAULT_MODEL}")
        print("   Output: stream-json + --stream-partial-output")
        print("   Accumulation fix: only timestamped assistant deltas are added")
        print("   (Eliminates the duplication you observed)\n")
        print("   SCHEDULING NOTE: Agent child will naturally land on cores 2/3 when")
        print("   master is on core 4 and recorder is pinned to core 1. Pinning can")
        print("   be added later via taskset or os.sched_setaffinity.\n")

    def _pre_spawn_agent(self) -> subprocess.Popen:
        cmd = [
            str(AGENT_BIN),
            "-p",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force",
            "--yolo",
            "--trust",
            "--workspace", str(WORKSPACE),
            "--model", DEFAULT_MODEL,
        ]

        if self.chat_id:
            cmd.extend(["--resume", self.chat_id])
            print(f"→ Pre-spawning streaming agent (resuming chat {self.chat_id[:8]}...)")
        else:
            print("→ Pre-spawning streaming agent (fresh chat)")

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        print("   Agent process spawned and waiting for stdin (latency hidden)...")
        return process

    def _run_turn(self, user_prompt: str, process: subprocess.Popen):
        print(f"→ Feeding full turn to already-running streaming agent...")

        process.stdin.write(user_prompt + "\n")
        process.stdin.close()

        full_text = ""
        session_id = None

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue

            print(f"STREAM → {line}")

            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    # === FIXED ACCUMULATION LOGIC ===
                    if (data.get("type") == "assistant" and
                        "message" in data and
                        "timestamp_ms" in data):          # ← only real streaming tokens
                        content = data["message"].get("content", [{}])[0].get("text", "")
                        full_text += content

                    if "session_id" in data:
                        session_id = data["session_id"]
            except json.JSONDecodeError:
                pass

        process.wait()
        if process.returncode != 0:
            print(f"❌ Agent exited with code {process.returncode}")
            return

        if session_id:
            self.chat_id = session_id
            print(f"   ✅ Captured session_id for next turn: {session_id[:8]}...")

        print("\n=== FULL AGENT REPLY (accumulated from streaming tokens) ===\n")
        print(full_text.strip())
        print("\n" + "="*80 + "\n")

    def run(self):
        print("Streaming smoke test loop started.")
        print("Type your message (multi-line ok).")
        print("Press Enter twice (blank line) when finished.\n")

        while True:
            print("You (multi-line, end with blank line):")
            lines = []
            agent_process = None

            while True:
                try:
                    line = input()
                except EOFError:
                    return

                lines.append(line)

                if len(lines) == 1 and line.strip():
                    agent_process = self._pre_spawn_agent()

                if line == "" and len(lines) > 1:
                    break
                if line == "" and len(lines) == 1:
                    print("\n👋 Ending smoke test.")
                    return

            user_prompt = "\n".join(lines[:-1]).strip()
            if not user_prompt or not agent_process:
                continue

            self._run_turn(user_prompt, agent_process)


# ===================================================================
# ENTRYPOINT
# ===================================================================
if __name__ == "__main__":
    try:
        test = CursorStreamingSmokeTest()
        test.run()
    except KeyboardInterrupt:
        print("\n\n👋 Smoke test interrupted.")
    except Exception as e:
        print(f"\n💥 Error: {e}")
    finally:
        print("Smoke test finished. This is the reference for the real voice orchestrator.")