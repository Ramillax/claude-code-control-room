# Security policy

## Read this first: it has no authentication, by design

Claude Code Control Room gives whoever can open the page **a shell running as your user**. It
ships without login on purpose (see the README's Security section): you are expected to put it
behind your own authentication (an SSO/zero-trust proxy, a VPN, or an SSH tunnel) and keep the
server bound to `127.0.0.1`.

So "anyone who reaches the port gets a terminal" is **expected behavior, not a vulnerability**.

## What IS a vulnerability

Things that break the protections the project does promise, for example:

- escaping the file browser's allowed roots (`CR_FILE_ROOTS`) to read or download other files
- deleting anything outside `uploads/` and `outbox/`
- opening a tmux session that is not in the `CR_SESSIONS` allowlist
- reaching ttyd without going through the server's `/tty/` proxy
- getting typed text (uploads, dictation) to execute without the user pressing Enter
- the server binding to anything other than `CR_BIND`, or leaking API keys (logs, pages, the dictation log)

## How to report

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security tab → "Report a vulnerability"** on this repository.

Include what you did, what happened, and the version (commit) you tested. You'll get an answer
as soon as possible; fixes go to the `main` branch (there are no separately maintained versions).
