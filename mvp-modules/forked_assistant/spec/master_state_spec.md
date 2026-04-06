# Master-side state object — design specification

**Date:** 2026-04-06
**Status:** Design brief — **Phase A (inter-process contract) signed off 2026-04-06** (§5a + §5d); Phase B (intra-process) ready to finalize. Not yet implemented.
**Parent:** [interface_spec.md](./interface_spec.md), [recorder_state_spec.md](./recorder_state_spec.md)
**Implements in:** `assistant/voice_assistant.py` (new class, replaces scattered locals in `master_loop`)

---

## 0. Foundational model and design sequencing

### 0a. State as an inter-process contract

State tracking is not “a master refactor” with a child implementation detail on the side. It is **foundationally a state contract between the recorder child and the master process**: the same phase vocabulary, cycle order, transition rules, and validity of events/commands apply to both ends of the pipe. Each side embodies that contract in a **state object** — `RecorderState` on the child (authoritative phase, hardware side-effects) and `MasterState` on the master (believed phase, software side-effects). The objects are asymmetric in *who drives transitions* and *what side-effects run*, but they must agree on the **shared rules** or the system drifts into the stale-echo and ordering bugs described in §2.

### 0b. Strict sequencing (gated progression)

Design work is **strictly sequenced**:

| Phase | Scope | Gate |
|-------|--------|------|
| **A — Inter-process contract** | Phase names, cycle order, transition classification (`forward` / `cycle_reset` / `stale` / `noop`), which events are legal in which phase, command ↔ confirmation mapping, shutdown sub-protocol. Expressed in spec + ideally one shared code module (`phase_protocol.py` or equivalent). **No** commitment to Python method names on either class beyond what is needed to name the contract. | **Complete — signed off 2026-04-06** (§5a + §5d accepted as the contract boundary; §5b–§5c remain supporting material). |
| **B — Intra-process interfaces** | `MasterState.on_*` dispositions, `RecorderState` hook shapes, fast-forward side-effect ordering, resource fields on the master object, migration steps in `master_loop` / `set_phase()`. | **Open** — may now be finalized and implemented against §5. |

**Rule:** §4 (master message API), §7c (migration wiring), and process-local hook lists may be treated as **authoritative design targets** for Phase B. The **contract in §5** (especially §5a, §5d) remains the foundation; if it changes, Phase B must be revisited.

### 0c. What “fully defined” means for Phase A

Phase A is done when all of the following are settled (see §5 for the body of the contract). **Satisfied 2026-04-06** (sign-off on §5a + §5d as the normative contract; §5b–§5c are explanatory).

- Legal **phase vocabulary** and **cycle order** (including `dormant` vs the repeating `wake_listen` → `capture` → `idle` loop).
- **Transition rules** identical on both sides (same classifier semantics).
- **Event validity by phase** (`WAKE_DETECTED`, `VAD_*` constraints).
- **Command–confirmation pairs** (and shutdown sequence) aligned with [interface_spec.md](./interface_spec.md).
- Agreement on **how the shared rules are enforced in code** (minimum: shared module with `classify_transition()`; optional heavier patterns deferred).

Phase A cleared **2026-04-06**; §4 and §7 are no longer provisional on account of the inter-process gate.

---

## 1. Problem statement

The master process in `voice_assistant.py` manages the other half of the IPC state machine, but does so with **ad-hoc local variables** scattered across the `master_loop` function body: `processing` (bool), `wake_pos` (int), `capture` (optional session object), plus implicit expectations about message ordering baked into the if/elif chain. There is no central object, no transition validation, and no mechanism to detect or recover from messages that arrive out of the expected sequence.

The child side already solved this with `RecorderState` — a central object that owns the phase, enforces transition ordering, and fires side-effects in a defined sequence. The master side needs an equivalent.

---

## 2. Observations from production (exec-log 2026-04-06_3)

A single wake-to-idle cycle was traced through the log. The pipeline is: wake word → agent pre-spawn + STT capture → VAD stop → Deepgram finalize → cognitive loop (agent + TTS) → return to wake_listen. The following sequencing issues were observed.

### 2a. Stale state echoes after cognitive_loop

The master sends `SET_IDLE` and `SET_WAKE_LISTEN` in quick succession, then blocks in `cognitive_loop` for ~20 seconds. The child processes both commands within milliseconds and sends back two `STATE_CHANGED` messages. When the master returns from `cognitive_loop` and re-enters the `pipe.recv()` loop, it reads these stale confirmations out of context:

