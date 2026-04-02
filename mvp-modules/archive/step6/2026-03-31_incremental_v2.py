"""Incremental v2 — Minimal Pipecat framework wrapping, RTVI OFF.

Pipeline: LocalAudioTransport.input() only. No custom processors.
Tests whether PipelineRunner + LocalAudioTransport can exit cleanly on Ctrl+C.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

os.environ["ORT_LOG_LEVEL"] = "ERROR"

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams


async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,
            audio_out_enabled=False,
        )
    )

    pipeline = Pipeline([
        transport.input(),
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline, enable_rtvi=False)

    print("🎤 Incremental v2 (minimal Pipecat, RTVI OFF): Listening...")
    print("   No wake word or transcription — just testing Ctrl+C exit.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    print("✅ Incremental v2 finished cleanly — process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
