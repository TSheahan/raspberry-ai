# Voice Tuning Results

## Platform Precedence

| Priority | Backend | Role | Rationale |
|----------|---------|------|-----------|
| 1 | Cartesia | Primary | Streaming; friendly natural diction (Allie); ~1.1s warm first-chunk; emotion control available |
| 2 | ElevenLabs | Fallback | Streaming; excellent latency (~350ms warm); Matilda handles expressive text well |
| 3 | Deepgram | Tertiary | REST-only; full audio fetched before playback begins; starting-click artifact unresolved |

---

## Actionable Settings

Production-ready values to be wired into each `TTSBackend` class.

| Backend | Parameter | Value |
|---------|-----------|-------|
| Cartesia | voice | Allie (`2747b6cf-fa34-460c-97db-267566918881`) |
| Cartesia | speed tiers | Short 0.85 / Medium 1.0 / Long 1.2 |
| ElevenLabs | voice | Matilda (`XrExE9yKIg1WjnnlVkGX`) |
| ElevenLabs | speed tiers | Short 0.85 / Medium 1.16 / Long 1.2 |
| Deepgram | model | `aura-2-helena-en` |
| Deepgram | speed tiers | Short 1.05 / Medium 1.2 / Long 1.4 |
| Deepgram | pronunciation | IPA inline notation available — annotated, deferred (P3) |

---

## Cartesia (Allie)

Selected voice ID: `2747b6cf-fa34-460c-97db-267566918881`
Baseline: speed 0.85 (established with Katie; confirmed with Allie)

### Trials

| # | Settings | Sentence | Latency | Feedback | Keep? |
|---|----------|----------|---------|----------|-------|
| 1 | speed=0.85 | "Hello, how can I help you today?" | 1446ms | Comfortable and ideal for shorter utterances | Yes — default |
| 2 | speed=1.0 | Full CPAP paragraph (4 sentences) | 1247ms | Feels middling — not as far as we can push; fine for bulk | Yes — bulk |
| 3 | speed=1.2 | Two CPAP paragraphs (8 sentences) | 1360ms | Outstanding for getting through bulk content | Yes — heavy bulk |

### Chosen settings

```
voice: Allie (2747b6cf-fa34-460c-97db-267566918881)
speed: adaptive (see speed tiers below)
emotion: neutral (default)
buffer_delay_ms: 3000 (default)
model: sonic-3 (default/latest as of 2026-04-05)
notes: speed is the primary tuning axis; emotion not yet explored
```

### Speed tiers

| Tier | Speed | Use case |
|------|-------|----------|
| Short | 0.85 | Single-sentence / short replies |
| Medium | 1.0 | Paragraph-length content |
| Long | 1.2 | Multi-paragraph / bulk readouts |

The agent should select a speed tier based on response length. This is a
production-useful insight: making speed controllable per-turn (not just a
static config) lets the agent feel conversational on short answers and
efficient on data-heavy ones.

### Voice model audition

Shortlist: Katie, Allie, Kayla — all at speed 0.85 (proven default cadence).

Round 1 — short sentence ("Hello, how can I help you today?") at speed 0.85: all three.
Round 2 — long expressive monologue (bagel quote) at speed 1.0: all three.
Round 3 — same monologue at speed 0.85: Katie vs Allie.

| Voice | Voice ID | Result |
|-------|----------|--------|
| Allie — Natural Conversationalist | `2747b6cf-fa34-460c-97db-267566918881` | **Selected** — Friendly diction; natural cadence on long-form; sits at a slower base pace than Katie (1080 KB vs 870 KB audio at same speed setting), giving ellipses and rhythm more space |
| Katie — Friendly Fixer | `f786b574-daa5-4673-aa0c-cbe3e8534c02` | Runner-up — proven cadence at 0.85; compact delivery |
| Kayla — Easygoing Pal | `1ac31ebd-9113-405b-9d80-4a4bbbeea91c` | Eliminated round 2 |

---

### Observations

- 0.85 was proven in session 4 as a fix for Katie's rushed cadence at 1.0.
  Re-confirmed here: comfortable default for short speech.
