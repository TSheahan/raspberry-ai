"""
VAD-only harness: Pipecat LocalAudioTransport → Silero VAD → console.

Exercises the exact same Pipecat + Silero path as recorder_process.py but
without OWW, SHM ring, phase gating, or IPC. Every audio frame hits Silero
unconditionally, and all VAD state transitions + volume/confidence are printed.

Use this to isolate whether VAD fires on live mic input from the 2-mic HAT.

Run:
    cd ~/raspberry-ai
    source ~/venv/bin/activate
    python mvp-modules/vad-only/vad_harness.py

Ctrl+C to stop.
"""

import asyncio
import os
import signal
import sys
import time

import numpy as np

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import (
    LocalAudioTransport, LocalAudioTransportParams,
)
from pipecat.frames.frames import (
    Frame, AudioRawFrame, InputAudioRawFrame, StartFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.audio.vad.vad_controller import VADController
from pipecat.audio.utils import calculate_audio_volume

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "assistant"))
from frame_dump import FrameDumpProcessor, frame_dump_enabled
from input_quality import InputQualityProcessor, input_quality_enabled

INPUT_DEVICE = 1
SAMPLE_RATE = 16_000


class VADProbe(FrameProcessor):
    """Passes every audio frame through Silero and logs results."""

    def __init__(self, vad_analyzer: SileroVADAnalyzer):
        super().__init__()
        self._analyzer = vad_analyzer
        self._controller = VADController(vad_analyzer)
        self._frame_count = 0
        self._speech_frames = 0
        self._started = False
        self._t0 = time.monotonic()

        @self._controller.event_handler("on_speech_started")
        async def on_speech_started(_ctrl):
            elapsed = time.monotonic() - self._t0
            print(f"\n>>> SPEECH_STARTED  at {elapsed:.1f}s  (frame {self._frame_count})")

        @self._controller.event_handler("on_speech_stopped")
        async def on_speech_stopped(_ctrl):
            elapsed = time.monotonic() - self._t0
            print(f"<<< SPEECH_STOPPED  at {elapsed:.1f}s  (frame {self._frame_count}, "
                  f"speech_frames={self._speech_frames})")
            self._speech_frames = 0

        @self._controller.event_handler("on_speech_activity")
        async def on_speech_activity(_ctrl):
            self._speech_frames += 1

        @self._controller.event_handler("on_push_frame")
        async def on_push_frame(_ctrl, frame, direction):
            await self.push_frame(frame, direction)

        @self._controller.event_handler("on_broadcast_frame")
        async def on_broadcast_frame(_ctrl, frame_cls, **kw):
            pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._controller.process_frame(frame)
            self._started = True
            print(f"[harness] StartFrame received, sample_rate={frame.audio_in_sample_rate}")

        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self._frame_count += 1

            if not self._started:
                await self.push_frame(frame, direction)
                return

            audio = frame.audio
            sr = getattr(frame, 'sample_rate', SAMPLE_RATE)
            ch = getattr(frame, 'num_channels', 1)
            n_samples = len(audio) // 2

            volume = calculate_audio_volume(audio, sr)

            audio_np = np.frombuffer(audio, dtype=np.int16)
            rms = float(np.sqrt(np.mean(audio_np.astype(np.float64) ** 2)))

            confidence = self._analyzer.voice_confidence(audio) if len(audio) == 1024 else -1.0

            if self._frame_count <= 5 or self._frame_count % 50 == 0:
                print(f"[frame {self._frame_count:>5}] samples={n_samples} ch={ch} sr={sr} "
                      f"rms={rms:7.1f}  vol={volume:.3f}  conf={confidence:.3f}")

            await self._controller.process_frame(frame)

        await self.push_frame(frame, direction)


async def main():
    print(f"[harness] VAD-only probe — device={INPUT_DEVICE}, rate={SAMPLE_RATE}")
    print(f"[harness] Speak to test. Ctrl+C to stop.\n")

    dump = frame_dump_enabled()
    if dump:
        print("[harness] PIPELINE_FRAME_DUMP=1 — PCM capture enabled")

    analyzer = SileroVADAnalyzer(
        params=VADParams(stop_secs=1.8, start_secs=0.2, min_volume=0.0),
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=INPUT_DEVICE,
            audio_in_channels=1,
            audio_out_enabled=False,
        )
    )

    probe = VADProbe(analyzer)

    processors = [transport.input()]
    if dump:
        processors.append(FrameDumpProcessor(prefix="harness_dump"))
    if input_quality_enabled():
        processors.append(InputQualityProcessor())
    processors.append(probe)

    pipeline = Pipeline(processors)
    runner = PipelineRunner()
    task = PipelineTask(pipeline, idle_timeout_secs=None)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal():
        print("\n[harness] shutting down...")
        stop.set()
        asyncio.create_task(task.cancel())

    loop.add_signal_handler(signal.SIGINT, _on_signal)
    loop.add_signal_handler(signal.SIGTERM, _on_signal)

    await runner.run(task)

    print(f"[harness] done — {probe._frame_count} frames processed")


if __name__ == "__main__":
    asyncio.run(main())
