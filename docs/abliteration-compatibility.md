# Abliteration integration findings

Reviewed 2026-10-23. Model: `abliterated-model-large-flash`. Keep documentation claims separate from observed tests.

## Tool execution

- Codex uses `/v1/responses`, with flat function definitions and `function_call_output` results. Claude uses `/v1/messages`, with `input_schema`, `tool_use`, and `tool_result`. The local CLI executes the commands; the model requests them. These protocols support our runner fetching instructions and using ordinary shell commands to install the helper.
- Our live API probe accepted Codex's standard function definitions (HTTP 200), but rejected its grammar-format custom patch tool (HTTP 400 `invalid_tools`). This is a specific compatibility issue, not evidence that shell tools are unsupported.
- For this setup runner, use a Codex catalog with `apply_patch_tool_type: null` and `multi_agent_version: null`. Shell tools are sufficient. Namespace support has not been independently established. No tool translation service is needed for this approach.
- Codex 0.163.0 rejects the catalog value `apply_patch_tool_type: "function"` before contacting the API. It accepts `freeform`, but that sends the grammar tool rejected above. Do not confuse catalog configuration with API function schemas.

Sources: [Responses tools](https://docs.abliteration.ai/capabilities/tool-calling/openai-responses), [Messages tools](https://docs.abliteration.ai/capabilities/tool-calling/anthropic-messages), [compatibility matrix](https://docs.abliteration.ai/compatibility-matrix).

## Authentication and client configuration

- Direct Codex provider base: `https://api.abliteration.ai/v1`, `wire_api = "responses"`.
- Direct Claude base: `https://api.abliteration.ai`; recommended `ANTHROPIC_AUTH_TOKEN` sends a bearer token. Upstream also accepts `x-api-key`, but our server proxy expects bearer authorization. Our runner uses a temporary server credential, never the provider key.
- Their Claude guide explains that unknown model names can default to a smaller client context window. The chosen Flash model's context settings should be configured and tested explicitly; a successful request alone does not verify context accounting.
- Their Codex guide includes `--approval-mode auto`; do not copy that flag without checking the installed CLI. We use locally verified CLI arguments.

Sources: [Codex guide](https://docs.abliteration.ai/integrations/codex), [Claude guide](https://docs.abliteration.ai/integrations/claude-code).

## Reliability and long runs

- Streaming is supported on both protocols. A stream can report an error after HTTP 200; status code alone is not completion. Track the final protocol event and CLI outcome.
- Flash supports reasoning off, low, high and max; default is high. Its `medium` and `xhigh` aliases map to high. Higher reasoning is not a guarantee of successful installation.
- Cache reuse is automatic and best effort. Keep stable instructions/tool definitions early in the prompt. Routing hints improve reuse, but cache is not durable conversation storage.
- Rate limits apply across keys, projects and the organization. Ten PCs sharing one key share its limits. Respect retry guidance for temporary failures; do not repeatedly retry malformed tools or invalid credentials unchanged.
- Responses documentation is ambiguous: the endpoint is titled stateless, while the tool guide uses `previous_response_id` and the reference lists that field. We have not verified stored continuation. Continue with explicit conversation history and do not add a dependency on provider-side stored state.

Sources: [streaming](https://docs.abliteration.ai/capabilities/streaming), [errors](https://docs.abliteration.ai/api/errors), [reasoning](https://docs.abliteration.ai/capabilities/thinking), [caching](https://docs.abliteration.ai/capabilities/prompt-caching), [limits](https://docs.abliteration.ai/api/rate-limits), [Responses reference](https://docs.abliteration.ai/api-reference/responses/create-a-stateless-openai-responses-compatible-response).

## Verification boundary

Both corrected real Codex and Claude runs completed helper installation on their first attempts, received two verified heartbeats, and exited. Each helper sent later heartbeats and launched a worker that returned a file. Codex also returned a 12 MiB file through the Rust upload queue; a download matched the original bytes and checksum. See live-acceptance.md. No full Mac reboot was performed.

The user selected the lowest reasoning setting: the proxy forces Responses reasoning.effort=none and Messages thinking={"type":"disabled"}, output_config.effort=low for this model. The documented thinking:false form was rejected by the live API (object required); the native disabled object passed the complete Claude setup and worker test.
