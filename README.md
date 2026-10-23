# Helper Runner

A small skill that starts Codex or Claude. The runner receives installation instructions from our server, sets up the existing Rust helper, waits for verified heartbeats, then exits.

```sh
npx skills add random-react-dev/helper-runner
```

Ask your agent: **Use helper-runner to connect this Mac.**

Only `SKILL.md` and `scripts/launch.py` are installed. No helper binaries or installation commands are bundled in the skill. The old device-agent skill remains separate.

## Requirements and use

Apple Silicon macOS, Python 3.10+, and the selected Codex or Claude CLI are required. Codex is the default. From the repository:

```sh
python3 skills/helper-runner/scripts/launch.py --check
python3 skills/helper-runner/scripts/launch.py start
python3 skills/helper-runner/scripts/launch.py status
```

Use `--provider claude` for Claude. The default server is `https://ask-gateway.r95426882.workers.dev`. Both clients use **abliterated-model-large-flash** through the server's Abliteration.ai proxy, with the lowest configured thinking setting. The server supplies temporary model credentials; personal subscriptions and user CLI configuration are not used. There is no DeepSeek fallback for this new flow.

The default private state folder is `~/Library/Application Support/HelperRunner`. The helper installs once per Mac. A healthy compatible existing device installation is reused and verified without overwriting it.

The launcher handles connection, process startup, status and one retry. It checks server readiness every five seconds. After verified readiness, the runner has 30 seconds to exit naturally before the launcher stops its own runner process; the independent helper keeps running. It does not download, unpack or install the helper. The server gives those instructions to the runner. `task_agent.py` is server-distributed support for later work, not part of this skill's setup launcher.

## Isolated testing

Use a new test directory and unique LaunchAgent label. Explicit `--state-dir` avoids discovery of existing installations:

```sh
python3 skills/helper-runner/scripts/launch.py start --provider codex \
  --state-dir "$HOME/Library/Application Support/HelperRunnerTest-Codex" \
  --launchagent-label com.helper-runner.test.codex --label HelperRunnerTest-Codex
```

Use the same `--state-dir` with `status`. Never point a test at an existing installation. Verification evidence is recorded separately; process restart is not full reboot verification.

To remove a test, first confirm its runner has exited and no test task/upload is active. Unload only the test's exact LaunchAgent with `launchctl bootout gui/$(id -u) /absolute/path/to/test.plist`, then remove that test plist and its dedicated directory. Do not unload or delete an existing shared helper.

## Development checks

```sh
python3 -m unittest discover -s tests -p test_launcher.py -v
```

Server integration code and tests are development assets, outside the installed skill. Raw credentials and model/tool outputs must not be committed.


## Server integration prerequisites

This repository is a skill plus an additive integration for the existing Research Control server, not a standalone replacement server. Its `server/helper_bootstrap.py` depends on the existing Store/device authentication, enrollment, model proxy, task scheduler, uploads and runtime. The deployed server uses Python 3.12, FastAPI, Uvicorn and HTTPX; use the existing server's pinned requirements. Test additions also require pytest.

Apply `server/integration.patch` to the matching baseline server checkout after reviewing it, and copy `server/helper_bootstrap.py` beside `app.py`. The patch changes `app.py`, `store.py`, `provider_proxy.py`, `device_delete.py` and `static/dashboard.js`. Back up the database and code before deployment. Do not apply it blindly to a newer or modified server.

Configure the following server environment values:

- `PUBLIC_BASE_URL`: externally reachable HTTPS origin.
- `HELPER_BOOTSTRAP_RELEASES`: directory containing immutable `{sha256}.zip` artifacts and an explicitly selected `current.json` release manifest.
- `HELPER_BOOTSTRAP_MODELS`: path to the new release's Abliteration Codex catalog, not the old DeepSeek catalog.
- `HELPER_BOOTSTRAP_MODEL=abliterated-model-large-flash`.
- `ABLITERATION_API_KEY`: private server environment only; never place it in the skill or repository.
- Optional `HELPER_BOOTSTRAP_INSTRUCTIONS`: text file whose content is appended and pinned when each new setup session is created.

The existing server database/admin secret configuration remains required. HTTPS must forward public release downloads, authenticated `/api/device/helper-bootstrap/` requests, and native `/api/model/` requests. Registration uses the server's existing enrollment flow. Keep existing DeepSeek credentials if old devices still use that provider.

Build releases with `tools/build_release.py --help`; the build requires a matching existing helper binary and worker runtime source. Deploy the new module, hooks, release and catalog before making the skill available to users. This integration adds SQLite tables on startup; old endpoints and clients remain supported.

For integration tests outside the original workspace, set `RESEARCH_SERVER_SOURCE` to the patched server source directory, and put this repository's `server` directory on `PYTHONPATH`. Tests skip server-dependent modules when that source is unavailable:

```sh
RESEARCH_SERVER_SOURCE=/absolute/path/to/patched-server \
PYTHONPATH="$PWD/server" python3 -m pytest tests/test_backend.py tests/test_provider.py -q
```

See [verification boundaries](docs/verification-boundaries.md) for the distinction between simulated failures and live installation tests.

## Verified release

Both Codex and Claude completed real setup, exited after verified helper heartbeats, and subsequently completed helper-launched tasks. A 12 MiB background upload was downloaded and checked byte-for-byte. See [acceptance results](docs/live-acceptance.md) and [test boundaries](docs/verification-boundaries.md). A full Mac reboot has not been tested.
