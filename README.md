# ai-vfs

A virtual filesystem library for AI agents.
Provides filesystem semantics (read, write, list, search, execute) over pluggable storage backends, with per-file versioning, path-based access control, and sandboxed code execution.

## Overview

ai-vfs separates concerns into two storage layers:

- **Blob store** (S3-compatible) — immutable, content-addressed file storage (BLAKE3 hashes)
- **Metadata store** (pluggable SQL/NoSQL) — paths, versions, permissions, audit log, search artifact manifests

Agents interact through a VFS layer that orchestrates these stores behind a simple API: `read`, `write`, `list`, `stat`, `search`, `versions`, `rollback`.
The VFS enforces permissions, manages versioning, and emits OpenTelemetry traces.

### Key features

- **Per-file versioning** with undo/rollback and Time Machine-style retention
- **Content-addressed deduplication** — identical files share one blob
- **Path-based access control** with invisible pruning (default-deny)
- **Pluggable search** — built-in glob/grep, optional bloom filter acceleration and semantic search
- **Sandboxed execution** — [Monty](https://github.com/pydantic/monty) initially, with VFS operations injected as callbacks; [Bashkit](https://github.com/everruns/bashkit) remains a future shell provider, and [just-bash](https://github.com/vercel-labs/just-bash) is a tertiary JS/TS follow-on
- **Optimistic concurrency** — no locks, no coordination layer; CAS via version stamps
- **Self-hostable** — sensible local defaults (SQLite + local filesystem), scales to S3 + Postgres

### Storage adapters

| Layer     | Adapters                                                                               |
| --------- | -------------------------------------------------------------------------------------- |
| Metadata  | SQLite, PostgreSQL, MongoDB/CosmosDB                                                   |
| Blobs     | Local filesystem, S3/MinIO, Azure Blob                                                 |
| Search    | Glob/grep (built-in), bloom filter, semantic (plugins)                                 |
| Execution | Monty (initial); Bashkit (future); just-bash, Eryx, PyMiniRacer, E2B (tertiary/future) |

## References

### Inspiration

- [How we built a virtual filesystem for our Assistant](https://www.mintlify.com/blog/how-we-built-a-virtual-filesystem-for-our-assistant)
- [Implementing a virtual filesystem over Elasticsearch – Leonie Monigatti](https://leoniemonigatti.com/blog/virtual-filesystem-elasticsearch.html)
- [Backends - Docs by LangChain](https://docs.langchain.com/oss/python/deepagents/backends#use-a-virtual-filesystem)
- [Litestream Writable VFS · The Fly Blog](https://fly.io/blog/litestream-writable-vfs/)
- [Everything is Context: Agentic File System Abstraction for Context Engineering - 2512.05470v1.pdf](https://export.arxiv.org/pdf/2512.05470)
- [Code Mode: the better way to use MCP](https://blog.cloudflare.com/code-mode/)
- [Code execution with MCP: building more efficient AI agents \\ Anthropic](https://www.anthropic.com/engineering/code-execution-with-mcp)
- [Introducing smolagents: simple agents that write actions in code.](https://huggingface.co/blog/smolagents)
- [Forget MCP, Bash Is All You Need - Dead Neurons](https://deadneurons.substack.com/p/forget-mcp-bash-is-all-you-need)
- [I Improved 15 LLMs at Coding in One Afternoon.
  Only the Harness Changed. | Can.ac](https://blog.can.ac/2026/02/12/the-harness-problem/) and [Hash anchors + Myers diff + single-token anchors: 60% cheaper AI code edits - Dirac](https://dirac.run/posts/hash-anchors-myers-diff-single-token#token-efficiency)
- [Introducing bash-tool for filesystem-based context retrieval - Vercel](https://vercel.com/changelog/introducing-bash-tool-for-filesystem-based-context-retrieval) and [Testing if "bash is all you need" - Vercel](https://vercel.com/blog/testing-if-bash-is-all-you-need)
- [strukto-ai/mirage: A Unified Virtual Filesystem For AI Agents](https://github.com/strukto-ai/mirage)

### Prior Art / Possible Dependencies / Integrations

- [fsspec/filesystem_spec: A specification that python filesystems should adhere to.](https://github.com/fsspec/filesystem_spec)
- [juicedata/juicefs: JuiceFS is a distributed POSIX file system built on top of Redis and S3.](https://github.com/juicedata/juicefs)
- [vercel-labs/just-bash: Bash for Agents](https://github.com/vercel-labs/just-bash)
- [Introducing bash-tool for filesystem-based context retrieval - Vercel](https://vercel.com/changelog/introducing-bash-tool-for-filesystem-based-context-retrieval)
- [everruns/bashkit: Rust virtual bash with Python and JS/TS bindings](https://github.com/everruns/bashkit)
- [Bashkit architecture docs](https://www.mintlify.com/everruns/bashkit/concepts/architecture)
- [bashkit/crates/bashkit-python/README.md at main · everruns/bashkit](https://github.com/everruns/bashkit/blob/main/crates/bashkit-python/README.md)
- [pydantic/monty: A minimal, secure Python interpreter written in Rust for use by AI](https://github.com/pydantic/monty)
- [timescale/tigerfs: Mount PostgreSQL as a filesystem. Build apps with files, explore databases with ls and cat.](https://github.com/timescale/tigerfs)
- [eryx-org/eryx: A Python sandbox using Wasmtime](https://github.com/eryx-org/eryx)
- [inducer/starlark-pyo3: A Python wrapper for starlark-rust](https://github.com/inducer/starlark-pyo3)
- [mickael-kerjean/filestash: :file_folder: File Management Platform / Universal Data Access Layer (without FUSE)](https://github.com/mickael-kerjean/filestash)
- [c4pt0r/agfs: Aggregated File System (AGFS), a modern tribute to the spirit of Plan 9](https://github.com/c4pt0r/agfs)
- [vstorm-co/pydantic-deepagents: Build Claude Code–style deep agents with Pydantic AI.](https://github.com/vstorm-co/pydantic-deepagents) and [vstorm-co/pydantic-ai-backend: File Storage & Sandbox Backends for Pydantic AI](https://github.com/vstorm-co/pydantic-ai-backend)

### Collaborative editing?

- [yjs/yjs: Shared data types for building collaborative software](https://github.com/yjs/yjs)
- [prosemirror/prosemirror-collab: Collaborative editing for ProseMirror - code.haverbeke.berlin](https://code.haverbeke.berlin/prosemirror/prosemirror-collab)
- [Lies I was Told About Collaborative Editing, Part 2: Why we don't use Yjs / Moment devlog](https://www.moment.dev/blog/lies-i-was-told-pt-2)

## Documentation

For user-facing docs and API reference, this project uses MkDocs + Material + mkdocstrings.

Install docs dependencies:

```bash
uv sync --group docs
```

Run a local docs server:

```bash
uv run --group docs mkdocs serve
```

Build static docs:

```bash
uv run --group docs mkdocs build
```

Build output is written to `site/`.

For self-hosted Docat packaging and upload handoff, see `docs/docat-handoff.md`.

## AI Disclosure

This project uses spec-driven development to allow AI coding assistance to work on well-specified features.
See the `sdd-*` family of [ahgraber/skills: Agent skills](https://github.com/ahgraber/skills).

## Contributing

Contributions and fixes are welcome.
Please open issues or pull requests with clear descriptions and tests where appropriate.

### Releasing

This project uses [uv-ship](https://github.com/floRaths/uv-ship) to manage releases.
Install it as a uv tool:

```bash
uv tool install uv-ship
```

To cut a release:

```bash
# do a dry run first!
uv-ship --dry-run next <major | minor | patch>

# if everything looks good, ship it
uv-ship next <major | minor | patch>
```

This bumps the version in `pyproject.toml`, updates `CHANGELOG`, commits, tags, and pushes.
See `[tool.uv-ship]` in `pyproject.toml` for configuration.
