# `assistant/` — Agent Context

Runtime Python for the two-process voice assistant (repo root). Imported by `voice_assistant.py` (cognitive path), `recorder_process.py` (Pipecat pipeline), and tests via `sys.path` to this directory.

Parent context: [mvp-modules/forked_assistant/AGENTS.md](../mvp-modules/forked_assistant/AGENTS.md).

## File index

| File | Role |
|------|------|
| [voice_assistant.py](voice_assistant.py) | Master process entry: SharedMemory/Pipe setup, spawns recorder child, `master_loop` thin pipe dispatcher + shutdown + `main()`. Orchestration lives in `WiredMasterState`. |
| [agent_session.py](agent_session.py) | `AgentSession` ABC + `CursorAgentSession`: Cursor CLI subprocess, stream-json parsing, sentence-boundary streaming, session resume window. |
| [tts_backends.py](tts_backends.py) | `TTSBackend` + cloud/local implementations (`DeepgramTTS`, `ElevenLabsTTS`, `CartesiaTTS`, `PiperTTS` stub), `_AudioOut` (pyalsaaudio / PyAudio). |
| [recorder_process.py](recorder_process.py) | Recorder subprocess entry: Pipecat pipeline, ring buffer writer, shutdown protocol toward master. |
| [recorder_state.py](recorder_state.py) | Contract-first `RecorderState`: authoritative phase, counters, `gate_phase_transition()` / `commit_phase()` (child side of `phase_protocol`). |
| [recorder_state_wired.py](recorder_state_wired.py) | `WiredRecorderState`: pipe, SHM ring writer, `set_phase` worker orchestration, `write_audio` / `signal_*`. |
| [phase_protocol.py](phase_protocol.py) | Shared phase vocabulary + `classify_transition()` ([master_state_spec.md](../mvp-modules/forked_assistant/spec/master_state_spec.md) §5a); used by `RecorderState` and `MasterState`. |
| [master_state.py](master_state.py) | Pure base `MasterState`: belief from `STATE_CHANGED`, VAD gating, capture teardown on phase exits ([master_state_spec.md](../mvp-modules/forked_assistant/spec/master_state_spec.md) §4). Subclass: `WiredMasterState` in `master_state_wired.py`. |
| [master_state_wired.py](master_state_wired.py) | `WiredMasterState(MasterState)`: wires pipe, agent, TTS, ring reader, Deepgram; STT thread + `cognitive_loop` side effects. |
| [test_phase_protocol.py](test_phase_protocol.py) | Runnable checks: `python assistant/test_phase_protocol.py`. |
| [test_master_state.py](test_master_state.py) | Runnable checks: `python assistant/test_master_state.py`. |
| [audio_shm_ring.py](audio_shm_ring.py) | SharedMemory ring layout, `AudioShmRingReader` / `AudioShmRingWriter` for audio spans. |
| [logging_setup.py](logging_setup.py) | Shared loguru setup for master and recorder. |

**Specs:** [mvp-modules/forked_assistant/spec/agent_session_spec.md](../mvp-modules/forked_assistant/spec/agent_session_spec.md), [mvp-modules/forked_assistant/spec/interface_spec.md](../mvp-modules/forked_assistant/spec/interface_spec.md) (shutdown §3), [master_state_spec.md](../mvp-modules/forked_assistant/spec/master_state_spec.md) (phase contract, §7c checkpoint after `phase_protocol` + `RecorderState`).