```
19:29:05.893  cognitive loop latency: 20.52s
19:29:05.895  listening for wake word...
19:29:05.896  [master] state -> idle         ← stale: master already past idle
19:29:05.909  [master] state -> wake_listen  ← stale: master already sent this
```

The master logs `state -> idle` after it has already sent `SET_WAKE_LISTEN` and logged "listening for wake word." The child-side sequencing is correct — the master just processes the confirmations late and doesn't know they're stale.

### 2b. No validation of STATE_CHANGED content

The master currently logs `STATE_CHANGED` messages and does nothing else with them. It has no record of what phase it believes the child is in, so it cannot detect:

- A state that arrives out of the expected order
- A state that was skipped (fast-forward)
- A state echo that is behind the master's current belief (stale)

### 2c. VAD events assumed 1:1

The master treats `VAD_STOPPED` as the definitive "capture is done" signal and immediately tears down the STT session and enters the cognitive loop. There is no tracking of whether `VAD_STARTED` preceded it, and no accommodation for multiple start/stop cycles within a single capture phase (which would occur in dictation with pauses, or Silero retriggering on ambient noise between utterance segments).

### 2d. Async resource lifecycle tied to handler location

The STT capture session (`_SttCaptureSession`) is created in the `WAKE_DETECTED` handler and torn down in the `VAD_STOPPED` handler. The agent subprocess is pre-spawned in `WAKE_DETECTED` and consumed in `cognitive_loop`. The TTS warm-up is kicked off inside `cognitive_loop`. These resource lifecycles are coupled to specific message handlers rather than to state transitions, making it fragile to reorder or skip messages.

---

## 3. Jitter modes

The pipe is asynchronous. Messages from the child arrive when the child sends them, but the master reads them only when it calls `pipe.recv()`. This creates several categories of timing variation that the master must handle.

### 3a. Blocking-gap jitter

The master blocks in `cognitive_loop` for seconds to tens of seconds. During this window the child continues operating — it processes `SET_IDLE` and `SET_WAKE_LISTEN`, and may even detect another wake word. When the master unblocks, it finds a backlog of messages that were valid when sent but may be stale relative to the master's current intent.

**Example from log:** `STATE_CHANGED(idle)` arrives after the master has already sent `SET_WAKE_LISTEN` and considers itself past idle.

### 3b. Command–confirmation race

The master sends a command (e.g. `SET_CAPTURE`) and immediately continues processing messages. The child's `STATE_CHANGED(capture)` confirmation may arrive after intervening messages (e.g. a `WAKE_DETECTED` that was already in the pipe before the child processed `SET_CAPTURE`). The master must not assume the next message after sending a command will be the confirmation.

### 3c. Multi-fire VAD

Silero VAD can produce multiple `VAD_STARTED`/`VAD_STOPPED` pairs within a single capture phase. The stop_secs parameter (currently 1.8s) controls how long silence must persist before `VAD_STOPPED` fires, but shorter pauses within an utterance can still produce a stop followed by a restart. A future dictation mode would intentionally allow this.

### 3d. Spurious or duplicate messages

The child's `signal_state_changed()` fires on every `set_phase()` call, including no-op transitions (same phase → same phase). These produce `STATE_CHANGED` messages where the reported state matches the master's existing belief. Harmless but noisy — the state object should recognize and discard them.

---

## 4. Design requirements (Phase B — intra-process)

**Dependency:** Finalize §5 (inter-process contract) and obtain explicit agreement before locking the APIs and field lists below. Until then, use this section for alignment and test planning only.

### 4a. Central state object

A single `MasterState` object (or similar) must own all mutable state that currently lives as locals in `master_loop`:

| Current location | Variable | Moves to |
|---|---|---|
| `master_loop` local | `processing` (bool) | `MasterState.processing` |
| `master_loop` local | `wake_pos` (int) | `MasterState.wake_pos` |
| `master_loop` local | `capture` (_SttCaptureSession \| None) | `MasterState.capture` |
| Implicit in if/elif | believed child phase | `MasterState.phase` |
| Not tracked | VAD speaking flag | `MasterState.vad_speaking` |

### 4b. Phase model with ordered transitions

The child's phase cycle is:

```
dormant → wake_listen → capture → idle → wake_listen → capture → idle → …
```

