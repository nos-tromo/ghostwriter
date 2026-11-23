# Data flow

The record of processing: what Ghostwriter keeps, for how long, where data
goes, and who can read it.

## What is stored

SQLite on the `ghostwriter-data` volume (`ghostwriter/models.py`).

| Table | Columns | Kept |
|---|---|---|
| `chats` | id, owner (the account), title (the first message's first line, or the owner's), created_at, updated_at, expires_at | until `expires_at` (90 days after the start, [retention.md](retention.md)) or the owner's delete |
| `messages` | id, chat_id, position, role (`user`, `assistant`), content (the text sent, and the answer shown), created_at | with the chat |
| `tool_steps` | id, message_id, position, round, call_id, tool, arguments (the model's, code included), result (what the model read back, at most 12,000 characters), status, created_at, content_expires_at | `arguments` and `result` until `content_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `attachments` | id, chat_id, message_id, workbench_file_id, path, name, size, mime, kind (`document`, `image`, `media`), status (`processing`, `ready`, `failed`), render (the manifest: the page count, the pages without text and the sizes of the renders; for a clip its frame count, its length and whether it has a transcript), failure (a token), created_at | with the chat, or until its owner takes it back before sending it; `render` until the file period (7 days), then emptied ([retention.md](retention.md)) |
| `results` | id, message_id, position, kind (`image`, `table`, `file`), workbench_file_id, path, name, mime, size, preview (a header and at most 5 rows), created_at, preview_expires_at | `preview` until `preview_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `file_marks` | chat_id, created_at: when the chat's workbench folder may have got a file (an attachment, a hand-over, a tool call, a step or a result) | with the chat |
| `retention_periods` | the chat period's state, and the file period the content deadlines were set under | no personal data |

## What is never stored

- **File bytes.** Uploads stream through to workbench and downloads stream back.
- **Clip bytes and Nextext's artifacts.** A clip streams from workbench to Nextext, and its transcript and
  frames stream from Nextext into workbench, chunk by chunk through memory: none is spooled to disk, read
  whole or kept here. The processor never holds a transcript line, and none is in a log or the audit. Once
  the model reads a clip with `read_document`, what it read is a tool result like any other
  (`tool_steps.result`, at most 12,000 characters, until its content deadline within
  `WORKBENCH_RETENTION_DAYS`).
- **Image bytes and data URLs.** The renders of a message's pictures and of the
  pages or pictures its tools showed are fetched from workbench and held in
  memory for that one message. A data URL exists only in the request to the
  router: never in the database, a log or the audit.
- **Recordings and transcripts.** A recording is held in memory for its call;
  the transcript goes back to the composer only, and becomes a message only if
  the user sends it.
- **The thinking.** It is never shown, stored, replayed or recorded. Templates
  that always think, or a round cut at `max_tokens` inside a think block, need
  vllm-service's `CHAT_REASONING_PARSER`; otherwise the held reasoning is
  released as text, as in docint.
- **The purpose.** Only in the audit events and in the headers to workbench and
  to Nextext (every call: create, poll, artifacts, delete), which record it in
  their own audit too. The browser keeps it for the tab's session.

Logs hold ids, counts, statuses and tokens only.

## Where data goes

| To | What | Under |
|---|---|---|
| The router, chat (`OPENAI_API_BASE`) | the system prompt (the date and, with tools, the chat's folder and the file period; when workbench offers `write_presentation`, also a fixed rule on presentations; while the model sees, also how to look at files and at the decks it builds; these rules hold no data), the message with its attachments by name, path and size (a clip also by its length, its frame count and whether it has a transcript), the turn's tool calls and results, and earlier turns within the budget; while the model sees, also the attached pictures and the pages or pictures a tool showed, as `image_url` data URLs of workbench's renders (workbench removes their metadata), held for one message, and the earlier turns' pictures while the budget allows | `X-Cache-Scope` (an HMAC of the account under a key that never leaves the process, new at each start), `X-Request-ID` |
| The router, speech-to-text | the recording | the same |
| workbench `/mcp` | tool arguments; results back, with the pages or pictures a tool shows | `X-User-Id` and `X-Session-Id`: HMAC-SHA256 of the account and of the chat id under `WORKBENCH_API_KEY`; the purpose (or `ghostwriter chat`), `X-Request-ID`; `X-Accept-Images: 1` on the discovery of the listing for images, and on a turn's calls only while it sees |
| workbench `/files/upload`, `/files/view` | an attachment's bytes, a download's bytes | the same |
| workbench `POST /files/prepare` | the path of a picture, a PDF or an Office file in the chat's folder, and with a fixed image cost `strips=2` for a large picture; the manifest back | the same |
| workbench `GET /files/view?rendition=` | the number of a page or picture; its render back, for a message's model call or for the owner's preview | the same |
| Ollama `POST /api/show`, on the router's host | the chat model's name only, once at start; only with `INFERENCE_PROVIDER=ollama` and pictures on | the router's key |
| Nextext `POST /jobs` (`NEXTEXT_API_BASE`) | a video or audio clip, streamed from workbench, under the name `media.<ext>` and never the user's file name, with fixed options (frames at 20 a minute, at most 19; no captions, no summary); only while Nextext is set | `Authorization: Bearer <NEXTEXT_API_KEY>` (the service `ghostwriter`), the purpose (or `ghostwriter chat`), `X-Request-ID` |
| Nextext `GET /jobs`, `GET /jobs/{id}/artifacts/{name}` | the job's status from the list; the transcript (`docint.jsonl`) and the frames (`keyframes.zip`) back, streamed | the same |
| Nextext `DELETE /jobs/{id}` | the job, as soon as its artifacts are in workbench, it failed or its budget was spent; also every job of Ghostwriter's no clip owns ([retention.md](retention.md)) | the same |
| workbench `POST /files/derived` | the clip's transcript and frames, streamed from Nextext, to be derived files of the clip | `X-User-Id` and `X-Session-Id`, as for the other workbench calls |
| workbench `DELETE /files/upload` | the path of an attachment not yet sent, which its owner takes back | the same |
| workbench `DELETE /files/chat` | the chat's folder, on the owner's delete, the sweep, or an upload into a chat deleted meanwhile | the same; the sweep as the system |
| obs-plane | the audit chain, references only ([audit.md](audit.md)), kept by obs-plane | the account as actor |

No account name and no chat id reach the router, workbench, Ollama or Nextext: Nextext sees one service,
`ghostwriter`, and the clip as `media.<ext>`.

workbench 0.7.0 lists `view_file` and returns images only to a call that carries
`X-Accept-Images: 1`; without it, it answers as 0.6.0 did, with no `view_file`,
no images and the old `read_document` text. Ghostwriter 0.3.0 discovers each
listing with its own header and caches them apart. A turn sees while pictures
are on, the model takes them and the listing for images holds `view_file`: it is
then offered that listing, and its calls carry the header. A turn without sight
is offered the listing without the header, whose `read_document` promises no
image, and its calls carry none; so does every round after the model refused
pictures.

## A picture, a PDF or an Office file, after its upload

While pictures are offered, a picture or a PDF is stored `processing` and its
upload answers at once. The attachment processor (`ghostwriter/processing.py`,
the one process, at most two prepares at a time) has workbench prepare it
(`POST /files/prepare`, idempotent) and keeps its manifest as `render`: the
attachment is then `ready`. A picture whose prepare fails is `failed`, with a
token, and is named to the model as unreadable; a PDF is `ready` without a manifest
and reads as text. A message that names a `processing` attachment is answered
409. One found `processing` at start, whose job ended with the last process, is
prepared again.

A Word, PowerPoint or OpenDocument text file (`.docx .dotx .pptx .potx .odt`) is
`ready` as soon as its upload answers, and Send never waits for it. While
pictures are offered, it is also prepared after that, in the background and one
at a time; without them it is `ready` and never prepared, as before 0.5.0. Until
its manifest is kept it reads as text. Then its line names the pages or slides,
and the model can view them, while the turn sees. The manifest keeps its `unit`
(`page` or `slide`). workbench 0.8.0 converts the file once and keeps the PDF as
its own derived file, deleted with the file.

Four things of workbench 0.8.1 matter here (0.8.0 converts and draws the file,
but has none of them):

- **Conversions have a gate of their own.** workbench converts one Office file
  of Ghostwriter's at a time, over all its users, apart from the gate where
  pictures and PDFs are prepared. A picture's or a PDF's prepare is therefore
  admitted while a deck converts, and Send waits for the picture only. This
  holds while workbench's `GHOSTWRITER_WORKBENCH_SLOTS` is 3 or more, its
  default: a conversion, a picture's prepare and a tool call each take one. With
  fewer, a prepare or a tool call can queue behind a conversion.
- **A tool call waits out the prepare.** A call that needs a deck's slides while
  its background prepare runs (`view_file`, or the pictures of `read_document`)
  waits for it in workbench, up to 120 s, and then shows the slides. No second
  conversion runs. 120 s is less than `WORKBENCH_TIMEOUT` (150 s). Past it,
  workbench answers busy and the call is retried once, as every busy call is.
- **A refusal is remembered.** A file workbench refuses as unreadable or
  unsupported (422) stays `ready` without a manifest, and Ghostwriter does not
  ask for its prepare again. workbench answers every later view or picture read
  of that file with the same refusal at once, with no new conversion, until it
  restarts or the file is deleted. A refusal that may pass (a timeout, a busy
  answer) is not remembered. `read_document` still reads the text.
- **An attachment is taken back before it is sent.** The X on its tile cancels
  its job and has workbench delete its file and renders (`DELETE
  /files/upload`), and then its row goes; the model no longer finds it in
  `list_files`. Next to workbench 0.8.0 the take-back answers 502 (workbench
  answers 405), and the file stays.

A background prepare cut by a restart is not run again: only a `processing`
attachment is. Next to workbench 0.7.0 the prepare answers kind `none`, and the
file reads as text, as before.

- **The wait.** The SPA follows the status with a long poll: `GET
  /api/chats/{id}/attachments/{attachment_id}?wait=` answers when the status
  settles, or after at most 20 s, and keeps nothing. After a `processing`
  answer the SPA asks again no sooner than 500 ms after its request began. A
  prepare is asked once more after a busy workbench (503), never after one that
  timed out.
- **The preview.** `GET …/preview?page=` streams one render from workbench's
  view route, with its hardening headers, for a tile or the lightbox. Nothing is
  kept or cached here, and each call is a `disclose` event.
- **Strips.** With `GHOSTWRITER_IMAGE_CONTEXT_TOKENS` set, every image costs the
  same, so one whose long side is over 1024 px goes as two strips to keep its
  detail. After the manifest, the processor asks `prepare(..., strips=2)` once,
  so the first turn finds them cut; if that fails (60 s a read, never retried),
  the picture stays `ready` and workbench cuts the strips as the turn views it.
  Pages go as strips at view time.
- **Without workbench.** With pictures on and workbench off, unreachable or
  before 0.7.0 (no `view_file`), the turn has no sight: no picture goes to the
  model, nor a note that `view_file` shows one, and the attachment line says the
  picture is not shown.

## A video or audio clip, after its upload

With Nextext not set, or against a workbench before 0.9.0, a clip is refused 415 as any
type not taken, and Ghostwriter behaves as 0.5.0 did.

**The offer.** Clips (`.mp4 .mov .m4v .webm .mkv .mp3 .m4a .wav .ogg .opus .flac`) are offered only while
all three hold: `NEXTEXT_API_KEY` and `NEXTEXT_API_BASE` are set; a background probe of Nextext's `/ready`
(HTTP 200 only; start-up never waits for it, and after a failure it asks again after 5 s, doubling to 300 s,
or at once when a clip's job finds Nextext down; after Nextext refused the key, it also lists the jobs with the
key, and only a 200 counts) says it can transcribe; and workbench's discovery names 0.9.0 or
later. `GET /api/config` then lists them as `media_types`, with `media: true`. Nextext 1.22 answers `/ready`
`200` only when it has just transcribed a moment of silence through its Whisper path (it keeps the answer
for 30 s, and gives the probe 10 s, so Ghostwriter waits 15 s) and `503` otherwise, so while its Whisper is down no
clip is offered; `/health` stays `200` then, and says only that Nextext is up. A `404` is a Nextext before 1.22,
without the route: `/health` is asked instead, and then the probe sees that Nextext is up, not that its Whisper
is, so a clip fails at Nextext within seconds. `/ready` is asked first on every probe, so an upgraded Nextext is
picked up without a restart. A clip whose job fails `inference_unavailable` (Nextext 1.22 and later: its
Whisper or its chat model did not answer) turns clips off at once, until the probe passes again. Clips need no
vision: the transcript is text. The frames reach the model only through `view_file`, so only while it sees.
The SPA reads `/api/config` once; a clip the server refuses 415 reads it again, so the picker drops the clip
types. Nextext coming up after the page loaded needs a reload.

**The round trip.** A clip is stored `processing` (kind `media`, within the 50 MiB cap), its upload answers at
once, and the processor takes it on `MEDIA_SLOTS` of its own (2), never a prepare slot, so a picture attached
meanwhile is ready within its usual prepare:

1. It waits for the start reconcile (below), at most `START_WAIT_S` and `RECONCILE_EXTRA_S` (120 s and 60 s) and
   a few seconds more. A gate that never opened fails the clip `nextext_unavailable`, and so does Nextext
   turned off since the upload; nothing is sent then, and no hand-over is recorded.
2. A `disclose` event (`nextext:transcribe`: its kind and size) is written. Unrecorded, nothing is sent
   (`audit`).
3. The clip is fetched from workbench (`GET /files/view`) and streamed, chunk by chunk, into `POST /jobs` at
   Nextext as `media.<ext>`: one job for one clip.
4. The job is polled through `GET /jobs`, every 2 s and every 5 s after the first 60 s, never through `GET
   /jobs/{id}`, whose answer holds the whole transcript, which Ghostwriter must not hold. The whole round trip
   (the upload, the queue behind other jobs, the run and the downloads) is within `NEXTEXT_TIMEOUT` (900 s),
   counted from the recorded hand-over. A job the list does not name is gone (Nextext restarted, its jobs
   living in memory), and so is one whose list item does not read: `nextext_failed`, at the next poll. A
   poll that fails (a list that timed out, a connection reset) is ridden out, up to 3 in a row; a fourth in a
   row fails the clip `nextext_unavailable`.
5. A completed job (also one `skipped` for `vad_no_speech`, which is no failure: the clip is ready and its line
   says "no speech") is recorded as a hand-over to workbench (`workbench:prepare`). Then each artifact streams
   from Nextext's `GET /jobs/{id}/artifacts/{name}` straight into workbench's `POST /files/derived`, as a
   derived file of the clip: the transcript `docint.jsonl` (at most 8 MiB) and the frames `keyframes.zip`
   (32 MiB). An artifact Nextext did not make (404: no transcript for a clip without speech, no frames for
   audio) or one over its cap (`artifact_too_large`, logged) is left out, and the clip is ready with what
   remains. A completed job that was not skipped and served neither artifact is polled once more and fails the
   clip `nextext_failed`, gone (Nextext restarted before the downloads) or still listed: a clip with speech
   always has a transcript. A workbench that answers busy (503: its lock on the clip, held by a prepare of the
   same clip) is asked once more after 10 s; still busy, the clip fails.
6. In a `finally` shielded from a cancel, the job is deleted at Nextext (a job it no longer holds counts as
   deleted) and the delete is recorded (`delete` / `chat.nextext`, the job id only). This also holds for a clip
   taken back, or whose chat is deleted, while it is at Nextext: it is the one record a cancelled job still
   writes. A delete Nextext refuses, or does not answer within 10 s, is followed by a reconcile four minutes
   later (below).
7. workbench prepares the clip (`POST /files/prepare`, on a prepare slot, asked once more after a busy answer)
   and its manifest is kept: the kind `media`, the unit `frame`, the frames (0 for audio), its length and
   whether it has a transcript. The clip is `ready`. A prepare refused as unreadable or unsupported (422)
   fails the clip with that token; one that timed out (`read_timeout`), with `timeout`.

A clip that cannot go through is `failed` with a token, and an `error` child under the hand-over whose step
failed ([audit.md](audit.md)): `nextext_unavailable` (Nextext does not answer, times out, fails with a 5xx,
refuses the key, or its job failed `inference_unavailable`; the probe is told, so clips are not offered until it
passes again, and after a refused key, which logs one error naming `NEXTEXT_API_KEY`, until a list of the probe's
with the key answers 200),
`nextext_failed` (the job failed for any other cause, was cancelled or interrupted, is gone, or served nothing),
`undecodable_media` (Nextext cannot decode the clip),
`timeout` (the budget was spent), or workbench's own token. Unlike a document, a clip is never `ready` without
its round trip: it has nothing to read. A video with no sound track at all fails `undecodable_media` only at a
Nextext before 1.22, and then gets no frames either; from 1.22 Nextext skips it (`no_audio_stream`) and returns its
frames, so the clip is `ready` with no transcript.

**Orphaned jobs.** A job no clip owns is deleted by a reconcile, which lists Ghostwriter's jobs at Nextext and
records a count before it deletes any: at start, once Nextext passes its probe (every job listed, since no clip
of this process can own one yet, and each clip waits for it before it makes a job); four minutes after a create
whose answer was lost or a clip's own delete that failed (sparing jobs younger than 3 minutes); and in the daily
sweep (sparing jobs younger than `NEXTEXT_TIMEOUT` and 60 s). Each spares the jobs a clip owned when its list went
out or owns when it answers. One reconcile runs at a time; the sweep's is bounded at 60 s and never stops the
chats' sweep. The rules are in [retention.md](retention.md).

**Taken back, deleted, restarted.** Nextext's `DELETE` of a running job answers 204 and drops it from its list
at once, but its pipeline runs on and keeps its one queue slot, and Nextext removes the job's upload when the
pipeline ends. A clip taken back, whose chat is deleted, or whose budget is spent mid-transcription therefore
stays at Nextext until its job ends there ([retention.md](retention.md)). A clip found `processing` at start, or requeued after a
failed delete or take-back, runs the whole round trip again, and workbench replaces its derived files.

**What the model reads.** The attachment line says `video 02:14, 8 frames; transcript`, `audio 31:07; transcript`
or `video 00:40, 3 frames; no speech`; while the turn does not see, a video's line adds that its frames are "not shown
to the model". It says "no speech" whenever the manifest holds no transcript: also for a transcript left out past its
cap or one workbench could not read, since the manifest keeps workbench's facts, not why Nextext's transcript is
missing. The model reads the transcript with `read_document` and looks at the frames with `view_file`. workbench
0.9 leaves a clip out of `run_python` and `search_files` jobs, so these two tools are the only way to it. The
transcript is text in a tool result, and so goes to the router like any other. workbench's own
`docs/data-flow.md` (0.9.0) describes the other end of the derived files.

## Who can read it

The owner only. Every request first resolves the chat by the request's own
account; another owner's chat answers 404, and there is no admin path or
`owner` parameter. Anyone with root on the host can read the volume.

## Residual

A tool call cut short by the message's wall time may still leave outputs in the
chat's folder without a card, and so may outputs past a message's 20 cards and
those of a regenerated answer. `list_files` shows them; they go with the chat,
or after 7 days.
