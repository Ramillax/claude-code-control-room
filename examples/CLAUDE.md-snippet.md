<!-- Paste this into your project's CLAUDE.md (adjust the resource names to your project).
     slog only works if the agents are TOLD to use it: the locks are advisory. -->

## Parallel sessions — `slog`

Several Claude Code sessions work on this project at the same time, each blind to the
others. They coordinate through a shared board with the `slog` command.

```bash
slog "what I'm doing"          # one feed line when you start something substantial
slog status                    # active locks + recent feed  <- ALWAYS before touching a shared resource
slog take "api:auth"           # lock only what you touch (a part), or "api" for the whole thing
slog free "api:auth"           # release as soon as you're done
slog done                      # at the end of the session: removes your own lines
```

Rules:
1. Before changing a **shared resource** (production config, the database schema, a deployed
   service, a file another session may be editing), run `slog status`. If someone holds an
   overlapping lock, do not touch it — tell the user who holds it.
2. Lock the **smallest** thing you touch (`api:auth`, not `api`) so other sessions can keep working.
3. `take` warns when your lock overlaps another session's. Treat that warning as a stop sign.
4. Locks are for dangerous shared things only, not for every file you open.
5. Never edit the board file by hand; use the subcommands.

## Handing things to the user

- `cr-clip "text"` — a long command or ID the user must paste somewhere, or anything they ask you to copy: they copy it with one tap (📋). Use it by default: the user may be on a phone, where selecting text in the terminal does not work.
- `cr-expose <file>` — a file the user should download: it appears first in the 📥 dialog.
- Uploaded files arrive under `.controlroom/uploads/`. For PDFs, read the `.txt` sidecar first — far fewer tokens.

## Skills — durable knowledge

The board is ephemeral. What you learn goes into the project's skills (`.claude/skills/`),
following the 3-layer structure: `SKILL.md` = router (short), `references/<topic>.md` = how it
works today, `references/changelog.md` = decision log with stable `#NNN` ids.

- Never copy live values (versions, counts, flags) into a skill: write where they live and how
  to read them (`↻ verify: <command>`).
- Only document YOUR OWN work, verified against the live system.
- Before editing a skill: `slog take "skill:<name>"`; release it after.
- When the user says "sync skills" (or at the end of a session that changed how something works),
  use the `skill-sync` skill, run `skill-lint`, and finish with `slog done`.
