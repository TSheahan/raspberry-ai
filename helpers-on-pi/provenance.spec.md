# Provenance — abstract tool specification

Platform-agnostic definition of **conversation provenance capture**: a durable markdown
record that preserves user direction, decisions, and a compressed account of agent
actions.

This spec is canonical for behaviour and output shape. Agent-specific install
artifacts (rules, commands, pointers) are derived from it; see
`provenance.agentic-memory.md` for how this tool sits in structured memory and
`provenance.reflection.md` for when to write without a magic phrase.

---

## Capture model (no recall path)

- **Single source of truth:** the agent’s **current working context** (live
  conversation visible to the agent). Provenance is always **capture** from that
  context.
- **No log reconstruction:** there is no separate command or workflow to rebuild a
  record from archived transcripts, external session stores, or listing scripts.
  Omit any session-discovery, extraction, or “pick a past session” behaviour from
  implementations of this spec.
- **Implicit capture:** producing a provenance file does not depend on a secondary
  “recall” capability. When policy or reflection calls for a record, the agent
  synthesises it from the context it already has.

---

## Shared conventions

### Output format

Write a markdown file with this structure:

```
# Provenance: <desc>

**Session date:** <date of session>

---

## Summary

<summary>

---

## Transcript

<transcript>
```

Do **not** include session IDs, internal correlation identifiers, or other platform
metadata in the file body.

### Summary

A structured summary covering:

- What was built or changed and why
- Key decisions made
- How the session was directed at a high level — the arc of the conversation

Rules:

- Exclude content related to private or shared **memory operations** (reads, writes,
  memory file paths and their contents)
- Exclude session metadata (IDs, version strings, internal system context)
- Describe what was accomplished, not which tools were called

### Asymmetric transcript

Work through the conversation turn by turn:

**User turns:** reproduce authored text in full. The only transformation allowed is
replacing pasted or non-authored content (terminal output, file contents, error logs,
data) with a short descriptor in brackets, e.g. `[pasted: error output]`. Everything
the user wrote themselves is kept verbatim — prose, context-setting, rationale, not
only directives. Attachment descriptors (e.g. `[attached: filename lines 1-50]`) may
replace bulky attachment bodies.

**Assistant turns:** summarise to one paragraph maximum. Capture the key action,
response, or output. Where the live context included tool-call indicators or narration
of tool usage, replace with a brief description of what was accomplished — no more than
three lines.

**Drop entirely:** entries that contain only session metadata or system context.

### Timestamps

Use the system clock for all timestamps — filename, session date, and any other time
reference. Assume the clock is already in the user’s local timezone; do not perform
timezone conversion or offset arithmetic unless the user explicitly requests it.

### Filename

`YYYYMMDD-HHMMSS_PROV_<desc>.md`

Components after the timestamp:

1. **Type prefix** — `PROV` for provenance records, `PLAN` for companion plan files.
2. **Description** — hyphen-separated, lowercase, no punctuation, 3–5 words. Derive
   from the conversation’s main topic or action. If the user supplies a description,
   use it.

When a provenance record has a companion plan file, the **identical timestamp** is
the correlation key. Each file carries its own description suited to its content.

**Non-time-bound override:** if the user directs a durable (non-time-bound) record,
drop the timestamp prefix — `PROV_<desc>.md` — and use the user’s location. Default
remains time-bound.

### Write location

Provenance is a **time-bound artifact by default** (date-prefixed filename). The agent
proposes a default directory; the user confirms or redirects.

**Default:** the session’s primary work directory — where the main work product lives.
If work is spread across locations, prefer the most-edited directory; if still
ambiguous, use the deepest common ancestor of the session’s changes.

**Exploratory fallback:** when there is no clear work directory (pure conversation,
research, or cross-cutting exploration), ask for a location.

Always confirm the path written.

### Plan collection

When the session included execution of a **plan**, collect it as a companion file next
to the provenance record. Default **on** unless the user opts out (e.g. “no plan”).

**Plan file format:**

```
# Plan: <plan desc>

**Session date:** <date of session>

---

<plan content as authored>
```

**Plan filename:** `YYYYMMDD-HHMMSS_PLAN_<plan-desc>.md` — same timestamp as the
companion provenance file, distinct description. Derive the plan description from the
plan’s title or subject (3–5 words, hyphenated, lowercase) unless the user overrides.

If the plan was revised during the session, capture the **final** version that drove
execution.

### Context compression

Platforms may compress long conversations, replacing early turns with a summary
artifact. A provenance record should still cover the **full session** with the best
fidelity available: the compressed portion may be lower fidelity, but coverage must not
be truncated. **How** to detect compression and map summary sections into transcript
and summary narrative is **platform-specific**; this spec only states the goal.

---

## Invocation

Implementations may expose a natural-language trigger (e.g. “provenance capture”) for
explicit requests. **Regardless of trigger,** the behaviour is always live-context
capture as defined above — there is no alternate “recall” implementation surface.

---

## Coherence with implementations

When this spec changes, every derived platform artifact should be updated in the same
change set. Enhancements discovered in a platform-specific file should be back-ported
here so all platforms stay aligned.
