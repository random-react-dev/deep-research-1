# Live acceptance — 2026-10-23

Model: abliterated-model-large-flash. Thinking disabled. Codex 0.163.0, Claude Code 2.1.294, Apple Silicon macOS.

Release 2026.10.23.5: SHA-256 `25d288ab7492b40566c4fa8392cc4acf64619f8ffdb70760d8162b96cfd65e45`.

| Check | Observed result |
| --- | --- |
| Real Codex setup | First attempt: fetched server instructions, downloaded and verified package, installed helper, server confirmed two heartbeats, runner exited |
| Real Claude setup | First attempt after correcting provider thinking field: same complete flow |
| Independent helper | Both sent fresh heartbeats after their launcher and runner exited |
| Subsequent task | Both helpers launched the requested CLI worker and completed a harmless file-creation task |
| Returned files | Both text files downloaded and matched expected bytes |
| Background upload | Codex produced a 12 MiB file; Rust helper uploaded it; server download matched every byte and SHA-256 |
| Repeat start | Codex returned already_ready, retaining its session and single helper |
| Isolation | All eight test-only registrations/directories and three test LaunchAgents removed; private logs retained outside repository |

The original skill and helper configuration, executable and plist match all 19 recorded baseline hashes. No test resource or provider credential is published here.

The final setup tests used separate device registrations, directories and LaunchAgent labels. No task worker ran during setup. Later task tests were submitted after setup success. Earlier failures exposed incompatible Codex custom grammar tools, Claude bearer configuration, HTTP User-Agent handling and the provider's boolean thinking incompatibility; those were corrected before the successful runs.

Failure tests and limits are described in verification-boundaries.md. Download disconnect/checksum tests use a real local HTTP fault fixture and curl, not a billed model. This release does not claim a full-machine reboot test or that every model error is recoverable.

Final combined regression run: **173 passed**, three subtests passed, one dependency deprecation warning. Includes the actual Codex catalog/request fixture and download-fault fixtures. Earlier Rust unit and resumable-upload integration runs passed (7 and 8 tests respectively).

Publication check: the repository is public and anonymously readable. The exact command `npx skills add random-react-dev/helper-runner` completed in an isolated project, discovered one skill, and installed exactly `SKILL.md` and `scripts/launch.py`. Both files match the published source.
