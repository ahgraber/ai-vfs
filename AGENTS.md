# Instructions

Deliver exactly what was requested.
Avoid speculative extras, but include the minimum tests, documentation, and safeguards needed to keep behavior correct and prevent regressions.

## Directive Priority

If directives conflict, prioritize:

1. Correctness and safety at external boundaries
2. Explicit user instructions
3. Minimal scope and simplicity

## 1. Think Before Coding

Objective: surface ambiguity and tradeoffs before writing any code.

- State assumptions explicitly.
- If uncertainty would materially change the implementation, ask.
  Otherwise, state your assumption and proceed.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so.
  Push back when warranted.

## 2. Simplicity First

Objective: write the minimum change that meets the request.

- No features, abstractions, or configurability beyond what was asked.
- No "flexibility" or "configurability" that wasn't requested.
- Prefer the golden path for internal logic; let tests define edge-case expectations.
- Add explicit validation and error handling at external boundaries (I/O, network, persistence, auth, parsing, external APIs).
- If you write 200 lines and it could be 50, rewrite it.
- Apply YAGNI ruthlessly.

## 3. Surgical Changes

Objective: every changed line traces directly to the request.

When editing existing code:

- Touch only what the request requires.
  Don't "improve" adjacent code, comments, or formatting.
- Match existing style, even if you'd do it differently.
  Don't refactor existing code unless it is part of the request.
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked — mention it instead.

## 4. Goal-Driven Execution

Objective: define success criteria, then loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → write tests for invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → ensure tests pass before and after.

Testing guardrails:

- Never modify a failing test to make it pass.
  Fix the code under test.
- If a test is genuinely wrong, explain why and await user approval before changing it.
- Write implementations that solve the general problem, not code that special-cases specific test inputs.

For multi-step tasks, state a brief plan defining the step task and associated verification checks.

## 5. Definition of Done

- The requested behavior works as specified.
- Behavior changes are covered by tests, or testing gaps are explicitly stated.
- Public contract changes are documented.
- Required checks were run when available; if not run, state what was skipped and why.

## Defaults

- Review available skills for relevance before writing code.
- If intermediate, user-aligned work is needed before final output, surface in .\_scratch/.
- If worktrees are warranted use a local .worktrees/ directory.
- Use descriptive, consistent naming conventions.
- Write docstrings or comments for public contracts and non-obvious behavior.
- Comments and docstrings describe what exists now (or the rationale for the current design), never what the code used to be.
  No "previously…", "no longer…", "changed from…", or "renamed from…" — that history belongs in commit messages and changelogs.
  When editing, delete stale historical asides you encounter rather than preserving them.
- Use type annotations where the language supports them.
- Use structured logging where the project uses logging.
- Run lint/format/test through project tooling when available; do not hand-format code.
- Write tests for public behavior and regressions, not implementation details.

## Technology & Data Handling Requirements

- Python code runs in the uv-managed environment; dependencies are pinned via uv/lockfiles (pyproject.toml + uv.lock) and honored by Nix devshells — no ad-hoc global installs.
- Default runtime targets CPU-only execution; GPU use requires an explicit cost and ops justification.
- Storage of raw inputs and derived artifacts must permit replay; blob/object storage locations are recorded alongside metadata.
- Secret management: Secrets/keys MUST NOT be committed.
  Store them in a gitignored `.env` file (or a secret manager) and access them via environment variables at runtime.
- Process identification: Every Python process MUST set a descriptive process title using `setproctitle` so hosts running multiple Python processes can distinguish them.

## Workflow & Quality Gates

- Use Spec-Driven Development and Test-Driven Development.
- Any change to data schemas, embedding parameters, or retrieval scoring requires a migration/test plan and version bump of the affected artifact.
- Code review checks for reproducibility (pinned deps, seeded operations), privacy adherence, and observability hooks (structured logs + metrics).

### Spec authoring

