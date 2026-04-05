# forked_assistant `src/` — Agent Context

Runtime library for the two-process voice assistant. Imported by `master.py` (cognitive path), `recorder_child.py` (Pipecat pipeline), and tests via `sys.path` to this directory.

Parent context: [../AGENTS.md](../AGENTS.md).

## File index

| File | Role |
|------|------|
| [master.py](master.py) | Master process: pipe IPC with recorder child, Deepgram live STT tail, `CursorAgentSession` + `TTSBackend`, wake/capture/idle state machine. |
| [agent_session.py](agent_session.py) | `AgentSession` ABC + `CursorAgentSession`: Cursor CLI subprocess, stream-json parsing, sentence-boundary streaming, session resume window. |
| [tts.py](tts.py) | `TTSBackend` + cloud/local implementations (`DeepgramTTS`, `ElevenLabsTTS`, `CartesiaTTS`, `PiperTTS` stub), `_AudioOut` (pyalsaaudio / PyAudio). |
| [recorder_child.py](recorder_child.py) | Recorder subprocess entry: Pipecat pipeline, ring buffer writer, shutdown protocol toward master. |
| [recorder_state.py](recorder_state.py) | `RecorderState` base: phase hooks for wake/capture/idle (shared with harnesses). |
| [ring_buffer.py](ring_buffer.py) | SharedMemory ring layout, `RingBufferReader` / writer side for audio spans. |
| [log_config.py](log_config.py) | Shared loguru setup for master and recorder. |

**Specs:** [../spec/agent_session_spec.md](../spec/agent_session_spec.md), [../spec/interface_spec.md](../spec/interface_spec.md) (shutdown §3).
