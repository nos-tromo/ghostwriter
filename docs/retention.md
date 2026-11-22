# Retention

| What | Kept | How it goes |
|---|---|---|
| A chat: its messages, steps, attachments and results | `GHOSTWRITER_CHAT_RETENTION_DAYS` (90) after it started | the daily sweep deletes it |
| Tool contents (a step's arguments and result) and table previews | `WORKBENCH_RETENTION_DAYS` (7), or less: never past a file they may come from | read as expired at once; the daily sweep empties them |
| An attachment's manifest (its page count, the pages without text, the sizes of its renders) | `WORKBENCH_RETENTION_DAYS` (7) after the attachment was made | the daily sweep empties it; the attachment itself stays with its chat |
| The files, and the renders workbench made of them | 7 days, in workbench | workbench's own retention; here their cards read "expired" and a download or a preview answers 410 |

Using or continuing a chat never extends its period. The sweep runs a minute
after start, then daily (`ghostwriter/retention.py`). A line under the composer
and a line under attached files state the periods, leaving out the chat period
while chat retention is off and the file period while attachments are off.

## The chat period and its notice

Each start puts the configured period in force (docint's rule, `ghostwriter/retention_rule.py`):

| Change | Effect |
|---|---|
| First start, or on again after `off` | nothing is deleted for `RETENTION_NOTICE_DAYS` (30) |
| A longer period | applies at once (or when a running notice ends) |
| A shorter period | the notice holds only what it makes due sooner; the period before keeps deleting meanwhile |

Example: 90 days shortened to 60 on 1 March. A chat started 70 days before goes
on 21 March, its old date; one started 50 days before on 31 March, the end of
the notice, not on 11 March; one started 20 days before on 10 April, its new date.

## Tool contents and previews

They expire `WORKBENCH_RETENTION_DAYS` after they were made, or with the
earliest file that may still be in the chat's folder then, if that goes first,
so nothing derived from a file outlives it. Example: a file on day 0; a step on
day 6 goes on day 7, a step on day 10 on day 17. Emptied, a step reads "Details
deleted after 7 days". A changed file period applies at the next start to what
has not expired yet, without a notice; nothing expired comes back.

## Manifests and renders

An attachment's manifest is derived from its file, so it goes with the file's
period: the sweep records it, then empties it, and the attachment stays. The
renders themselves (the pages and pictures the model sees) are workbench's
derived files and are deleted with their source there. Ghostwriter keeps none:
the processing and preview routes hold nothing (the preview streams a render
through), and a picture's bytes live in memory for the one message that sends it.
After the period an earlier picture is read as expired: the follow-up's line says
so and nothing is fetched.

## The delete order

1. Hold the chat's turn: while an answer runs, the owner's delete gets 409 and
   the sweep leaves the chat to its next run.
2. Record the `delete` event.
3. Cancel the jobs that prepare the chat's attachments, then delete the chat's
   folder in workbench (`DELETE /files/chat`), and record what went. A chat that
   never used workbench is not sent. A delete that fails after the cancel keeps the
   chat, and its `processing` attachments are prepared again.
4. Delete the chat's rows. The owner's delete stops when workbench fails (502,
   the chat is kept); the sweep goes on, as the chat is due and workbench's own
   period takes its files.

Tool contents are recorded, then emptied. Deleted rows are overwritten
(`secure_delete`), and each sweep ends with a write-ahead-log checkpoint.

A file attached by mistake is taken back before it is sent: the X on its tile
deletes it in workbench with its renders (workbench 0.8.1), and then its row; a
job preparing it is cancelled. A sent file stays with its message until the chat
is deleted or its 7 days are over.

## The dry run

`make retention-dry-run` asks the running backend what a sweep would delete
now, in counts, ages and dates only: the chats, and the tool steps, previews
and manifests whose period has passed; `CHAT_DAYS=n` previews another chat
period, with the notice it would get. It records and changes nothing.
