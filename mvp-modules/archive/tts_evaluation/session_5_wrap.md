# TTS Evaluation — Session 5 Summary (2026-04-05)

## What happened

Recovered from crash mid-audition. Completed Cartesia voice audition (Katie / Allie /
Kayla shortlist), reviewed ElevenLabs voice selections, ran cross-platform speed
consistency checks, and established platform precedence order. All voice and speed
configuration is now locked and deposited.

## Cartesia voice audition

Crash occurred before any results were recorded. Resumed by fetching voice IDs from
the Cartesia API (`voices.list()`), then ran two rounds:

- Round 1: short sentence at 0.85 — all three voices
- Round 2: bagel monologue at 1.0 — all three; Kayla eliminated
- Round 3: bagel monologue at 0.85 — Katie vs Allie

**Selected: Allie** (Natural Conversationalist, `2747b6cf-fa34-460c-97db-267566918881`).
Friendly diction; naturally slower base cadence than Katie — more audio generated at
same speed setting, giving ellipses and rhythm more space on expressive long-form text.
Katie retained as runner-up.

## ElevenLabs voice model review

Re-ran shortlist (Matilda, Rachel, Jessica, Sarah) at Short speed for reference.
No changes — **Matilda confirmed**. Also ran Allie alongside for fallback-transition
feel check (Allie → Matilda → Rachel). Acceptable transition; no change to selection.

## Speed tier review

Tier nomenclature standardised: Short / Medium / Long (replaces Default/Bulk/Heavy bulk
and Default/Mid/Fallback mid). Fallback is now an inline annotation on a tier.

Cross-platform consistency verified by ear (Short + Medium + Long on both
Cartesia/Allie and ElevenLabs/Matilda):

| Tier | Cartesia | ElevenLabs | Consistent? |
|------|----------|------------|-------------|
| Short | 0.85 | 0.85 | Yes |
| Medium | 1.0 | 1.16 | Yes — Matilda reads as slower at equivalent values |
| Long | 1.2 | 1.2 | Yes — ElevenLabs Long corrected from "ceiling only" to confirmed tier |

Deepgram speed tiers carried from Thalia evaluation; not re-validated with Helena
(P3 — effort stops here).

## Platform precedence

| Priority | Backend | Voice | Role |
|----------|---------|-------|------|
| 1 | Cartesia | Allie | Primary |
| 2 | ElevenLabs | Matilda | Fallback |
| 3 | Deepgram | Helena | Tertiary |

Deepgram: REST-only latency and starting-click artifact make it unsuitable as primary.
Pronunciation control (inline IPA) is an available surface — annotated and deferred.

## Code changes

| File | Change |
|------|--------|
| `src/tts.py` | Module docstring: precedence order, usage example updated to `CartesiaTTS()`. `CartesiaTTS`: `_DEFAULT_VOICE_ID` = Allie, `_DEFAULT_SPEED` = 0.85. `ElevenLabsTTS`: `_DEFAULT_VOICE_ID` = Matilda. `DeepgramTTS`: `_DEFAULT_MODEL` = Helena, `_DEFAULT_SPEED` = 1.05. Section headers: PRIMARY / FALLBACK / TERTIARY. |
| `voice_tuning_results.md` | Platform precedence table. Actionable settings expanded with speed tiers. Speed tiers renamed Short/Medium/Long. Cartesia section header corrected to Allie. Deepgram pronunciation annotation added. |
| `voice_tuning_brief.md` | Voice IDs table expanded with all Cartesia shortlist entries and selection markers. |
| `AGENTS.md` | Voice audition phase marked complete. Final selections and precedence recorded. |
| `memory/architecture_decisions.md` | TTS Rearchitecture section updated: status, interface contract, platform table, file index. |

## What's next — Phase 3

1. In `master.py`: replace `PiperTTS(...)` with `CartesiaTTS()` (no args needed)
2. Call `tts.warm()` during a non-blocking window (STT phase or agent init)
3. Full cognitive turn on Pi: wake → STT → agent → TTS → wake_listen
4. Measure warm() vs no-warm() first-turn latency delta
5. Confirm: time-to-first-audio < 2s, no OOM, clean Ctrl+C, multi-turn
