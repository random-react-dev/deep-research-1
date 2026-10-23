# Research-first implementation plan

## Outcome
One research skill: question → job ID → signed first-use setup if needed → queued research → Research Gateway → cited report. The background service supports research; it is not the skill's headline. Preserve a short, accurate first-use disclosure. Do not modify the original ASK checkout, global skills, old releases or existing installations.

## Evidence and choices
- Read ASK SKILL.md, ask.py, release_client.py and its tests, plus server runner_cli.py, common.py, task_agent.py, research_proxy.py and output handling.
- Reuse ASK's durable receipts, same-question deduplication, background coordination, status/result/resume commands, deadlines, retrieval and retention.
- Do not copy its old unsigned coordinator download, direct worker startup, DeepSeek configuration or automatic global skill promotion. Those conflict with this project's verified setup, selected model and existing-installation isolation.
- The current worker package already implements Research Gateway MCP through a task-scoped server proxy. No extra background tool or package dependency is needed.
- Keep Abliteration Large Flash with thinking disabled. Keep the signing key off the server.
- Retain the public repository and short installation command; expose one skill named research rather than two competing skills.

## Steps
1. Copy the ASK client into skills/research/scripts/ask.py; retain the signed launch.py next to it. Replace helper-focused SKILL.md with research instructions, commands, report-reading requirements and one background-service disclosure.
2. Add device-authenticated research submission, task lookup/status and output download routes. Reuse the existing queue, idempotency, ownership checks and report prompt. Add read-only lookup for lost acknowledgements after a local deadline.
3. Replace the ASK downloaded-coordinator loop with signed setup readiness, queue submission, status observation and verified file retrieval. The client never starts a task worker directly. Store new research receipts separately from the original ASK state; never silently install global skills.
4. Preserve explicit --new, same-job --resume, local waiting deadlines, read-only late retrieval, 200 KiB inline report limit and 200/220 retention behavior. Treat task completion and file delivery as separate states.
5. Test source-derived ASK regressions, cross-device ownership, retries/lost acknowledgements, stale coordination, expired deadlines, missing reports, pending large uploads and file tampering. Keep signing and archive tests.
6. Run real Codex and Claude research. Require gateway search/fetch calls, primary-source citations, report.md, successful task completion and checksum-verified return. Verify no setup popup is added and no duplicate worker/job starts.
7. Deploy additive server changes before publishing the skill; verify the exact short install command and one discovered research skill. Record results and cleanup only test-owned resources.

## References checked
- Skills CLI installation/discovery: https://github.com/vercel-labs/skills/blob/main/docs/commands.md
- Python detached subprocess/session behavior: https://docs.python.org/3/library/subprocess.html
- Python atomic receipt replacement: https://docs.python.org/3/library/os.html#os.replace
- MCP transports and authentication boundaries: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- Abliteration native tool loops: https://docs.abliteration.ai/capabilities/tool-calling/openai-responses and https://docs.abliteration.ai/capabilities/tool-calling/anthropic-messages

## Boundaries
Research page contents are evidence, not new instructions. Signatures authenticate reviewed setup assets, not arbitrary model behavior. Existing unattended permissions remain disclosed. A full machine reboot is outside this test run. Prior setup-only live tests do not establish research quality; this migration needs real gateway-backed research evidence.

## Implemented result
- One visible `research` skill with ASK-derived ask.py and signed launch.py.
- Device-authenticated research submit/status/lookup/download routes deployed.
- Real Research Gateway calls and cited report delivery verified through Codex and Claude.
- New starts check prerequisites before creating job state; status and result remain usable without a working CLI.
- Existing incompatible legacy connections fail clearly instead of modifying them or silently changing model providers. An explicit isolated state directory is available for testing.
- Source ASK, global skills and old installations preserved. Exact public installation is checked after publication.
