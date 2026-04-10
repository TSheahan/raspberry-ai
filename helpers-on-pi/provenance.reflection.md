# Provenance — reflection (session boundaries)

Use at **natural session boundaries** (pause, hand-off, end of task) alongside the
project’s own retention rules. This file does not replace project or team policy; it
connects **implicit provenance capture** to **deliberate reflection**.

Canonical write format and steps: `provenance.spec.md`.

---

## Implicit capture without a magic phrase

Provenance is **always** synthesised from **current live context** (see spec). There
is no transcript “recall” path. Reflection answers: **should this session leave a
provenance artifact now**, not “which archived session to rebuild.”

---

## Retention assessment (compact)

Before closing or major context switch, briefly assess:

1. **Worth a record?** Would a future reader (human or agent) benefit from the
   **decisions, constraints, and outcomes** of this session, not just the final diff?
2. **Project-purpose fit?** Does the substance support the repo’s declared purpose
   (per root agentic memory)? If not, skip or keep notes in scratch only.
3. **Primary work directory?** Resolve write location per the spec (primary product
   directory, most-edited path, or ask).
4. **Plan sibling?** If a plan drove execution, include the companion `PLAN` file with
   the same timestamp unless the user declined.
5. **Memory hygiene:** in the provenance **summary and transcript**, do not leak
   private memory operations or internal session metadata (per spec).

If the user already asked explicitly for a capture, execute the spec; this checklist
still applies for location, plan sibling, and exclusions.

---

## Two-destination mental model (optional)

When the project adopts **structured project memory** fully:

- **In-repo:** provenance and companion knowledge files in the appropriate directory,
  with AGENTS.md routing updated when filing becomes routine.
- **Outbound:** if the session produced **transferable** patterns that pass the
  shared-memory test for a team vault, flag for synthesis there — without duplicating
  full transcripts unless policy asks for it.

Provenance in this folder is **project-scoped narrative**; outbound material is
usually a shorter synthesis with pointers back to the repo.

---

## After writing

- Confirm the **path** with the user when practical.
- If provenance becomes a **recurring** artifact for this directory, add or update a
  routing line in the relevant **AGENTS.md** (“read when reviewing session history for
  X”) rather than bloating AGENTS.md with full text.
