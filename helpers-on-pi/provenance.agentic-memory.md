# Provenance — agentic memory (helpers-on-pi)

Lightweight **structural** context for agents working beside `provenance.spec.md` and
`provenance.reflection.md`. This file is the routing and placement briefing; the
canonical behaviour lives in the spec.

---

## Role in the three-file set

| File | Role |
|------|------|
| `provenance.spec.md` | Canonical tool behaviour and output shape (platform-agnostic). |
| `provenance.agentic-memory.md` | **This file** — where provenance lives in the tree, how it relates to parent memory, tooling pipeline expectations. |
| `provenance.reflection.md` | When to write provenance at session boundaries and how that intersects retention assessment. |

---

## Structural rules inlined (tool / memory pattern)

The following condenses patterns from **structured project memory** and **shared
`tools/` lifecycle** policies. Full architecture and edge cases live in upstream
policy docs; what follows is **execution-sufficient** for placing and maintaining
provenance artifacts.

### `tools/` pipeline expectation (abstract)

Shared tools follow **spec → implement → install** for the high-repetition happy
path:

- **Spec** — `provenance.spec.md` is the canonical definition. It is directly
  consumable: an agent given the spec and asked to produce a provenance record can
  do so without a platform-specific implementation.
- **Implement** — platform-native artifacts (e.g. editor rules, command markdown) are
  **derived** conveniences; they should not redefine behaviour that contradicts the
  spec.
- **Install** — derived copies live in the agent product's private memory or project
  config, removing the need to read the spec each time. The repo remains source of
  truth.
- **Co-committed updates:** when provenance filing conventions change or a standing
  provenance directory is added, update the nearest agentic memory file in the same
  change.

The pipeline is an optimisation gradient, not a gate. For corner cases or unfamiliar
platforms, showing an agent the spec directly is a viable — and often sufficient —
execution path.

**Install note (pattern borrowed from CLAUDE.md-style command files):** per-platform
install docs should name the **installed filename**, the **private memory path**, and
any **placeholders** (e.g. path to this repo) to substitute. Frontmatter and command
titles are product-specific; behaviour must match `provenance.spec.md`.

### Naming as lifecycle signal

- Time-bound knowledge: **date-prefixed** filenames (provenance default:
  `YYYYMMDD-HHMMSS_PROV_…`).
- Durable overrides: user-directed `PROV_<desc>.md` without timestamp prefix.

### Context budget

Load this trio **task-driven**: read the spec when implementing or writing a record;
read this file when deciding **where** provenance fits in repo memory; read
reflection when **ending** or checkpointing a session.
