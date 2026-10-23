# Testing helper-runner

## Current release: research MCP compatibility

Version `2026.10.23.6` supersedes `.5` by setting the Codex catalog's `supports_search_tool` to false. All five other package members are byte-for-byte unchanged. Archive SHA-256: `7cb7e015d795dfd9be199749da457f76eac0d42485df5498d42c4c883d129c64` (398,362 bytes). Standalone manifest/catalog are in development evidence `release-2026.10.23.6/`.

The actual installed Codex CLI was tested with a local mock MCP server advertising `research_search` and `research_fetch`, plus a dummy local model endpoint. The new catalog emits ordinary function tools and an MCP namespace containing function children, with no native `tool_search` or custom grammar tools. Both MCP initialization/tool listing and presence of both research tools are checked. Nine packaging tests pass. This local capture checks wire definitions; real provider execution and actual gateway research are separate live acceptance tests owned by the release operator. The default `.5` catalog instead emitted native tool discovery; removed feature flags did not change that behavior. The supported model capability was located in OpenAI's official `codex-rs/core/src/tools/spec_plan.rs` and `codex-rs/protocol/src/openai_models.rs`.

## Signed setup instructions

`server/setup-install.md` and `server/setup-reuse.md` are reviewed, static server instructions. The runner reads only its validated `HELPER_RUNNER_CONTEXT` file for machine-specific values. Installation uses a bounded, no-redirect Python download and verifies the signed archive SHA-256 before any extraction. This supersedes the earlier curl/unzip instruction pattern described in historical tests below.

The extraction block checks the exact signed manifest, member hashes and sizes, a 32-member/128 MiB expanded bound, safe relative paths and regular-file types. It rejects duplicate names, symlinks, special files, unsupported ZIP metadata and linked destination files/parents. Existing differing files are preserved by failing rather than overwriting. The reuse template only polls status.

`tools/sign_setup.py` is a development-only signing tool. Supply `--key`, `--release`, `--models`, `--install`, `--reuse`, `--revision` and `--output`; optional `--expiry-days` defaults to 90. It signs canonical ASCII JSON using OpenSSL RSA-SHA256 and writes a base64 payload/signature envelope. Generate and store the private key separately; do not commit it. The launcher pins only the public verification key. The tool refuses to overwrite a different envelope.

Seven signed-asset tests passed using the development Python 3.12 environment, including extraction of the actual `.5` release, hostile archive cases, partial-response retry, RSA signature verification and immutable output. These tests never load a LaunchAgent. The local Python 3.14 installation failed to import `plistlib` because its `pyexpat` dynamic library was incompatible with the system libexpat; use the verified Python 3.12 interpreter for the setup context on this test machine.

## Current release: lowest thinking

Version `2026.10.23.5` supersedes `.4`. Build it with `--model-profile abliteration --patch-tool none`. Its SHA-256 is `25d288ab7492b40566c4fa8392cc4acf64619f8ffdb70760d8162b96cfd65e45` (398,361 bytes). Codex catalog defaults to `none` and advertises only `none`; the packaged runtime explicitly uses reasoning `none`. Claude gets `alwaysThinkingEnabled:false`, `MAX_THINKING_TOKENS=0`, and `--effort low`. The server must also enforce disabled thinking for a third-party provider that treats omission as its own default. Existing source files remain unchanged.

Nine packaging tests passed. The actual Codex CLI fixture passed in 0.978 seconds and verified outgoing `reasoning.effort` was `none`, with only standard function tool definitions. This is local wire-format verification, not a claim about the provider's internal processing. The standalone deployment manifest and catalog are staged in `work/helper-runner-evidence/release-2026.10.23.5/{current.json,models.json}` in the development workspace. Earlier release notes below are historical.

## Local download fault evidence

The server instructions now include `--retry-all-errors`. Three behavioral tests in `tests/test_download_faults.py` passed against a temporary loopback HTTP server using Apple's `/usr/bin/curl` 8.7.1, plus a fourth check ties the tested flags and fail-closed ordering to the server instruction source. A response interrupted halfway returned curl error 18 with the historical `--retry 3` flags and correctly did not extract. Adding supported `--retry-all-errors` caused a second request, downloaded the complete package, verified SHA-256 and extracted successfully. An intentionally incorrect SHA-256 stopped the `set -eu` shell before creating the extraction directory. This tests the shell transport/integrity pattern only; it does not demonstrate an AI runner repairing a real interrupted setup. No helper or model was launched by these tests.

The system skill validator also reported `Skill is valid!` for the then-current `skills/helper-runner` (now `skills/research`), using the existing development virtual environment.

The installed skill contains only `SKILL.md` and `scripts/launch.py`. The release builder is a developer tool, not part of the installed skill. It does not install or start anything.

## Release checks

