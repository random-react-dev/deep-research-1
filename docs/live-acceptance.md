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

## Signed setup update

Revision `2026.10.23.signed1` binds the reviewed setup/reuse instructions, release `.5` and model catalog with an offline RSA-3072/SHA-256 signing key. The private key was not uploaded or committed.

Both real Codex and Claude runs verified the envelope before model startup, installed the unchanged helper package, reached server readiness on attempt one, and exited. Both helpers subsequently sent fresh heartbeats, launched their selected workers and returned `signed-setup.txt`; downloaded bytes matched `SIGNED_SETUP_OK\n`. Only the two test-owned services, directories and records were removed. All 19 original baseline hashes still match.

Final regression result: **203 passed**, eight subtests passed; skill validation passed. Coverage includes real signature validation, tampering/wrong-key/expiry rejection, model/catalog mismatch, preflight failure without model startup, hostile ZIPs, linked destinations and interrupted downloads. Two warnings are a dependency deprecation and an intentionally duplicated hostile ZIP member.

This machine's default Python 3.14 has a broken `plistlib` dependency. The launcher now detects that prerequisite failure clearly; live tests used the working Python 3.12 interpreter. No permission prompts were added. Unrestricted runner permissions and the model/proxy trust boundary remain unchanged.

## Research-first acceptance

The only discovered skill is now `research`, derived from the user's ASK source. It contains SKILL.md plus ask.py and the existing signed launch.py. Original ASK files and global skill installations were not modified.

Release `2026.10.23.6` (SHA-256 `7cb7e015d795dfd9be199749da457f76eac0d42485df5498d42c4c883d129c64`) changes only the model catalog to disable optional native tool search. Actual Codex requests expose ordinary function tools and an MCP namespace of function tools; Abliteration accepted that format. Signed setup revision `2026.10.23.research1` explicitly enables Research Gateway.

| Live check | Codex | Claude |
| --- | --- | --- |
| One-time signed setup | First attempt ready | First attempt ready |
| Successful Research Gateway searches | 8 | 3 |
| Successful Research Gateway fetches | 8 | 5 |
| Non-generated report.md returned | 2,714 bytes | 2,828 bytes |
| Download checksum verified | Yes | Yes |
| Same question reuses job ID | Yes | Yes |
| Service online after setup runner exit | Yes | Yes |

The acceptance question compared R2 multipart cleanup with GCS resumable-session expiry. Both reports included official-source citations, the configurable-vs-fixed distinction and uncertainty. This proves actual tool use and report delivery, not perfect factual accuracy for every future research question. Source fetching and synthesis remain separate quality concerns.

Both test connections, background services and server records were removed after completion; evidence and example reports remain privately outside the repository. The original ASK source and all 19 old installation/skill baseline hashes remain unchanged.

Final research-first regression run: **235 tests passed**, eight subtests passed; the skill validator passed.
