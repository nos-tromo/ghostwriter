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
| `GHOSTWRITER_VISION` | `on` | On, users can attach images, which go to the chat model with their message, and the model is shown the pages that have no text. `off`, or a workbench off or before 0.7.0 (no `view_file`), behaves as before: no picture, nor a note of one, goes to the model. Only an Ollama model is checked: on `INFERENCE_PROVIDER=ollama`, Ghostwriter reads the chat model's capabilities once at start (`POST /api/show` on the router's host) and turns images off when `vision` is missing; it can only turn them off, and an answer it cannot read changes nothing. On vLLM nothing is checked: set `off` for a text-only model, or every round that carries pictures is refused by the router and goes again without them, telling the user `images_dropped`. |
| `GHOSTWRITER_MAX_IMAGES` | `8` | The most images one request carries (1–32). Must not exceed vllm-service's `CHAT_LIMIT_MM_PER_PROMPT`. |
| `GHOSTWRITER_IMAGE_CONTEXT_TOKENS` | unset | Tokens one image costs the model (1–10,000). Unset: one per 32x32 pixels (Qwen). A number: a fixed cost per image (280 for gemma); pages then go as two strips. |
| `WHISPER_MODEL` | `openai/whisper-large-v3` | The dictation model. |
| `RESPONSE_LANGUAGE` | `en` | The SPA's language, `en` or `de`; any other value is `en`. |
| `GHOSTWRITER_SPEECH_INPUT` | `on` | `off` removes `/api/transcribe` (404) and the microphone. |
| `WORKBENCH_API_KEY` | unset | workbench's `GHOSTWRITER_WORKBENCH_KEY`. Unset: no tools and no attachments, chats work. Shorter than 32 characters stops startup. Rotating it renames every chat's folder (the pseudonyms are keyed with it), so earlier files read as expired. |
| `WORKBENCH_API_BASE` | `http://workbench-api:8000` | workbench on `workbench-net`. |
| `WORKBENCH_TIMEOUT` | `150` | Seconds one tool call may take. A call starts only while this much of the message's wall is left. |
| `NEXTEXT_API_KEY` | unset | Ghostwriter's service token at Nextext: the same value as its entry in Nextext's `NEXTEXT_SERVICE_TOKENS` (`ghostwriter=<token>`); `make env` fills both in. Unset: video and audio are not offered, and nothing else changes. Shorter than 32 characters stops startup. One entry per Ghostwriter: Nextext keeps a service's jobs under its entry's name (`service:ghostwriter`), not its token, and a start deletes every job of that owner, so a second Ghostwriter needs an entry of its own name and its own token; under the same name, even with another token, it would delete this one's jobs. A key Nextext refuses (401 or 403) logs one error naming `NEXTEXT_API_KEY`, never its value, and clips are not offered until a list of the probe's with the key answers 200. |
| `NEXTEXT_API_BASE` | `http://nextext-backend:8000/api/v1` (set by compose) | Nextext's API on `inference-net`. Empty turns video and audio off. Clips also need workbench 0.9.0 or later; against an earlier workbench they are not offered either. |
| `NEXTEXT_TIMEOUT` | `900` | Seconds one clip may take at Nextext, 60–86,400: the upload, the wait behind other jobs, the transcription and the downloads. Nextext runs one job at a time (its `NEXTEXT_JOB_CONCURRENCY`, 1), so a clip queues behind docint's ingest within this budget. Whisper on CPU runs at about real time, so a CPU-only host needs a larger budget for long recordings; 900 s suits a GPU. |
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
| `TOOL_RESULT_CHARS` | 16,896 | Characters of a result the model reads: workbench's 16 KB default plus 512 for the null fields its size check leaves out, so a default answer is never cut; a `read_document` answer is cut by whole parts (one is trimmed only when it is the only one that fits), keeping `next_part`, and its `hint` is made to agree with it |
| `HISTORY_ARGUMENT_CHARS` / `HISTORY_RESULT_CHARS` | 2,000 / 2,000 | A replayed call's arguments and result |
| `MAX_RESULTS_PER_MESSAGE` | 20 | Cards a message shows |
| `INLINE_IMAGE_BYTES` | 10 MiB | The largest image shown inline (png, jpeg, gif, webp) |
| `MAX_RECORDING_S` / `MAX_RECORDING_BYTES` | 120 s / 10 MiB | A recording (`inference.py`) |
| `MAX_ATTACHMENT_BYTES` | 50 MiB | An attachment, a video or audio clip included (`workbench.py`) |
| `MAX_RENDITION_BYTES` | 768 KiB | A render of a page or picture fetched must be smaller (`workbench.py`) |
| `MAX_IMAGE_BYTES` / `MAX_IMAGES_BYTES` / `MAX_TOOL_IMAGES` | 1 MiB / 3 MiB / 8 | The pictures kept from one tool answer: each, all together, and how many (`workbench.py`) |
| `PREPARE_TIMEOUT_S` | 390 | Seconds each read of a prepare's answer may wait: workbench's 120 s of queue, two job walls of 120 s (an Office file is converted, then drawn) and 30 s (`workbench.py`). A prepare whose file another call is still making waits for that call first, up to 120 s more, and then usually finds the manifest made; the rare one that waits it out and then converts the file itself can pass 390 s and is cut, and the file reads as text |
| `PREPARE_RETRY_S` | 10 | Pause before a prepare workbench answered busy (503) goes once more; one that timed out is not tried again (`processing.py`) |
| `STRIPS_TIMEOUT_S` | 60 | The same for the second prepare that cuts a large picture's strips; it is not tried again (`processing.py`) |
| `STRIP_LONG_SIDE` / `TOKEN_SIDE` | 1,024 px / 32 px | At a fixed image cost, a page or picture whose long side is over the first goes as two strips; with none set, an image costs a token per started square of the second (`agent/images.py`) |
| `RENDITION_TIMEOUT_S` | 30 | Seconds the renders of one fetch get together (`workbench.py`) |
| `PREPARE_SLOTS` | 2 | Attachments prepared at once, in the one process (`processing.py`) |
| `BACKGROUND_SLOTS` | 1 | Office files prepared at once in the background, within `PREPARE_SLOTS`, so a picture's prepare always has a slot here (`processing.py`). From workbench 0.8.1 a conversion has a gate of its own too, so Send never waits for an Office file while workbench's `GHOSTWRITER_WORKBENCH_SLOTS` is 3 or more, its default |
| `MEDIA_SLOTS` | 2 | Clips at Nextext at once, in the one process; a clip there holds none of the `PREPARE_SLOTS`, so a picture is ready as fast as without clips (`processing.py`) |
| `POLL_S` / `POLL_SLOW_S` / `POLL_SLOW_AFTER_S` | 2 s / 5 s / 60 s | Between two polls of a clip's job at Nextext, and the age of the job from which the longer wait holds (`processing.py`) |
| `POLL_TOLERATED` | 3 | Failed polls in a row a clip rides out within its budget; a fourth in a row fails it `nextext_unavailable`. A job that is gone fails it at once (`processing.py`) |
| `START_WAIT_S` | 120 s | The longest the start reconcile waits for Nextext's probe. A clip waits for that reconcile this and `RECONCILE_EXTRA_S` longer, and 5 s of slack, and past it makes no job (`processing.py`) |
| `RECONCILE_EXTRA_S` | 60 s | The longest the start reconcile takes once the probe has passed, and the sweep's reconcile in all; also what the sweep's grace adds to `NEXTEXT_TIMEOUT` (`processing.py`, `retention.py`) |
| `ORPHAN_GRACE_S` / `ORPHAN_RECONCILE_S` | 180 s / 240 s | The reconcile after a create whose answer was lost, or a clip's own delete that failed: it runs 240 s after it and spares jobs younger than 180 s (`processing.py`) |
| `ARTIFACT_CAPS` | 8 MiB / 32 MiB | The transcript (`docint.jsonl`) and the frames (`keyframes.zip`) of a clip; one over its cap is left out (`nextext.py`) |
| `JOB_OPTIONS["keyframes_max"]` / `["keyframes_per_minute"]` | 19 / 20 | The frames of a video: at most 19, since workbench keeps 19 (`MEDIA_FRAMES_MAX`), at 20 a minute; Nextext's own default of 4 a minute would give a 30 s clip 2 frames (`nextext.py`) |
| `CREATE_READ_S` / `CALL_S` / `ARTIFACT_READ_S` / `READY_S` / `HEALTH_S` | 120 s / 10 s / 60 s / 75 s / 5 s | Each wait on Nextext: its answer to a create once the clip is sent, a list or a delete, one read of an artifact, the readiness probe (`GET /ready`, above the 60 s Nextext gives its own transcription of a moment of silence, which takes 10 to 20 s on a CPU Whisper, so a slow Whisper is not read as down), and the liveness probe (`GET /health`, asked only when `/ready` answers 404, a Nextext before 1.22). The probe asks again after 5 s, doubling to 300 s, while Nextext does not pass it (`nextext.py`) |
| `DERIVED_TIMEOUT_S` | 150 | Seconds workbench is waited for once it holds a whole artifact: the clip's lock for up to 120 s, and the work (`workbench.py`) |
| `MAX_WAIT_S` | 20 | The longest `?wait=` of an attachment's status long poll (`processing.py`) |
| `MAX_PENDING` | 10 | Unexpired attachments waiting in a chat (`upload.py`); 10 per message too (`api/messages.py`) |
| `MAX_MESSAGE_CHARS` | 20,000 | Characters of a message, after stripping (`api/messages.py`) |
