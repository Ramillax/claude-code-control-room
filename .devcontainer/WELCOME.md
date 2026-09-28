# Welcome to the Control Room codespace

The panel is already running on port **7680**. If your browser didn't open it:
**Ports** tab (bottom panel) → **Control Room** → 🌐 *Open in Browser*.

1. Open the **claude1** tile and log in to Claude Code (first time only; a link opens in your browser).
2. Open **claude2** too, and give each session a task on this repo.
   Try: *"Take a lock on README with `slog take readme`, then suggest improvements"*, and watch the board 🗂.
3. When a session needs your permission, its tile turns red and a badge appears in the top bar.

**Security**: the port is **Private** (only your GitHub account can open it). Keep it that way.
Making it Public would expose a shell to anyone with the URL.

Panel stopped? Run `bash .devcontainer/start-bg.sh` in a terminal. Log: `/tmp/controlroom.log`.
