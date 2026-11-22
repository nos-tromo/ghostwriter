# Ghostwriter

The federation's chat assistant: ask, write and explain in a plain chat. It can
also work with files: attach a document, workbench processes it with its tools,
and the results come back in the chat as cards to download. A chat needs no
file, and chats work with workbench off or unreachable.

Pictures can be attached too: with a model that sees, they go to it with the
message, and a tool can show it the pages of a document. Word, PowerPoint and
OpenDocument text files are prepared too, in the background so that sending
never waits for them, and their pages and slides can then be seen (with
workbench 0.8.1).

A reasoning switch in the composer lets the model think before it answers; it
is off by default, and the thinking is never shown or stored.

```bash
cp .env.example .env    # then set OPENAI_API_KEY
make network            # once per host: inference-net, edge-net, workbench-net
make dev                # build, then start with host ports (dev account `dev`)
make up                 # production shape, no host ports; build or load the images first
```

`make dev` serves the SPA on `http://localhost:8504/ghostwriter/`. In
production users reach it through edge-plane at `https://<EDGE_HOST>/ghostwriter/`.
`make help` lists every target.

| Where | What |
|---|---|
| `CLAUDE.md` | the design record: where it fits, the module map, the invariants |
| `docs/configuration.md` | every variable, and the caps per message |
| `docs/audit.md` | what is recorded, by route |
| `docs/retention.md` | the chat and file periods, the notice, the dry run |
| `docs/data-flow.md` | the record of processing |
