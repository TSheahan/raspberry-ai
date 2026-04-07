# Provenance: wired-master-state-design

**Session date:** 2026-04-07

---

## Summary

- Analyzed the architectural tension in `master_loop` (`voice_assistant.py`): conditionals over pipe messages feel stateful and could belong in `MasterState`, but the current separation keeps workers out of the state object, preserving testability.
- Compared with the child-side `RecorderState`, which takes the opposite approach — holding weakrefs to workers and executing side-effect sequences directly in `set_phase()`. Identified that the two state objects represent two valid answers to the same architectural question, driven by different side-effect characteristics (deterministic hardware resets vs. policy-driven service invocations).
- Arrived at a transcending design: keep `MasterState` as a pure testable base class; create a `WiredMasterState` subclass in a new module that absorbs the orchestration logic and worker access, collapsing `master_loop` to a thin dispatcher. This preserves base-class testability while restoring protocol cohesion in the subclass.
- Ran a six-item gap analysis to close open decisions. Key decisions: subclass in a separate module (`master_state_wired.py`), post-construction `set_*` wiring (RecorderState pattern), all strong refs (no cycle risk today), subclass owns worker lifecycle via `close()`, helpers migrate with subclass, `master_loop` retains READY handshake / SHUTDOWN handling / loop dispatch.
- Produced a detailed implementation plan for a fresh session to execute from.

---

## Transcript

**USER:**
take a look at master_loop in @assistant/voice_assistant.py .
We've just broken out some more stateful logic into `assistant/master_state.py`.

Now I'm observing all those conditionals over msg and cmd.

Present an analysis - are we looking at an implementation tension here?

On the one hand, those conditionals feel extremely state-ish. Loks they could belong in the state object.

On the other hand, I understand that the current arrangement expresses a separation of concerns, where the state object does not need to directly reference worker objects.

Dig in on that, show me what you find.

**ASSISTANT:**
Read `voice_assistant.py`, `master_state.py`, `phase_protocol.py`, and `master_state_spec.md`. Produced a detailed analysis identifying three specific tension points: (1) the "bookkeeping tax" where the loop must call annotation methods like `note_agent_prepare()` after each side effect, (2) computed properties like `stt_arm_ready` that are decisions dressed as queries — the state already knows what should happen but expresses it indirectly, and (3) protocol knowledge split across two files so that understanding any single event handler requires reading both. Concluded the arrangement is at a local optimum for testability and explicitness, with the bookkeeping method count as the canary for when the balance tips.

**USER:**
Interesting. We can surface one more layer here.

Look at child (recorder process) state object `RingBackedRecorderState` in `assistant/recorder_process.py`. See it expressing a different philosophical approach (or design trajectory with baked-in partial decisions), where it was deemed useful to track weak refs to worker objects (in parent class RecorderState). Testability didn't emerge the same way through that design sequence, I think.

Run a similar analysis on the shild state object as was just done for master state, then discuss the delta.

