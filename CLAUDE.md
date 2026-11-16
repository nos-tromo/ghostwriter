# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## What this repo is

Ghostwriter is the federation's chat assistant: talk to it, or attach a
document and download the result in the chat. Plain conversation is a full
use of the app, also while workbench is switched off or unreachable. A
FastAPI backend keeps the chats in SQLite on the external `ghostwriter-data`
volume and reaches the model only through the router on `inference-net`;
attached documents go to workbench over `workbench-net`, which processes
them and hands the results back for download, so no file is stored here. A
React SPA (`@infra/ui`, served by nginx under `/ghostwriter/`) is reached
only through the edge gateway on `edge-net`.