```sh
python3 -m unittest discover -s tests -p 'test_release.py' -v
python3 tools/build_release.py \
  --binary /absolute/path/to/device-helper \
  --runtime /absolute/path/to/worker-runtime \
  --out /absolute/path/to/new-release-directory \
  --version 2026.10.23.1
```

The builder reads an explicit allowlist, verifies Python syntax and the model catalog, normalizes ZIP timestamps and permissions, and writes `<sha256>.zip` plus `<sha256>.json`. It does not overwrite the old release channel or select a live release. Deployment explicitly copies the chosen manifest to the new channel's `current.json`.

For the new Abliteration connection, build version `2026.10.23.4` with additional flags `--model-profile abliteration --patch-tool none`. This transforms only archive copies of the worker's model allowlist/default and Codex catalog to `abliterated-model-large-flash`. The shared source files and old release are untouched. The catalog sets `apply_patch_tool_type` and `multi_agent_version` to null, uses text/image inputs and a 1,048,576-token context, and contains no DeepSeek labels. The packaged runtime disables multi-agent and goals features so it emits standard function tools. Shell tools can still create and edit files. Nine packaging checks pass, including isolated transformation, source-drift rejection and null custom-tool settings. Version `.2` used a `function` catalog value rejected by the installed CLI; `.3` used a custom freeform grammar tool rejected by the provider. Retain those artifacts only as historical evidence and do not deploy them.

`tests/test_codex_catalog.py` exercises the real installed CLI with the built catalog, a dummy key and a temporary loopback endpoint. Set `HELPER_RUNNER_TEST_RELEASE` to the absolute ZIP path when running it. The fixture deliberately returns HTTP 400 after capturing the request. It verifies catalog parsing and, for the null custom-tool configuration, that every emitted tool has type `function`. This confirms CLI configuration and tool-shape compatibility, not model execution or provider acceptance.

Final version `.4` passed the actual CLI fixture test on 2026-10-23 (0.985 seconds), with web search and multi-agent/goals features disabled to match the launcher. Every emitted tool was a standard function. Its archive SHA-256 is `ed0a8d0071d818b2f35dd4679b6f3e8639ae8ccff13e8f4671f7d89d10519012`; archive and all six member hashes were independently rechecked.

Archive contents:

```text
bin/device-helper
runtime/common.py
runtime/models.json
runtime/outputs.py
runtime/release_client.py
runtime/task_agent.py
```

There is no installer, runner wrapper, setup script, credential, or PC configuration in the archive. `task_agent.py` is support for later work and must not run during setup. Server instructions must set `uploader_binary` to the absolute installed `bin/device-helper` path so uploads do not fall back to an old runtime name. The runner sets executable permission after extraction; Python's ZIP extraction need not restore Unix executable bits automatically.

## Live acceptance checks

Record actual results separately; this checklist is not evidence that a test ran.

1. Use an explicitly isolated test registration, directory and LaunchAgent label. Record old-installation fingerprints first; never replace the user's working helper for a test.
2. Run setup with Codex and with Claude independently. Verify each runner fetches server instructions, downloads the exact digest-addressed artifact, verifies SHA-256, configures the helper and loads its LaunchAgent.
3. Ensure server readiness needs two authenticated helper heartbeats from the same instance after setup began, at least 15 seconds apart. A runner report alone must never mark ready.
4. Confirm runner exit, then another independent helper heartbeat. Queue one harmless worker task and verify its file arrives through helper-managed uploading.
5. Repeat start during setup and after readiness; check no duplicate runner, registration or helper appears.
6. Exercise interrupted download, checksum mismatch, API outage, expired credential, runner termination and absent helper heartbeat. Verify bounded retries and a useful failure state.
7. Change a harmless server instruction for a new session; the same installed skill must follow it without modification.
8. Run resumable-upload regressions. Process interruption is not a physical restart test; label evidence accurately.
9. Stop and remove only test-owned LaunchAgents, directories and registrations. Compare old-installation fingerprints afterward.
10. Test anonymous skill installation in an isolated temporary directory using `npx skills add random-react-dev/deep-reserach`. The public repository must contain no generated secret files or evidence containing credentials.

## Local results during initial build

On 2026-10-23, all six release unit tests passed: deterministic archives and hashes, exact member allowlist and permissions, symlink rejection, missing dependency rejection, invalid Python/catalog rejection, and immutable overwrite protection. Live tests are reported by the release operator after deployment.

The helper was rebuilt offline into a separate build directory without updating the old build output. Seven Rust unit tests passed. Eight existing upload integration tests also passed against the new binary and a temporary loopback HTTP fixture: concurrent submission, lost acknowledgements, helper process termination and offset recovery, new arrivals during upload, completion cleanup, terminal server failure and source-file independence. These tests used no LaunchAgent and no paid remote API. The initial sandboxed run could not bind a loopback socket; the authorized local-fixture run completed successfully in 61.836 seconds.
