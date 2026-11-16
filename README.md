# Ghostwriter

The federation's chat assistant. Talk to it, or attach a document and
download the result in the chat.

```bash
cp .env.example .env    # then set OPENAI_API_KEY
make network volumes
make build && make up   # production shape; `make up-dev` publishes ports
```

`make help` lists every target. The design record is `CLAUDE.md`.
