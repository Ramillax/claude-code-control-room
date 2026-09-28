---
name: skill-sync
description: Consolidate what this session learned into the project's skills so the NEXT session (or a parallel one) starts oriented instead of cold. Use when the user says "sync skills", "update the skills", "document this in the skill", or similar; and proactively offer it at the end of a session that changed how a system behaves, fixed a bug, made a design decision, or discovered that the docs were wrong (drift).
---

# Skill Sync

Skills are the shared, durable memory of a project that several Claude Code sessions work on
in parallel. The live board (`slog`) is ephemeral; this is where what matters lands before a
session closes. The goal is not more text: it is that a future session, starting cold,
understands the system correctly and builds on it without re-discovering everything.
**A stale fact is worse than a missing one**: it makes the next session assert something false
with confidence, and every decision built on it inherits the error.

## The structure to respect: 3 layers

| Layer | File | What goes here | What does NOT |
|---|---|---|---|
| **Router** | `SKILL.md` | frontmatter, 3–5 line intro, map of references, short durable rules | subsystem detail, history, walls of text |
| **Current state** | `references/<topic>.md` | how each subsystem works TODAY | how it got that way |
| **History** | `references/changelog.md` | what changed, when and **why**; dated entries with stable `#NNN` ids | current state (that lives in the topic file) |

**One truth, one place.** Every fact lives in exactly one file. If it is in three places, two
will rot. The router is loaded in full every time the skill triggers, so every KB there is paid
on every use: keep `SKILL.md` under 12 KB and move detail to a reference. References have no
size budget; they are read with grep + a partial Read.

## Snapshot vs pointer: the rule that prevents most drift

What rots is whatever **mirrors live state**: a value, a version, a count, "currently only X is
enabled". What lasts is the *why*, the architecture, a gotcha, an identifier.

Before writing a fact, classify it:
- **Durable** → write it. This is knowledge that lives nowhere else (reasoning, decisions, traps).
- **Live state** → do NOT copy the value. Write one line about what it does, plus **where it
  lives and how to read it now**:

```
Retries failed webhooks with backoff.
↻ verify: <command that shows the current config / query / file:line>
```

If you catch yourself copying a number, version or status that could differ tomorrow, stop and
leave the pointer instead.

## Procedure

### 1. Inventory YOUR work first (write nothing yet)
List the concrete changes and findings of this session: what you changed (with identifiers),
behavior that is new or corrected, decisions and their reasons, and every place where reality
differed from what the skill said. If the scope is unclear, ask the user.

**Only your own work.** Read the board (`slog tail 40`) to orient yourself (not to overwrite
someone's change, to notice a stale base), **not to collect material**. A feed line has no
reasoning and no gotchas: documenting another session's work from it produces something
plausible and wrong, and it collides with what that session writes. Each session consolidates its
own work; if something of theirs looks unconsolidated, tell the user.

### 2. Verify against the live system before asserting
Do not write from memory or from what you believe. For each fact, confirm it with a tool
result: read the code, query the system, check the config. An identifier or behavior that did
not come out of a tool result does not get written.

### 3. Route each fact to its single owner file
Open `references/owners-map.md` (in this skill) to find which skill/file owns the topic, and
edit there. Apply snapshot-vs-pointer to every fact. If a change spans topics, split it. If you
found **drift** (a reference said something no longer true), fix it: that is the most valuable
part of the job, more than adding the new thing.

**Parallel sessions:** before editing a skill file, `slog take "skill:<name>"`; release it at
the end. Two sessions rewriting the same reference is how facts get silently lost.

### 4. Changelog discipline
`references/changelog.md` is a single chronological log, **newest on top**, grouped by day
(`## YYYY-MM-DD`), one entry per line block: `**#NNN** · **Title** (date; ids; state in
references/<topic>.md) — what, why, gotchas, ↻ pointers`.
- **id = next free integer.** Ids are stable references (other files cite "see #42"): never
  renumber or reorder them.
- ⚠ **Parallel sessions collide here**: two can read the same maximum id and both use it.
  Before finishing, run `skill-lint`. It flags duplicate ids. Renumber the entry that is cited
  **less** (`grep -rn "#NNN"`) and fix its citations.
- If a change supersedes an older one, say so ("SUPERSEDES #42") instead of deleting it.
- The changelog is a **decision log**, not a diff history: git already tells you which bytes
  changed. Write what git cannot see: the *why*, and changes outside the repo (databases, APIs,
  deployed services, dashboards).

### 5. Skill or memory?
Skills = **how the system works** (shareable, for any session). Claude Code's memory = the
**user's preferences and project state** that is not derivable from code. A user preference
goes to memory, not to a skill. Sometimes both: the skill gets the "how", memory the "why
they want it".

### 6. Close the board
For each of YOUR feed lines with something durable that hasn't landed yet, land it now (steps
3–5). Then run **`slog done`**: it removes your lines, never other sessions' lines, never a lock
you still hold. Never delete or rewrite the board by hand: other sessions' live locks are in it.

### 7. Report
Tell the user which files you touched and what changed in each (a short table). List any drift
you noticed but did not fix, so they can decide.

## Golden rules

- **Exact beats complete.** If you doubt a fact, verify it or leave it out.
- **Confirm before restructuring.** Editing and adding entries: go ahead. Moving, renaming or
  re-architecting files: show the plan first.
- **Move verbatim.** When relocating content, move it with a script and `diff` to prove nothing
  was lost; back up large files first.
- **Staleness check in passing.** If a reference hasn't been touched in months but its
  subsystem did change (grep the changelog), verify its main claims before building on it.