**ASSISTANT:**
Read `recorder_process.py` and `recorder_state.py`. Analyzed `RecorderState` as "the state object that acts" — `set_phase()` validates the transition and executes the full side-effect sequence (drain OWW predict, reset Silero LSTM, start/stop PyAudio stream), reaching deep into worker internals via weakrefs (`oww.model.preprocessor.raw_data_buffer`, `vad._vad_analyzer._model.reset_states()`, `transport._in_stream`). Identified what it gained (protocol cohesion, sequence enforcement, thin callers — `command_listener` is a trivial one-liner-per-command dispatcher) and what it lost (testability without mocks, encapsulation via `hasattr` chains three layers deep, coupling to OWW/Silero/PyAudio internals). Presented the delta as two local optima for different constraint profiles: child-side effects are deterministic hardware resets with no branching decisions, while master-side effects are policy-driven service invocations with the 20-second `cognitive_loop` blocking call. The development trajectory went RecorderState first (workers baked in) then MasterState later (deliberate separation informed by the child's experience).

**USER:**
What if there is a transcending solution here, and MasterState is the development hook through which it could be expressed?
- let MasterState continue to exist as a pure testable state object
- subclass MasterState and adopt the additional patterns from RecorderState in the subclass

**ASSISTANT:**
Traced the concrete shape of a `WiredMasterState` subclass: each `on_*` method overrides with `super()` for the guard logic, then runs the side-effect sequence. Showed `master_loop` collapsing to a thin dispatcher structurally identical to `command_listener`. Identified what it resolves (bookkeeping tax vanishes, protocol cohesion restored, computed-property-as-decision resolved), what it preserves (base class testability intact), and the retroactive symmetry it creates — both sides of the pipe could share the same two-layer architecture (pure base, wired subclass). Assessed the `cognitive_loop`-inside-a-state-method question as acceptable (same pattern the child uses, caller still explicitly calls the method knowing it does work). Noted the pattern is sound and `MasterState` as it exists is the natural hook to express it through.

**USER:**
It's useful to reflect how we got here. When it became clear that the co-mingled state processing in @assistant/voice_assistant.py  was threatening to decohere or become unmaintainable, I drove a design process which rigidly placed the inter-process state protocol at the root of the design process. This effort led to the clean testable MasterState class, and we see it driving design choices that seemed to have the effect of evicting certain logics from the state class. A subclass design may validly express different design choices, suiting its nature and purpose.

I agree with your analysis. This is the correct design direction, and although the script does work in its current form, I judge the aesthetic, design and maintainability aspects to be more than worthwhile. Back-port to the recorder child can be tracked as an option after the master process is fully squared away.

**Work product of this session will be a design/plan.**

A fresh session can get needed context from the plan and execute the creation of the new subclass. worker access might follow the weakref pattern from recorder, particularly when a worker might not endure for the lifetime of the master process, or if cross-ref issues arise.

give me a gap analysis - how is our conceptual coverage, and decision coverage, needed to write the plan?

**ASSISTANT:**
Produced a gap analysis. Identified six decisions needed before the plan: (1) file placement for the subclass, (2) complete method override inventory, (3) where helper functions/classes land, (4) worker wiring strategy (constructor vs `set_*`), (5) worker lifecycle ownership, (6) what stays in `master_loop`. Assessed most as having natural answers needing confirmation, not deep analysis. Presented a table with leanings and discussion-needed flags.

**USER:**
Desisions response:
1. query: why not a separate module for the state subclass? we might even see many of those imports evaporate from @assistant/master_state.py  afterwards.
2. open question bullet responses:
  - log may stay in the loop I think
  - maybe a suitable way to express this is to distinguish abstract (or overridden) methods in the plan
  - we should break out the subclass and retain existing function. extension paths may be annotated and left as an exercise for the future.
3. if helpers can move to broken-out state subclass because @assistant/voice_assistant.py  cleanly no longer needs them, then they should move. Method/subclass may migrate in their current form, with annotation when they are eligible for cleaner expression later.
4. constructor injection is one thing, but then the class needs constructor args too. I feel leery of this.
  - adopt the post-construction pattern
  - there should be annotation that this is a refactor candidate later
  - implementation must include disposition of the weakref question per worker.
5. let subclass own lifecycle.
6. let those stay. if the master process has nothing left but tumbleweeds, we probably overcooked this.

I didn't fully address item 1, and it is fundamental. engage me to discuss and close that out first.

**ASSISTANT:**
Analyzed file placement. Traced the import graph showing that `master_state.py` already has zero runtime imports (just `phase_protocol`, stdlib, loguru) and wouldn't change either way. The file that sheds imports is `voice_assistant.py` — `threading`, `EventType`, `AgentSession`, `AgentError`, `CHANNELS`, `SAMPLE_RATE` all serve helpers that would move. Noted that worker construction imports (`DeepgramClient`, `CursorAgentSession`, TTS backends, `AudioShmRingReader`) stay in `voice_assistant.py` because construction stays in `master_loop`. Presented the three-module separation as: `master_state.py` (pure base, no runtime imports), new subclass module (state + workers + helpers), `voice_assistant.py` (config, construction, thin loop, shutdown, main).

**USER:**
Solved, and I see increasing alignment. resolve 1 with the decision to build the subclass in a separate module.

Let's prepare the plan

**ASSISTANT:**
Ran final import-graph verification via grep. Created the implementation plan via the CreatePlan tool, covering: architecture diagram, new module contents and class design (overrides, new methods, wiring, ref strength per worker), changes to `voice_assistant.py` (removed code, import changes, thin dispatcher shape), no changes to `master_state.py`, documentation updates, annotated extension paths (constructor injection, weakref conversion, method promotion, RecorderState back-port), and testing strategy.

**USER:**
do we have a provenance command?

**ASSISTANT:**
Looked up the current session's transcript UUID (`68e82ad0-52e3-48b2-8cec-6dcdb5df330a`). Offered to add a provenance link to the plan.

**USER:**
no, there's missing context. in some projects I defined a rule or skill or slash command with specific protocol for retention of provenance.
never mind, this time we'll just one-shot it. read the spec here: `C:\Users\tim\PycharmProjects\tims-agentic-customizations\sync-back\aiops-agent-memory\tools\provenance\spec.md`. do a capture of provenance for this session please. suitable destination: `mvp-modules/forked_assistant/archive/`

**ASSISTANT:**
Read the provenance spec. Retrieved the timestamp. Read the plan file for the companion. Produced both files.
