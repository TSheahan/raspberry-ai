# `assistant/` — Agent Context

Runtime Python for the two-process voice assistant (repo root). Imported by `voice_assistant.py` (cognitive path), `recorder_process.py` (Pipecat pipeline), and tests via `sys.path` to this directory.

Parent context: [mvp-modules/forked_assistant/AGENTS.md](../mvp-modules/forked_assistant/AGENTS.md).

## File index

| File | Role |
|------|------|
| [voice_assistant.py](voice_assistant.py) | Master process: pipe IPC with recorder child, Deepgram live STT tail, `CursorAgentSession` + `TTSBackend`, wake/capture/idle state machine. |
| [agent_session.py](agent_session.py) | `AgentSession` ABC + `CursorAgentSession`: Cursor CLI subprocess, stream-json parsing, sentence-boundary streaming, session resume window. |
| [tts_backends.py](tts_backends.py) | `TTSBackend` + cloud/local implementations (`DeepgramTTS`, `ElevenLabsTTS`, `CartesiaTTS`, `PiperTTS` stub), `_AudioOut` (pyalsaaudio / PyAudio). |
| [recorder_process.py](recorder_process.py) | Recorder subprocess entry: Pipecat pipeline, ring buffer writer, shutdown protocol toward master. |
| [recorder_state.py](recorder_state.py) | `RecorderState` base: phase hooks for wake/capture/idle (shared with harnesses). |
| [phase_protocol.py](phase_protocol.py) | Shared phase vocabulary + `classify_transition()` ([master_state_spec.md](../mvp-modules/forked_assistant/spec/master_state_spec.md) §5a); used by `RecorderState.set_phase()`. |
| [test_phase_protocol.py](test_phase_protocol.py) | Runnable checks: `python assistant/test_phase_protocol.py`. |
| [audio_shm_ring.py](audio_shm_ring.py) | SharedMemory ring layout, `AudioShmRingReader` / `AudioShmRingWriter` for audio spans. |
| [logging_setup.py](logging_setup.py) | Shared loguru setup for master and recorder. |

**Specs:** [mvp-modules/forked_assistant/spec/agent_session_spec.md](../mvp-modules/forked_assistant/spec/agent_session_spec.md), [mvp-modules/forked_assistant/spec/interface_spec.md](../mvp-modules/forked_assistant/spec/interface_spec.md) (shutdown §3), [master_state_spec.md](../mvp-modules/forked_assistant/spec/master_state_spec.md) (phase contract, §7c checkpoint after `phase_protocol` + `RecorderState`).
