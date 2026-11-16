### ghostwriter — build-host helpers.
###
### Ghostwriter is the federation's chat assistant: FastAPI backend + React
### SPA (nginx) frontend. It reaches the model over the shared
### `inference-net` and workbench, for attached documents, over
### `workbench-net`. Its chats live on the external `ghostwriter-data`
### volume, so `docker compose down -v` is safe; only `make nuke` removes it,
### after you type `ghostwriter` to confirm.
###
### The lifecycle targets (network/volumes/build/bundle/up/up-dev/stop/down/
### logs/pre-commit/verify/test) + the versioned image tag come from
### make/common.mk, vendored from nos-tromo/.github. Only ghostwriter-specific
### config, its own targets and the help text live here.

.DEFAULT_GOAL := help

REPO     := ghostwriter
NETWORKS := inference-net edge-net workbench-net
VOLUMES  := ghostwriter-data
include make/common.mk

.PHONY: help retention-dry-run nuke
help:
	@echo "ghostwriter — the federation's chat assistant: FastAPI backend + React SPA (nginx)."
	@echo
	@echo "  make network    create the external inference-net + edge-net + workbench-net"
	@echo "  make volumes    create the external ghostwriter-data volume"
	@echo "  make build      build backend + frontend images"
	@echo "  make bundle     ship images as a versioned .tar.gz (latest annotated release tag)"
	@echo "  make bundle-dev like 'bundle', but from the current working tree (dev/soak)"
	@echo "  make up         start backend + frontend, detached; no build (production shape, no host ports)"
	@echo "  make up-dev     like 'up' + host ports; detached, no build (run 'make build' first)"
	@echo "  make dev        build, then up-dev"
	@echo "  make stop       stop containers (keep them)"
	@echo "  make down       stop + remove containers (the data volume stays)"
	@echo "  make logs       tail combined logs"
	@echo "  make retention-dry-run [CHAT_DAYS=n]"
	@echo "                  what retention would delete now, in the running backend: counts and ages only;"
	@echo "                  nothing is recorded or deleted (CHAT_DAYS=n previews another chat period)"
	@echo "  make nuke       DESTROY the ghostwriter-data volume, every chat (type 'ghostwriter' to confirm)"
	@echo "  make pre-commit run pre-commit hooks (ruff + pyrefly)"
	@echo "  make verify     pre-push gate: pre-commit + frontend lint/build; mirrors CI's lint gate"
	@echo "  make test       run pytest + vitest (test-backend / test-frontend for one)"

# The backend mounts the external volume, so it must exist first.
up up-dev: volumes

retention-dry-run:
	$(COMPOSE) exec backend python -m ghostwriter.retention --dry-run $(if $(CHAT_DAYS),--chat-days $(CHAT_DAYS))

nuke:
	@echo "This will DESTROY every Ghostwriter chat (volume: ghostwriter-data)."
	@read -p "Type 'ghostwriter' to confirm: " confirm && [ "$$confirm" = "ghostwriter" ] \
	  || (echo "aborted"; exit 1)
	$(COMPOSE) down
	docker volume rm ghostwriter-data
