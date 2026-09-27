# Contributing

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Ground rules of the project

These are what keep it easy to install and easy to trust, so PRs that break them will be
asked to change:

- **No new dependencies.** Python standard library, bash, and one HTML file. No npm, no pip
  packages, no build step, no frameworks, no CDN scripts.
- **Nothing downloads at runtime.** `install.sh` and the server never fetch code.
- **Security defaults stay safe.** Localhost binding, the session allowlist, the file-root
  checks, POST-only delete, and "type without Enter" must not be weakened. If a feature needs
  to relax one, make it opt-in and document the risk.
- **Uploads and dictation use form POSTs into an iframe, not `fetch()`.** Auth proxies that
  challenge background requests would silently break them (see the README).
- **English** in code, comments, UI and docs. Comments explain *why*, not *what*.

## Before opening a pull request

```bash
python3 -m py_compile server/server.py hooks/cr-state-hook.py bin/skill-lint
for f in bin/slog bin/cr-* bin/skill-new start.sh install.sh; do bash -n "$f"; done
```

Then run it for real with an isolated tmux server, so your own sessions aren't touched:

```bash
cat > /tmp/crtest.env <<'EOF'
CR_PORT=7699
CR_SESSIONS="t1 t2 sh"
CR_SHELL_SESSIONS=sh
CR_CLAUDE_CMD=bash
CR_TMUX_SOCKET=crtest
CR_STATE_DIR=/tmp/crtest
CR_WORKDIR=/tmp
EOF
CR_CONFIG=/tmp/crtest.env ./start.sh
# open http://127.0.0.1:7699 ; when done: Ctrl-C, then  tmux -L crtest kill-server
```

(A separate `CR_CONFIG` file, not inline variables: `start.sh` loads the config file, which
would override them.)

Describe in the PR what you tested (desktop / phone browser, which features).

## Reporting bugs

Open an issue with the bug template: what you did, what you expected, what happened, your
distro and the output of `tmux -V`, `ttyd --version` and `python3 --version`.
**Security problems go through private reporting instead** (see `SECURITY.md`).

## Credit

Contributions are released under the project's MIT license.