- 1.0 is not perceptibly fast on longer content — the paragraph gives the
  ear time to settle into the pace. Good middle gear.
- 1.2 is outstanding for heavy content — still fully intelligible, no
  "fast-forward narrator" feel. Viable ceiling for bulk readouts.
- Speed does not noticeably affect first-chunk latency (all ~1250–1450ms).
- Implication for production: `CartesiaTTS.play()` could accept a speed
  hint derived from chunk count or character length.
- **Emotion:** `CartesiaEmotion` enum added to `tts.py` as a reference surface
  (Happy, Calm, Angry, Sad, Curious, Surprised + NEUTRAL=None). Not wired to
  any deterministic call path — emotion selection belongs to the agent, not
  the script. To be continued.

---

## ElevenLabs (Rachel)

Voice ID: `21m00Tcm4TlvDq8ikWAM`
Baseline: defaults (no tuning yet)

### Trials

| # | Settings | Sentence | Latency | Feedback | Keep? |
|---|----------|----------|---------|----------|-------|
| 1 | speed=0.85 | Short sentence | 836ms | — | Yes — default |
| 2 | speed=1.0 | Paragraph | 477ms | Too cruisy for bulk | No |
| 3 | speed=1.2 | Two paragraphs | 457ms | — | Ceiling only |
| 4 | speed=0.85 | Short sentence | 357ms | — | Yes — default confirmed |
| 5 | speed=1.1 | Paragraph | 362ms | — | Candidate |
| 6 | speed=1.2 | Two paragraphs | 382ms | — | Explored |
| 7 | speed=1.12 | Two paragraphs | 361ms | — | Explored |
| 8 | speed=1.14 | Two paragraphs | 373ms | Solid increment down from sweet spot if needed | Fallback |
| 9 | speed=1.16 | Two paragraphs | 603ms | Sweet spot for mid/bulk | Yes |

### Chosen settings

```
speed: adaptive (see speed tiers below)
stability: server default
similarity_boost: server default
optimize_streaming_latency: server default
model: eleven_flash_v2_5 (default/lowest latency as of 2026-04-05)
notes: speed is the primary tuning axis; other knobs left at server defaults
```

### Speed tiers

| Tier | Speed | Use case |
|------|-------|----------|
| Short | 0.85 | Single-sentence / short replies |
| Medium | 1.16 (fallback: 1.14) | Paragraph-length content |
| Long | 1.2 | Multi-paragraph / bulk readouts |

### Observations

- ElevenLabs speed range is **0.7–1.2** in practice — API rejects values above 1.2
  (brief incorrectly stated 0.25–4; corrected).
- 1.0 feels too leisurely for bulk content — the comfortable bulk floor is higher
  than Cartesia's (1.16 vs 1.0) possibly because Rachel's voice character reads
  as more relaxed at equivalent speed values.
- 1.16 is the heavy-bulk ceiling. 1.14 is a reliable step down.
- Diction variation across sentences at the same speed setting (e.g. slower on
  short emotional sentences like "Overall, that's a solid night") is characteristic
  of this model — likely driven by sentence-length heuristics or inferred affect.
  Not a tuning problem; just a model behaviour to be aware of.
- First-chunk latency is excellent: ~350–600ms warm, consistently faster than
  Cartesia (~1300ms).

### Voice model audition

Round 1 — 5 candidates at speed 0.85 (short): Rachel, Jessica, Sarah, Bella, Matilda
Round 2 — 4 advancing at speed 1.16 (mid): Rachel, Jessica, Sarah, Matilda
Round 3 — final at speed 1.2 (long) + expressive monologue at 1.0: Rachel vs Matilda

| Voice | Voice ID | Result |
|-------|----------|--------|
| Matilda | `XrExE9yKIg1WjnnlVkGX` | **Selected** — Knowledgeable, Professional; handled expressive ellipsis-heavy text cleanly |
| Rachel | `21m00Tcm4TlvDq8ikWAM` | Runner-up — calm, warm default |
| Jessica | `cgSgspJ2msm6clMCkdW9` | Shortlist — Playful, Bright, Warm |
| Sarah | `EXAVITQu4vr4xnSDxMaL` | Shortlist — Mature, Reassuring, Confident |

