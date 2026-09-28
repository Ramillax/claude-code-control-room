<!-- Paste this into your project's CLAUDE.md (adjust the resource names to your project).
     Plain slog locks are advisory: they work because the agents are TOLD to use them.
     With the hooks installed (install.sh --hooks), locks taken with --paths are enforced for
     file edits, and every session starts with the board and the project notes in context. -->

## Parallel sessions — `slog`

Several Claude Code sessions work on this project at the same time, each blind to the
others. They coordinate through a shared board with the `slog` command.

```bash
slog "what I'm doing"          # one feed line when you start something substantial
slog status                    # active locks + recent feed  <- ALWAYS before touching a shared resource
slog take "api:auth"           # lock only what you touch (a part), or "api" for the whole thing
slog take "api:auth" --paths 'src/auth/*'   # …and tie it to files: other sessions can't edit them
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
6. If an edit is denied because another session holds the file, don't work around it (no shell
   tricks to write it anyway). Tell the user who holds it, or work on something else.
7. When several sessions could need the same exclusive thing for one command (a logged-in
   browser, a device), run it as `cr-exclusive <name> -- <command>`.

## Project notes — `cr-notes`

A new session (or one after compaction) starts with the project notes in its context. Keep them
current so nobody redoes finished work or reopens a settled decision:

```bash
cr-notes done "what you finished"      # when something is really finished
cr-notes next "what comes next"        # when you stop with work pending
cr-notes dont "decision / trap: why"   # something nobody should redo or undo
cr-notes drop <n>                      # remove a NEXT once it's done (numbers from `cr-notes show`)
```

## Other sessions — `cr-cc`

`cr-cc` shows every session's state (working / BLOCKED / idle), mode and locks; `cr-cc peek <session>`
shows what one of them is doing or asking. Use them to avoid stepping on another session's work.
**Never use `cr-cc reply`** to answer another session's prompt unless the user explicitly asks you to:
its permission prompts are for the user.

## History — `cr-hist`

Every file a session writes is recorded with the session that wrote it. Before changing a file
another session may have touched, or when something "changed by itself":

```bash
cr-hist changed 6          # what other sessions changed in the last 6 hours
cr-hist who <file>         # who changed it, when, and the session id (= transcript name)
cr-hist prev <file> <out>  # the version before the last change, to compare or revert
```

Don't commit to the project's own git to "save" work: history is already recorded, and the
project's commits are the user's decision.

## Handing things to the user

- `cr-clip "text"` — a long command or ID the user must paste somewhere, or anything they ask you to copy: they copy it with one tap (📋), and in the Chat view it also shows up as a **Copy** card right under that step. Use it by default: the user may be on a phone, where selecting text in the terminal does not work.
- `cr-expose <file>` — a file the user should download: it appears first in the 📥 dialog.
- In the Chat view, **write the file's full path in your reply** (absolute or `~/…`): if it exists inside the file roots it shows up as a one-tap **Download** card right there. Images and PDFs get a thumbnail and open in the in-page viewer instead.
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
