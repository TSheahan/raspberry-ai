#!/usr/bin/env python3
"""
CURSOR AGENT SMOKE TEST — TRUE VAD-STYLE PRE-SPAWN
=======================================================================

EXPLICIT INTENT (for future reasoning / as-built voice orchestrator):
This version demonstrates pre-spawning the agent process the *instant* the user 
begins typing the first line of a new turn. This perfectly simulates the real VAD trigger:

    VAD start → spawn agent immediately (while user is still speaking / STT is
    transcribing the rest of the utterance)

    User finishes speaking → STT gives us the full transcript → feed stdin and
    close it → agent responds with almost zero additional latency.

The previous version spawned too late (only after double-newline). This one
spawns at the *very beginning* of user input.

SCHEDULING NOTE (for later):
The master script will run on core 4. The recorder child is pinned to core 1.
When we spawn the agent subprocess here, the Linux scheduler will naturally
place it on cores 2 or 3 in most cases. If we want stricter isolation later,
we can add taskset or os.sched_setaffinity (function-based pinning) exactly
as you suggested. That decision is intentionally left as a comment for the
as-built phase — no pinning in this smoke test.

DESIGN DIRECTION (your words):
• VAD start → spawn agent -p --output-format json --trust --yolo --resume <chatId>
• Startup lag hidden behind STT transcription time
• STT done → write transcript to stdin → stdin.end()
• Agent runs, exits, emits JSON
• Parse .result → TTS
• Capture session_id → pass as --resume next spawn

Smoke-test simulation:
• Typing the first character/line = VAD trigger → immediate pre-spawn
• Double newline = "STT finished" → feed full prompt to the already-running
  process and close stdin
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

class CursorAgentSmokeTest:
    def __init__(self):
        if not WORKSPACE.is_dir():
            raise ValueError(f"Workspace not found: {WORKSPACE}")
        self.chat_id: Optional[str] = None
        print(f"✅ VAD-style pre-spawn smoke test ready — workspace: {WORKSPACE}")
        print(f"   Model: {DEFAULT_MODEL}")
        print("   Strategy: TRUE pre-spawn on first keystroke → feed stdin only on turn end")
        print("   (This hides spawn latency behind simulated VAD/STT time)\n")
        print("   SCHEDULING NOTE: Agent child will naturally land on cores 2/3 when")
        print("   master is on core 4 and recorder is pinned to core 1. Pinning can")
        print("   be added later via taskset or os.sched_setaffinity.\n")

    def _pre_spawn_agent(self) -> subprocess.Popen:
        """Spawn the agent process the moment VAD would fire (first line typed)."""
        cmd = [
            str(AGENT_BIN),
            "-p",
            "--output-format", "json",
            "--force",
            "--yolo",
            "--trust",
            "--workspace", str(WORKSPACE),
            "--model", DEFAULT_MODEL,
        ]

        if self.chat_id:
            cmd.extend(["--resume", self.chat_id])
            print(f"→ Pre-spawning agent (resuming chat {self.chat_id[:8]}...)")
        else:
            print("→ Pre-spawning agent (fresh chat)")

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
        """Feed the completed prompt to the already-running agent."""
        print(f"→ Feeding full turn to already-running agent...")

        process.stdin.write(user_prompt + "\n")
        process.stdin.close()          # signals end-of-input → agent starts working

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
                    if "result" in data and isinstance(data["result"], str):
                        full_text = data["result"]
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

        print("\n=== FULL AGENT REPLY ===\n")
        print(full_text.strip())
        print("\n" + "="*80 + "\n")

    def run(self):
        """Main loop — pre-spawn triggers on first line of input."""
        print("VAD-style pre-spawn loop started.")
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

                # === VAD MAGIC: spawn the instant the user starts typing ===
                if len(lines) == 1 and line.strip():   # first non-empty line
                    agent_process = self._pre_spawn_agent()

                if line == "" and len(lines) > 1:      # blank line after content = send
                    break
                if line == "" and len(lines) == 1:     # completely empty turn = quit
                    print("\n👋 Ending smoke test.")
                    return

            user_prompt = "\n".join(lines[:-1]).strip()   # drop the final blank line
            if not user_prompt or not agent_process:
                continue

            self._run_turn(user_prompt, agent_process)


# ===================================================================
# ENTRYPOINT
# ===================================================================
if __name__ == "__main__":
    try:
        test = CursorAgentSmokeTest()
        test.run()
    except KeyboardInterrupt:
        print("\n\n👋 Smoke test interrupted.")
    except Exception as e:
        print(f"\n💥 Error: {e}")
    finally:
        print("Smoke test finished. This is now the reference for the real voice orchestrator.")
