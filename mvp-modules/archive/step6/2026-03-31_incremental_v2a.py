"""Incremental v2a — Minimal Pipecat framework wrapping, RTVI ON (default).

Pipeline: LocalAudioTransport.input() only. No custom processors.
Same as v2 but with enable_rtvi=True (Pipecat's default).
Tests whether RTVIProcessor blocks CancelFrame propagation.
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
    task = PipelineTask(pipeline, enable_rtvi=True)

    print("🎤 Incremental v2a (minimal Pipecat, RTVI ON): Listening...")
    print("   No wake word or transcription — just testing Ctrl+C exit with RTVI enabled.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    print("✅ Incremental v2a finished cleanly — process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