`wake_listen` is the cycle boundary — arriving at `wake_listen` resets per-cycle state (VAD tracking, capture session, etc.).

The master must track its **believed phase** and apply incoming `STATE_CHANGED` messages against it:

1. **Forward progress:** new phase is ahead of current belief in the cycle order. Accept. Report any skipped intermediate phases so side-effects can fire for them.

2. **Cycle reset (wake_listen):** Always accept, from any phase. This is how the cycle wraps. If the current belief is not `idle`, the intervening phases were fast-forwarded.

3. **Stale echo:** new phase is behind current belief (e.g. `idle` arriving when master believes `wake_listen`). Discard silently or at debug level.

4. **No-op:** new phase matches current belief. Discard.

### 4c. Fast-forward with side-effects

When the master detects that a phase was skipped (e.g. it sees `wake_listen` while believing it's in `capture`, meaning `idle` was skipped), it must execute the **exit side-effects of the skipped phase(s)**. This is how resource cleanup happens even when confirmations arrive late:

| Skipped phase | Side-effects to execute |
|---|---|
| `capture` exit | Tear down STT capture session (stop_event, thread join) |
| `idle` exit | (none currently, but establishes the hook) |
| `wake_listen` exit | (none currently on master side) |

Entry side-effects of the *arrived* phase also fire:

| Entered phase | Side-effects |
|---|---|
| `wake_listen` | Reset VAD tracking, clear capture reference, set `processing = False` |
| `capture` | Reset VAD tracking (`vad_speaking = False`) |
| `idle` | (cognitive loop is triggered separately, not as a state-entry side-effect) |

### 4d. VAD event handling

`VAD_STARTED` and `VAD_STOPPED` are **events within the capture phase**, not state transitions. The state object must:

- Track `vad_speaking` (bool): set on `VAD_STARTED`, cleared on `VAD_STOPPED`.
- Reject `VAD_STOPPED` if `vad_speaking` is false (orphan stop without start).
- Reject both VAD events if the believed phase is not `capture`.
- Reset `vad_speaking` on phase transitions out of `capture`.

The master_loop's policy on *what to do* with VAD events (e.g. "first VAD_STOPPED triggers cognitive loop" vs. "accumulate multiple segments") remains in the event loop, not in the state object. The state object provides the validated tracking; the loop provides the policy.

### 4e. Message acceptance API

The state object should expose a method for each incoming message type that returns a disposition:

```python
class MasterState:
    def on_state_changed(self, new_phase: str) -> StateChangeResult:
        """Returns accepted phase, skipped phases, or stale/no-op indicator."""

    def on_wake_detected(self, write_pos: int, score: float, keyword: str) -> bool:
        """Returns False if wake should be ignored (processing, wrong phase)."""

    def on_vad_started(self, write_pos: int) -> bool:
        """Returns False if not in capture or already speaking."""

    def on_vad_stopped(self, write_pos: int) -> bool:
        """Returns False if not speaking or not in capture."""
```

The event loop reads the pipe, calls the appropriate method, and acts on the result. The state object does not call `pipe.send()` — it advises; the loop acts.

### 4f. Resource lifecycle hooks

The state object owns references to resources whose lifetime is tied to phases:

- `capture: _SttCaptureSession | None` — created on `WAKE_DETECTED`, torn down on capture exit.
- `agent: CursorAgentSession` — long-lived, but `prepare()` is called per-wake. The state object tracks whether a prepare has been issued for the current cycle.

Teardown of phase-bound resources happens in the side-effect methods (§4c), not in the event handlers directly.

---

## 5. Shared inter-process contract (Phase A — gate this first)

This section **is** the cross-process state contract. The two state objects sit on opposite ends of the same pipe; they depend on the same ground truth: the phase model and the event protocol. The contract is a strict subset of what either side does — phase names, cycle order, transition classification, event legality, command–confirmation pairing. No side-effects, no resource lifecycle, no hardware, no async. It is the simplest layer and the foundation everything else derives from: the child adds hardware side-effects on top of it, the master adds software side-effects and belief tracking on top of it, but neither layer can be correct if the foundation diverges.

**Phase A sign-off (2026-04-06):** §5a and §5d accepted as fully defining the inter-process state behavior for this effort. Proceed to finalize and implement §4, §6–§7 (Phase B) against §5.

Today the contract is implicit — it exists only in `interface_spec.md` as prose and in the coincidental agreement between `recorder_state.py`'s `set_phase()` and the if/elif chain in `master_loop`. If someone adds a phase, renames an event, or changes the transition order on one side, nothing forces the other side to notice.

### 5a. The shared contract (what both sides must agree on)

**Phase vocabulary.** The set of legal phase names:

```
dormant, wake_listen, capture, idle
```

Both objects must recognize exactly this set. An unknown phase is an error.

**Phase cycle order.** The progression within a single turn:

```
dormant → wake_listen → capture → idle
```

`wake_listen` wraps the cycle: after `idle`, the next phase is `wake_listen` again. `dormant` is an entry/exit state outside the normal cycle.

**Transition validity rules.** Given a current phase and a proposed next phase:

1. **Forward:** next phase is later in the cycle order → valid.
2. **Cycle reset:** next phase is `wake_listen` and current is `idle` (or any phase, for fast-forward) → valid.
3. **Backward/stale:** next phase is earlier in the cycle order and is not `wake_listen` → invalid (stale echo or error).
4. **No-op:** next phase equals current phase → valid but no side-effects fire.

These four rules are identical on both sides. They should be defined once.

**Event types within a phase.** `VAD_STARTED` and `VAD_STOPPED` are valid only during `capture`. `WAKE_DETECTED` is valid only during `wake_listen`. Both sides need to agree on these constraints even though only the child emits the events and only the master receives them — the child gates emission by phase, the master validates reception by believed phase.

**Command–confirmation pairs.** Each master command maps to an expected child confirmation:

| Master sends | Child confirms with |
|---|---|
| `SET_WAKE_LISTEN` | `STATE_CHANGED(wake_listen)` |
| `SET_CAPTURE` | `STATE_CHANGED(capture)` |
| `SET_IDLE` | `STATE_CHANGED(idle)` |
| `SET_DORMANT` | `STATE_CHANGED(dormant)` |
| `SHUTDOWN` | `SHUTDOWN_COMMENCED` → `SHUTDOWN_FINISHED` |

**How to read this next to child-originated ground truth (wake, VAD).** The pipe carries two different kinds of child→master facts; only one of them is “commanded then confirmed”:

| Kind | Who decides | What it means |
|------|-------------|----------------|
| **Phase** (`STATE_CHANGED`) | Master **requests** a mode with `SET_*`; child **implements** it (stream layout, OWW vs Silero, etc.) and **reports** the phase it has actually entered. | The child is authoritative on *what phase it is in right now*. `STATE_CHANGED` is not a prediction — it is the child’s assertion that its recorder state machine has committed to that phase after handling the command (or an equivalent internal transition that still results in that phase). |
| **Events** (`WAKE_DETECTED`, `VAD_*`) | Only the child **observes** the mic + models. The master never commands “detect wake now.” | These are **telemetry**: “this happened while I was in the phase I was in.” They are ground truth for *detection*, not substitutes for `STATE_CHANGED`. |

So **§5a’s transition rules apply to phase names** — comparing previous phase to next phase when processing `STATE_CHANGED` (and when the child validates `set_phase`). They do **not** describe “wake word transitions the system from wake_listen to capture.” The wake word does **not** carry a phase on the wire. The causal story in the normal turn is:

1. Child is in `wake_listen` (per last `STATE_CHANGED` and local `RecorderState`).
2. Child emits `WAKE_DETECTED` — ground truth that OWW fired at `write_pos`.
3. Master may apply policy (ignore if busy, etc.); if it accepts, it sends `SET_CAPTURE`.
4. Child enters `capture`, runs Silero, and sends `STATE_CHANGED(capture)` — ground truth of **mode**, not of wake.

The master’s **belief** about phase should track (2) vs (4) separately from **events**: you can receive `WAKE_DETECTED` while still believing the child is in `wake_listen` until `STATE_CHANGED(capture)` arrives — or, depending on timing, process a backlog where `STATE_CHANGED` and events are interleaved. The contract says **which events are legal in which phase** (child when sending, master when accepting); the command–confirmation table says **how phase changes are agreed** across the boundary.

#### Worked example: `wake_listen` → `capture` (phase alignment)

| Step | Child | Pipe | Master |
|------|--------|------|--------|
| 1 | In `wake_listen` (OWW on, ring writing). | → `WAKE_DETECTED` `{ write_pos, score, keyword }` | `on_wake_detected` / policy: accept if believed phase is `wake_listen` and not busy. |
| 2 | Still `wake_listen` until command handled. | ← `SET_CAPTURE` | Sends command; may start master-local STT / ring-tail (Phase B policy — not required for phase contract). |
| 3 | `set_phase(capture)` → Silero on, OWW off, etc. | → `STATE_CHANGED` `{ state: "capture" }` | Drains when ready; must not assume this is the next message after `SET_CAPTURE`. |
| 4 | In `capture`; may emit `VAD_*` only from here. | | `on_state_changed("capture")`: forward from `wake_listen` → belief `capture`. **Inter-process capture commenced** = child committed + master belief updated. |

Ring writes are already active before wake; wake supplies `write_pos`, not a separate “start ring” phase transition.

#### VAD series vs phase changes

`VAD_STARTED` / `VAD_STOPPED` carry **no phase**. While the child stays in `capture`, it may emit **many** VAD pairs (silence gaps, Silero retriggering, future multi-segment dictation). None of those messages change phase on their own.

**Phase** changes only when the master sends a `SET_*` and the child confirms with `STATE_CHANGED`. The master therefore decides **when** (and in a future design, **whether**) those VAD events lead to **sending** the commands that end `capture` (e.g. `SET_WAKE_LISTEN` after tearing down STT, or `SET_IDLE` first per pipeline policy). That decision logic is **master policy** (§5d), not part of the shared transition classifier — but the model explicitly allows “many VAD events, one eventual phase exit driven by master timing.”

### 5b. What differs between the two sides

While the contract above is shared, the two state objects are not symmetric:

| Aspect | RecorderState (child) | MasterState (master) |
|---|---|---|
| Relationship to truth | **Is** the phase — authoritative | **Believes** the phase — derived from confirmations |
| Transition trigger | Master command (`SET_*`) on pipe | Child confirmation (`STATE_CHANGED`) on pipe |
| Side-effects | Hardware: PyAudio stream, OWW buffers, Silero LSTM | Software: STT capture session, agent lifecycle, VAD tracking |
| Async model | `async def set_phase()` on asyncio event loop | Synchronous methods in blocking `master_loop` |
| Pipe direction | Receives commands, sends confirmations + events | Sends commands, receives confirmations + events |

The side-effect hooks have the same shape (entry/exit actions per phase) but completely different implementations. The phase logic (which transition is valid, which phases were skipped) is identical.

### 5c. Implications for implementation

The shared contract should be expressed in code, not just in documentation. Options, in order of increasing formality:

1. **Shared constants module.** A small module (e.g. `phase_protocol.py`) that defines `PHASES`, `PHASE_ORDER`, `PHASE_IDX`, and a pure function `classify_transition(current, proposed) → forward | cycle_reset | stale | noop`. Both `RecorderState` and `MasterState` import and use it. This is the minimum viable extraction — it prevents the transition rules from diverging without coupling the two objects.

2. **Shared base class with abstract hooks.** An ABC that owns the phase field, the `set_phase()` / `accept_phase()` transition logic, and declares abstract entry/exit hook methods. `RecorderState` and `MasterState` each subclass it and provide their process-local hooks. This is heavier than (1) but enforces the hook shape.

3. **Keep it documented, not coded.** Both objects implement the same rules independently, with this spec as the single source of truth. Acceptable only if the phase model is stable and unlikely to evolve.

The choice between (1) and (2) depends on whether the side-effect hook interface is stable enough to lock into an ABC. The phase vocabulary and transition rules are stable now — option (1) is safe to implement immediately. Option (2) can follow if the hook shape proves consistent across both sides after the master state object is built.

### 5d. What this contract is not

The shared contract does not dictate:

- **What side-effects fire.** Each side defines its own entry/exit actions. The contract only defines *when* they fire (on which transitions).
- **How the pipe is used.** The contract defines message shapes and expected sequences. It does not own the pipe — each side holds its own end.
- **Policy.** "What to do when VAD_STOPPED arrives" is master-side policy. "Whether to gate OWW during capture" is child-side policy. The contract only says which events are valid in which phases.

---

## 6. Scope and non-goals

### Phase A (inter-process) — signed off 2026-04-06

- **Shared phase protocol spec** — §5a + §5d are the agreed contract; §5b–§5c support implementation choices. Stays aligned with [interface_spec.md](./interface_spec.md) for wire messages.
- **Shared code module** (recommended) — `phase_protocol.py` (or similar): `PHASES`, cycle order, `classify_transition()` (§5c option 1). No requirement to wire it into both processes in the same commit as long as the **rules** are agreed.

### Phase B (intra-process) — in scope after Phase A sign-off

- **`phase_protocol.py` + `RecorderState` retrofit** — implemented 2026-04-06 (`classify_transition` gate in `set_phase()`). **User checkpoint at §7c** before `MasterState`.
- **Central `MasterState` class** in `voice_assistant.py` (or `master_state.py` if size warrants).
- Phase belief tracking with forward/stale/fast-forward logic; VAD validation; resource lifecycle hooks; replacement of scattered `master_loop` locals; logging of transitions, fast-forwards, stale rejections.
- Final `MasterState` message API (§4e) and migration path (§7c).

### Not in scope (future)

- Multi-segment dictation mode (multiple VAD_STARTED/STOPPED accumulating into one transcript). The state object should *not prevent* this, but the event loop policy for it is a separate design.
- Moving `cognitive_loop` invocation into a state-entry side-effect. The cognitive loop is a long blocking call that should remain explicit in the event loop, not hidden inside a state transition.
- Async master event loop. The master is currently synchronous (`pipe.recv()` blocks). Converting to async is orthogonal to the state object design.
- Shared base class with abstract hooks (§5c option 2). Deferred until the hook shape proves stable across both sides.

---

## 7. Implementation notes (Phase B)

### 7a. File placement

If the class is small (<150 lines), keep it in `voice_assistant.py` near the top (after imports, before `_SttCaptureSession`). If it grows, extract to `master_state.py` alongside `recorder_state.py`.

### 7b. Testing

The state object is pure logic (no I/O, no pipe access). It can be unit-tested with synthetic message sequences:

- Normal cycle: dormant → wake_listen → capture → idle → wake_listen
- Stale echo: send `idle` when already in `wake_listen` → rejected
- Fast-forward: send `wake_listen` when in `capture` → accepted, `idle` reported as skipped
- VAD without capture: send `VAD_STARTED` when in `wake_listen` → rejected
- Orphan VAD_STOPPED: send `VAD_STOPPED` without prior `VAD_STARTED` → rejected
- Duplicate state: send `capture` when already in `capture` → no-op

### 7c. Migration path

Steps 1–2 align with **Phase A** (contract in code + child parity). Steps 3–6 are **Phase B** (master object + loop wiring) and assume Phase A sign-off.

1. **Shared protocol.** Create `phase_protocol.py` with `PHASES`, `PHASE_CYCLE`, `classify_transition()`. Unit test the classifier with the synthetic sequences from §7b.
2. **Retrofit RecorderState.** Import the shared protocol into `recorder_state.py`. Gate `set_phase()` with `classify_transition()` (reject `STALE` and unknown phases). Verify no behaviour change on legal paths (harness / Pi smoke).

---

### User checkpoint — after steps 1–2

**Stop here for sign-off before MasterState (step 3).** Deliverables:

- [`assistant/phase_protocol.py`](../../assistant/phase_protocol.py) — contract constants + `classify_transition()`.
- [`assistant/recorder_state.py`](../../assistant/recorder_state.py) — uses the classifier; illegal transitions log and no-op without changing `_phase`.
- Quick verification: `python assistant/test_phase_protocol.py` from repo root (classifier always runs; `RecorderState` stale/noop checks run when `numpy` is available, e.g. Pi venv).

Optional: run `mvp-modules/forked_assistant/test/track2_pipeline_harness.py` or Pi `test_harness.py` if you want hardware confidence on legal command sequences.

---

3. **Introduce MasterState.** Build the class using the shared protocol for phase tracking and VAD validation. Unit test with the same synthetic sequences.
4. **Wire into master_loop.** Replace `processing`, `wake_pos`, `capture` locals with state object attributes. Wire `on_*` methods into the existing if/elif message handlers.
5. **Add fast-forward side-effects.** Capture session teardown on skipped `capture` exit. Verify with a forced fast-forward scenario (e.g. kill the cognitive loop mid-flight).
6. **Remove raw STATE_CHANGED handler.** All state messages go through the state object. The if/elif for `STATE_CHANGED` becomes a one-liner that delegates to `state.on_state_changed()`.

Each step is independently testable and deployable.
