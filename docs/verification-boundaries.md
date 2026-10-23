# Verification boundaries

Automated tests use temporary local state, fake credentials and mocked provider HTTP responses. They do not contact a billed model or prove how a real runner will recover from every network failure.

| Scenario | Automated evidence | What it does not prove |
| --- | --- | --- |
| Invalid release/checksum | Tampered artifact is rejected by release API; deterministic builder verifies hashes; server instructions require checksum verification before extraction | A live model correctly recovers from an intentionally corrupted download |
| Interrupted download | Server instructions use bounded curl retries and a private staging directory | A real interrupted helper-package download has been resumed or retried successfully |
| API unavailable | Launcher polling-error test cannot mark success and terminates its own child at deadline | Recovery from every real proxy/network outage |
| Credential expiry | Backend rejects expired/wrong-provider/previous-attempt model tokens | Provider billing/session behavior |
| Missing heartbeat | No readiness from runner exit or claimed completion; task gate stays closed; deadline becomes failed | Full machine reboot behavior |
| Heartbeat timing | Same instance, at least 15 seconds apart; >75-second gaps reset verification | Cryptographic proof of process identity beyond the authenticated device credential |
| Duplicate start | Local launcher locking and backend idempotency prevent duplicate active sessions; two-attempt maximum | Every possible filesystem or OS crash race |
| Provider selection | Mock native Responses/Messages requests route to Abliteration; no DeepSeek fallback; old DeepSeek thinking fields preserved | Upstream availability or model quality |
| Server instruction changes | New sessions pin updated text; existing sessions keep their original text | A fresh real model obeying every possible instruction change |

Live installation, worker execution, file-upload and cleanup results must be reported separately from these automated tests. Process termination/restart is not a full Mac reboot. Do not describe the interrupted-download case as live-verified without a recorded fault-injection run.

Provider compatibility correction: the live Messages endpoint rejected documented `thinking: false` with HTTP 400 (object required). The proxy uses the native `thinking: {"type":"disabled"}` shape and `output_config.effort: "low"`; this corrected object passed both the real Claude setup and subsequent worker task.