---

## Deepgram (Thalia)

Voice ID: `aura-2-thalia-en`
Baseline: speed 0.9

### Trials

| # | Settings | Sentence | Latency | Feedback | Keep? |
|---|----------|----------|---------|----------|-------|
| 1 | speed=0.85 | Short sentence | 2148ms | Starting click overlaps first word | Platform defect noted |
| 2 | speed=1.0 | Paragraph | 7380ms | Starting click; 7s silence before playback | Platform defect noted |
| 3 | speed=1.2 | Two paragraphs | 12031ms | Starting click; 12s silence before playback | Platform defect noted |
| 4 | speed=1.1 | Paragraph | 7346ms | — | Explored |
| 5 | speed=1.2 | Paragraph | 6499ms | Faster but not manic — good for mid | Yes — mid |
| 6 | speed=1.4 | Two paragraphs | 10029ms | Right for long | Yes — long |
| 7 | speed=0.8 | Short sentence | 2223ms | — | Explored |
| 8 | speed=0.9 | Short sentence | 2093ms | — | Fallback short |
| 9 | speed=0.95 | Short sentence | 1715ms | — | Explored |
| 10 | speed=1.0 | Short sentence | 1936ms | Solid step-down from 1.05 if wanted | Fallback short |
| 11 | speed=1.05 | Short sentence | 1669ms | Sweet spot for short utterances | Yes — short |

### Voice model audition

Round 1 — 7 candidates at speed 1.05:
Thalia, Cordelia, Iris, Luna, Delia, Helena, Harmonia

Round 2 — 5 advancing at speed 1.0:
Thalia, Cordelia, Luna, Delia, Helena

Round 3 — shortlist at speed 1.0:
Thalia, Cordelia, Helena

Final — mid paragraph at speed 1.2:
Thalia vs Helena

| Voice | Model ID | Result |
|-------|----------|--------|
| Helena | `aura-2-helena-en` | **Selected** — Caring, Natural, Positive, Friendly |
| Thalia | `aura-2-thalia-en` | Runner-up — Clear, Confident, Energetic |
| Cordelia | `aura-2-cordelia-en` | Shortlist — Approachable, Warm, Polite |

### Chosen settings

```
model: aura-2-helena-en (selected — see voice audition above)
notes: speed tiers below carried from Thalia evaluation; not re-validated with Helena
       (P3 — effort stops here)

pronunciation control: inline IPA notation available per-request (Early Access as of
2026-04-05); useful for names, medical terms, brand names; not exercised — annotated
and deferred. See deepgram_tts_notes.md § Pronunciation Control for syntax.
```

### Speed tiers

| Tier | Speed | Use case |
|------|-------|----------|
| Short | 1.05 (fallback: 1.0) | Single-sentence / short replies |
| Medium | 1.2 | Paragraph-length content |
| Long | 1.4 | Multi-paragraph / bulk readouts |

### Observations

- **Starting click:** Deepgram consistently prepends a microphone-click artifact
  that overlaps the first word of speech on Pi/ALSA hw:0,0. Observed at all
  speed settings. Cause unknown — may be ALSA device cold-open, Deepgram
  encoding header, or PCM framing. Not observed on ElevenLabs or Cartesia.
- **REST latency scales with content length:** Full audio is fetched before
  playback begins (REST-only; no streaming). Short sentence: ~2s silence.
  Paragraph: ~7s silence. Two paragraphs: ~12s silence. Makes Deepgram
  unsuitable for bulk readouts regardless of speed setting.
- **Speed tuning not pursued:** The starting click is a disqualifying UX defect
  for the default voice (Thalia). Speed tiers are moot until the click is
  resolved or a different voice/encoding path eliminates it.
- Deepgram remains available as a selectable backend but should not be the
  default until the click artifact is investigated.
