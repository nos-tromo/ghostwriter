# Configuration

Every variable of `.env.example`. A blank value counts as unset; a value that
cannot work stops startup and names the variable, never its value
(`ghostwriter/config.py`).

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | `dummy` | The router's key; the same value as vllm-service's. |
| `GHOSTWRITER_EDGE_SECRET` | unset | Set: `X-Auth-User` counts only with a matching `X-Edge-Secret`; shorter than 32 characters stops startup. Unset: the dev fallback, which trusts identity headers from any caller, with one warning. |
| `INFERENCE_NETWORK` | `inference-net` | The backend's network to the router. |
| `EDGE_NET` | `edge-net` | The frontend's network to the edge gateway. |
| `WORKBENCH_NET` | `workbench-net` | The backend's network to workbench. |
| `EXTRA_NO_PROXY` | empty | Appended to `NO_PROXY`; starts with a comma. |
| `INFERENCE_PROVIDER` | `vllm` | Must match the endpoint. On `vllm` every round sends `enable_thinking` with the composer's reasoning switch; other providers get no such field. |
| `OPENAI_API_BASE` | `http://vllm-router:4000/v1` | The OpenAI-compatible endpoint, with `/v1`. |
| `TEXT_MODEL` | `Qwen/Qwen3.8-27B-FP8` | The chat model. |
| `GHOSTWRITER_CONTEXT_TOKENS` | `40000` | The chat model's context: vllm-service's `CHAT_MAX_MODEL_LEN`. Each round's prompt budget follows from it (Caps per message); under 22,000 stops startup. Larger than the model's context, a long chat's round is refused by the router. |
| `GHOSTWRITER_VISION` | `on` | On, users can attach images, which go to the chat model with their message, and the model is shown the pages that have no text. `off`, a model without vision, or a workbench before 0.7.0 behaves as before. |
| `GHOSTWRITER_MAX_IMAGES` | `8` | The most images one request carries (1–32). Must not exceed vllm-service's `CHAT_LIMIT_MM_PER_PROMPT`. |
| `GHOSTWRITER_IMAGE_TOKENS` | unset | Tokens one image costs the model (1–10,000). Unset: one per 32x32 pixels (Qwen). A number: a fixed cost per image (280 for gemma); pages then go as two strips. |
| `WHISPER_MODEL` | `openai/whisper-large-v3` | The dictation model. |
| `RESPONSE_LANGUAGE` | `en` | The SPA's language, `en` or `de`; any other value is `en`. |
| `GHOSTWRITER_SPEECH_INPUT` | `on` | `off` removes `/api/transcribe` (404) and the microphone. |
| `WORKBENCH_API_KEY` | unset | workbench's `GHOSTWRITER_WORKBENCH_KEY`. Unset: no tools and no attachments, chats work. Shorter than 32 characters stops startup. Rotating it renames every chat's folder (the pseudonyms are keyed with it), so earlier files read as expired. |
| `WORKBENCH_API_BASE` | `http://workbench-api:8000` | workbench on `workbench-net`. |
| `WORKBENCH_TIMEOUT` | `150` | Seconds one tool call may take. A call starts only while this much of the message's wall is left. |
| `AUDIT_REQUIRE_PURPOSE` | empty | Audit actions that need a stated purpose (428 without one); an unknown action stops startup. |
| `GHOSTWRITER_CHAT_RETENTION_DAYS` | `90` | Days a chat is kept after it started (1–3650); `off` keeps chats, for development only. |
| `RETENTION_NOTICE_DAYS` | `30` | Days a new or shorter chat period waits before it deletes (0–365). |
| `WORKBENCH_RETENTION_DAYS` | `7` | workbench's file period (1–3650); tool contents and previews follow it. Must equal workbench's. |
| `GHOSTWRITER_DEFAULT_IDENTITY` | unset; `dev` in the dev overlay | The account for requests without a trusted identity, only while the edge secret is unset. |
| `GHOSTWRITER_FRONTEND_HOST_PORT` | `8504` | The dev overlay's SPA port. |
| `GHOSTWRITER_BACKEND_HOST_PORT` | `8004` | The dev overlay's backend port. |

Compose also sets `AUDIT_OUTPUT=container` (the audit chain on the container's
stdout). `LOG_LEVEL` (default `INFO`) sets the stderr log's level; it is not in
`.env.example`, and is read from `.env`, which compose passes in whole.

## Caps per message

They live in `ghostwriter/agent/limits.py`; the last rows name their own module.

| Cap | Value | Meaning |
|---|---|---|
| `CONTEXT_TOKENS` | 40,000 | The model's context while `GHOSTWRITER_CONTEXT_TOKENS` is unset |
| `MAX_ANSWER_TOKENS` / `REASONING_ANSWER_TOKENS` | 4,000 / 12,000 | A round's `max_tokens`, plain and reasoning; the thinking counts against the answer |
| `CONTEXT_MARGIN_TOKENS` | 2,000 | Kept free of prompt and answer: the estimate's error, the chat template |
| `PROMPT_TOKENS` / `REASONING_PROMPT_TOKENS` | 34,000 / 26,000 at 40k | A round's prompt, tool schemas included: the context less the answer and the margin (`prompt_tokens`) |
| `MIN_PROMPT_TOKENS` / `MIN_CONTEXT_TOKENS` | 8,000 / 22,000 | The least a reasoning prompt may hold, and so the smallest context the backend starts with |
| `CHARS_PER_TOKEN` | 3 | The estimate the budget is counted by |
| `MODEL_TIMEOUT_S` | 120 | Seconds each wait on the router may take, not the whole round |
| `MAX_TOOL_ROUNDS` / `MAX_CALLS_PER_ROUND` | 8 / 4 | Rounds offered tools; calls a round may run |
| `MESSAGE_WALL_S` | 300 | Seconds a message has for its tools |
| `TOOL_CUTOFF_S` | 10 | Below this much of the wall, a round gets no tools |
| `BUSY_RETRY_S` | 2 | Pause before a call workbench answered busy goes once more |
| `TOOL_RESULT_CHARS` | 12,000 | Characters of a result the model reads |
| `HISTORY_ARGUMENT_CHARS` / `HISTORY_RESULT_CHARS` | 2,000 / 2,000 | A replayed call's arguments and result |
| `MAX_RESULTS_PER_MESSAGE` | 20 | Cards a message shows |
| `INLINE_IMAGE_BYTES` | 10 MiB | The largest image shown inline (png, jpeg, gif, webp) |
| `MAX_RECORDING_S` / `MAX_RECORDING_BYTES` | 120 s / 10 MiB | A recording (`inference.py`) |
| `MAX_ATTACHMENT_BYTES` | 50 MiB | An attachment (`workbench.py`) |
| `MAX_PENDING` | 10 | Unexpired attachments waiting in a chat (`upload.py`); 10 per message too (`api/messages.py`) |
| `MAX_MESSAGE_CHARS` | 20,000 | Characters of a message, after stripping (`api/messages.py`) |