- **Contract floor (weakest common denominator).**
  When a spec defines a contract (protocol, interface, adapter) satisfied by more than one implementation or backend family, define the MANDATORY contract at the **intersection** of what every declared implementation guarantees — never the union, never the strongest one.
  Expose capabilities only some implementations provide as **optional, queryable capabilities** (feature detection, e.g. `native_text_search() -> None`), never as mandatory clauses only some backends satisfy.
  Name exemplar implementations in scenarios; keep exemplar-specific behavior out of contract prose.
  This is the Liskov Substitution Principle for backends: any declared implementation MUST be substitutable without callers observing a behavioral change.
- **Value-chain laddering.**
  Every change's user stories (in `proposal.md`) MUST ladder to the product north star (`.specs/NORTH-STAR.md`); every delta-spec requirement that advances a story carries a `Serves: <story>` backlink.
  A requirement that ladders to no story is scope to question, not implement.
- **Document hierarchy (single source of truth).**
  North star = product intent; baseline `specs/` = contracts; per-change `design.md` = decisions and rationale (there is no separate ADR store).
  On any conflict, north star + specs win.

## Governance Practices

- Semantic Versioning is REQUIRED (MAJOR.MINOR.PATCH).
- Conventional Commits are REQUIRED for commit messages and/or PR titles.
- Keep a Changelog is REQUIRED; it is managed via `uv-ship` during the release process and follows <https://keepachangelog.com> format.
- After the first MINOR release, all changes affecting data/schema/contracts MUST include a migration plan and a deprecation schedule.

## Testing

- Run tests via `uv run pytest tests/`.
- For parallel execution, use `pytest-xdist`: `uv run pytest -n auto -m "not isolate" tests/`.
- Tests marked `isolate` (subprocess lifecycle with `pytest-isolate`) are incompatible with xdist; run them separately: `uv run pytest -m isolate tests/`.
- Do not use `pytest-run-parallel` — it is a thread-safety stress tester (runs the same test N times in N threads), not a test suite parallelizer.

### Resource leak detection with pyleak

Use [pyleak](https://github.com/deepankarm/pyleak) to guard against leaked asyncio tasks and threads.
When writing tests for code that spawns concurrent work, wrap the act phase with the appropriate pyleak context manager:

- `no_task_leaks(action="raise")` — for code using `asyncio.create_task`, `asyncio.gather`, `asyncio.to_thread`, or `TaskGroup`.
- `no_thread_leaks(action="raise")` — for code using `ThreadPoolExecutor`, `threading.Thread`, or `subprocess.Popen` lifecycle management.

Existing examples: `test_execute.py`, `test_boundary_hardening.py`, `test_sqlite_metadata.py`, `test_monty_provider.py`, `test_postgres_metadata.py`.

## Commit & Review Guidelines

- **Hard gate before committing**: before running `git agent-commit`, present the user with (1) the proposed commit message and (2) a concise diff summary covering which files changed and what each change does.
  Wait for explicit user approval; do not proceed if the user requests changes.
- **Every commit message draft, without exception, must be produced by invoking the `commit-message` skill first.**
  A prior invocation earlier in the same session does not satisfy this requirement — re-invoke for each request.
  Drafting inline, from memory, or from habit is not acceptable.
- Commit format: `type(scope): summary` (e.g., `feat(zsh): …`, `fix(vscode): …`).
  Scope should reflect directories or logical surfaces.
- Separate unrelated changes (docs vs configs vs lockfile updates) into distinct commits.
- Use `git agent-commit` (not `git commit`) to create signed commits; this alias uses the dedicated agent signing key at `~/.ssh/id_ed25519_agent_signing`.

## Sandbox Limitations

- The sandbox may not be able to run `uv sync` or read `.env` / `.env.example` (permission errors) — attempt the command first rather than assuming failure.
- Delegate to the user only if a command actually fails on a permission or missing-tool error.
  Describe the exact command to run (e.g., `uv run pytest tests/...`).
