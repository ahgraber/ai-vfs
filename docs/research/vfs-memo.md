# The virtual filesystem is the right substrate for AI agents

_A memo synthesizing current thinking on agent architecture_

---

## Four teams independently built the same architecture

In 2025, four teams working on unrelated problems arrived at the same answer.

Mintlify was trying to make its documentation assistant faster.
It was spinning up a sandbox per conversation — 46-second boot times, $70K/year in compute, for what amounted to a read-only search problem.
Its fix was to stop giving the agent a real filesystem and give it the illusion of one instead, backed by the Chroma database it was already running.
Boot time dropped to 100 milliseconds.
Marginal cost dropped to zero.

Anthropic and Cloudflare were attacking a different problem: token bloat from MCP tool definitions.
Connecting five MCP servers costs 55,000 tokens of schema metadata before the user types a word.
Their fix represents tools as files in a directory tree, so the agent runs `ls` on the servers directory, `cat`s only the tool definitions it needs, and writes code that calls them.
Token usage dropped 98.7%.

CSIRO and ArcBlock published a paper proposing AIGNE, a framework formalizing a third angle on the problem: context engineering.
LLMs are stateless, memory is ad hoc, and tool integrations are fragmented.
AIGNE's proposed fix treats memory, tools, history, and human input alike as files mounted in a unified namespace, governed by the same access control and traversable by the same operations.

Fly.io was building ephemeral compute that needed durable storage without cold-start penalties.
It solved this with a virtual filesystem layer over S3, serving queries before the database had finished downloading.

None of the four consulted the others.
Mintlify was cutting compute costs, Anthropic and Cloudflare were cutting token costs, CSIRO and ArcBlock were solving for auditability and governance, and Fly.io was solving for cold starts.
All four landed on the same primitive: a filesystem interface between the agent and whatever actually stores its world.

---

## The filesystem offers five properties no other abstraction combines

The filesystem has survived fifty years of computing paradigm shifts, not because it is clever, but because it maps cleanly onto the fundamental operations of working with information.
For AI agents specifically, it provides five things that nothing else provides together.

**A uniform namespace.**
Every resource — a documentation page, a tool definition, a memory entry, a message queue, a live API — gets a path: `/tools/salesforce/updateRecord.ts`, `/memory/fact/user-preferences.json`, `/context/inbox/2026-03-11.ndjson`.
The path carries meaning before you open it.
A vector embedding does not.

**Navigable structure.**
Agents can browse.
`ls /tools/` reveals what is available; `cat /tools/salesforce/updateRecord.ts` reveals how to use it; `find /memory -name "*.json" -newer /checkpoints/last-run` retrieves what changed.
This is progressive disclosure: the agent discovers what it needs at the moment it needs it.

**Composition primitives.**
Shell pipes are inter-process communication.
`grep -r "deployment" /memory | head -5` chains two operations without touching the model.
`tool_a | tool_b | tool_c` is a three-step workflow with no intermediate inference pass.
At current API pricing, a five-step pipeline that costs fractions of a cent through pipes can cost dollars when each step requires a model round trip.

**Access control that is legible to the abstraction.**
ChromaFS prunes the directory tree before presenting it to the agent: a user without billing access never sees `internal/billing.mdx` in `ls`, cannot reference it, cannot ask for it.
RBAC that would require Linux user groups and container isolation in a real filesystem is three lines of filtering in a virtual one.

**Persistence and lifecycle semantics.**
Files persist.
They are versioned, diffable, shareable.
An agent's memory is not a transient embedding to be searched.
It is a home directory that accumulates.
An agent that has been running for six months has a richer workspace than one that started yesterday, in a way that compounds and that humans can inspect.

No other abstraction provides all five properties together:

| Abstraction             | Provides   | Lacks       |
| ----------------------- | ---------- | ----------- |
| REST APIs               | Access     | Navigation  |
| Databases               | Structure  | Composition |
| Tool-calling frameworks | Invocation | Persistence |

The filesystem is the only one of the four that provides all five as a coherent unit.

---

## The filesystem is cheaper on three independent counts

The convergence in the previous section would be interesting even if it were only architectural.
What makes it compelling is that three independent lines of evidence show the filesystem approach is strictly cheaper: in latency, in tokens, and in infrastructure.

**The latency argument (Mintlify).**
Real sandboxes have cold-start costs measured in seconds and infrastructure costs measured in tens of thousands of dollars a year.
Virtual filesystems have cold-start costs measured in milliseconds and marginal costs of zero, because they reuse existing infrastructure.
For any agent running at scale, this is not a close call.

**The token argument (Anthropic, Cloudflare).**
Direct tool calling loads every schema upfront; code execution with filesystem-organized tools loads nothing upfront, and the agent navigates to what it needs.
Anthropic measured 150,000 tokens vs. 2,000 tokens for the same task.
Cloudflare found that LLMs handle significantly more tools and more complex tool compositions when interfaces are presented as TypeScript APIs (familiar from training data) rather than as JSON tool schemas (synthetic).
LLMs are trained on millions of real-world code repositories, not on tool-call schemas: code is their native language.

**The composition argument (Dead Neurons, Vercel).**
Every MCP tool call requires a full model inference pass.
Pipes do not.
The operating system manages data flow between isolated processes without touching the context window; this is what operating systems are for.
The "fixes" being added to MCP (deferred tool loading, programmatic tool calling) are independently reinventing process invocation and IPC, primitives POSIX has provided stably for decades.
MCP is converging on the OS layer it already runs on top of.

---

## Filesystem semantics as the interface, specialized storage as the backend

The most important design insight across all these implementations is easy to miss: _the agent doesn't need a real filesystem._
_It needs the illusion of one._

ChromaFS intercepts UNIX commands and translates them into Chroma vector queries.
The directory tree is a gzipped JSON document stored in the same database it queries.
`grep -r` is a Chroma `$contains` filter followed by in-memory verification of matching chunks.
The agent never knows.
`cat /auth/oauth.mdx` returns a page; `grep -r "rate limit" /api-reference/` returns a list of files.

Litestream VFS serves SQLite queries directly from S3 object storage before the local database has been restored.
The AIGNE framework mounts REST APIs, MCP servers, and vector stores as directories under a common namespace.
AGFS mounts Redis, queues, and SQL under POSIX paths.
In every case, the filesystem interface is a semantic layer over heterogeneous backends.

That is the core design principle for a virtual filesystem for cloud agents: filesystem semantics as the interface, specialized storage as the backend.

| Backend          | Strength                       | Weakness             |
| ---------------- | ------------------------------ | -------------------- |
| Vector databases | Fast at semantic recall        | Opaque to navigation |
| SQL              | Precise for structured queries | Poor for exploration |
| Object storage   | Cheap                          | Unstructured         |

The virtual filesystem is the coordination layer that lets agents use all three through a single interface — `read`, `write`, `search`, `list` — without needing to know which backend answers each request.

The Vercel benchmarks prove this point empirically.
Pure bash (filesystem) achieved 52.7% accuracy on structured queries; pure SQL achieved 100%.
But a hybrid agent — bash for exploration and verification, SQL for structured queries — also hit 100%, and added something the pure SQL approach could not: self-verification.
The agent used grep to spot-check its SQL results, and that cross-layer checking is what elevated it above both single-abstraction approaches.

The lesson is not that bash wins or SQL wins: the right interface is the one that matches the operation type, and the virtual filesystem is the layer that unifies them.

---

## The memory problem is the filesystem problem

The biggest unsolved problem in production AI agents is not intelligence.
It is memory.

Agents do impressive work within a single session.
Across sessions — across days, weeks, months — they lose coherence: they forget what they learned, and each run starts closer to zero than it should.
The database answer to this problem is to build better retrieval: embed everything, index everything, rank by similarity.
That works for simple recall, but it breaks down when memory is nuanced, role-specific, or structurally related.

The filesystem approach is different in kind.
Memory is a navigable workspace with structure, and that structure encodes the meaning before retrieval begins:

| Path                      | Contents               |
| ------------------------- | ---------------------- |
| `/memory/fact/`           | Atomic facts           |
| `/memory/episodic/`       | Session summaries      |
| `/knowledge/competitors/` | Accumulated analysis   |
| `/context/inbox/`         | Recent external inputs |

yarnnn's three-storage-domain model formalizes this: external context (perception, ephemeral), agent intelligence (private memory, persistent), accumulated knowledge (shared, compound).
The AIGNE paper formalizes it differently: history (immutable), memory (mutable, typed), scratchpad (temporary).
Both arrive at the same conclusion: persistent memory requires explicit lifecycle governance — what to keep, for how long, at what level of abstraction — and the filesystem is the right substrate for that governance, because it makes the structure legible to both agents and humans.

This legibility is underrated.
When an agent's memory is a directory you can open in a text editor, you can audit it, correct it, fork it, version it.
When it is a vector index, it is opaque.
The filesystem approach solves the agent's memory problem and, with it, the human oversight problem.

---

## Seven properties a virtual filesystem product needs

Taken together, these converging threads suggest an architectural surface that does not fully exist yet.
It is worth building deliberately, with seven properties.

**Pluggable backends behind uniform operations.**
The complete primitive set is five operations: `read`, `write`, `list`, `search`, and `exec`.
Each path prefix can resolve to a different backend — vector store, object storage, SQL, REST API, MCP server, code execution sandbox — and the agent never negotiates with a backend directly.

**Access control embedded in the namespace.**
Access control lives in how the directory tree gets built, not bolted on as a middleware layer afterward.
What a user or agent can see in `ls` is exactly what they can access, with no capability inference, no policy engine, and no separate ACL check.

**Session-less, stateless by default, persistent by convention.**
The filesystem is the persistence layer: agent state is files, and a write operation is the commit mechanism.
There is no session to tear down, no cleanup step, and no risk of cross-contamination between agents.

**Lazy content resolution.**
A directory tree is cheap, because it is metadata; file content is expensive, because it requires retrieval.
The agent browses the tree for free, and content fetches happen on demand.
Large documents, API responses, and tool schemas load only when the agent explicitly reads them.

**Tool definitions as code, not schemas.**
MCP servers and other tool sources are projected as typed code files: TypeScript interfaces, Python stubs.
The agent writes code that imports and calls them.
Intermediate results stay in the execution environment; only final outputs return to the model.

**A graduated execution model.**

| Operation tier | Examples                    | Constraint                          |
| -------------- | --------------------------- | ----------------------------------- |
| Read-only      | Browsing, retrieval         | Zero-cost, stateless                |
| Read-write     | Memory updates, scratchpad  | Low-cost, scoped                    |
| Execution      | Running code, calling tools | Sandboxed, explicit resource limits |

The filesystem interface expresses this naturally: directories contain executables, and permissions indicate what the agent may do.

**Compounding memory across runs.**
An agent's workspace directory persists: memory files accumulate, and the shared knowledge directory grows.
Agents improve not because the model changes but because the workspace gets richer, a property most agent platforms still lack.

---

## Five problems the pattern hasn't solved yet

The four implementations above validate the pattern; they do not resolve everything about it.
Five problems remain open.

**Consistency in distributed deployments.**
A virtual filesystem shared across multiple agent instances needs a consistency model.
Eventual consistency is probably acceptable for accumulated knowledge; it is not acceptable for active task coordination.
The right answer likely involves explicit conflict zones — append-only logs for history, last-write-wins for ephemeral context, explicit merge for shared knowledge — rather than a single consistency level.

**Garbage collection and memory hygiene.**
A filesystem that only accumulates will eventually become noise.
Memory needs lifecycle governance: scratchpads expire, episodic memory compacts into facts, outdated knowledge gets flagged for review.
The AIGNE paper calls this "context rot."
The mechanisms for combating it need to be first-class, not afterthoughts.

**Schema versus free-form.**
The most powerful filesystem operations (grep, find, semantic search) work best when content is well-structured, but agents generate free-form output.
There is a tension between the flexibility that makes filesystems useful and the structure that makes retrieval reliable.
Virtual filesystems will need schema hints that guide rather than enforce, to help agents organize what they write.

**Sandboxing at the execution layer.**
Representing tools as executable files creates a natural execution surface, and that surface needs isolation: Cloudflare's V8 isolates, E2B's sandboxes, or the equivalent.
The filesystem interface unifies tool invocation, but the execution guarantees must come from the runtime underneath.
This is not a solved problem for every deployment context.

**The "bash is all you need" overcorrection.**
The Vercel benchmark is a useful corrective: raw bash on structured data loses badly to SQL.
The virtual filesystem framing is more nuanced than the "just use bash" camp acknowledges.
The filesystem is the interface; what matters is that the right backend answers each operation.
Advocates of filesystem-first agent design need to be careful not to over-index on the POSIX primitives at the expense of the backends that give them power.

---

## Build the layer deliberately

The virtual filesystem for AI agents is a pattern discovered independently, under production pressure, by teams that never coordinated with each other.
Every team got there by solving a different problem: latency and cost for Mintlify, token efficiency for Anthropic and Cloudflare, auditability and governance for the AIGNE authors, cold starts for Fly.io.
All four arrived at the same abstraction: the filesystem interface is the right layer between an agent and whatever actually stores its world.

The remaining work is to build that layer deliberately, rather than have the next team rediscover it under its own production pressure.
That means a virtual filesystem explicitly designed for the constraints of cloud agents: stateless LLMs, bounded context windows, heterogeneous backends, multi-agent coordination, compounding memory, and human oversight.
It means resolving the five problems above, not shipping around them.

The filesystem is the oldest interface in computing; it is also the one the most successful agent products keep arriving at, for an ordinary reason: an agent that browses, reads, and composes needs the same three operations a person at a terminal needed, even though what it browses now is memory, tools, and accumulated intelligence, files that didn't exist when the abstraction was invented.
What is left is not a prediction about the next decade.
It is the five problems above, and the deliberate choice to solve them once — a choice each of the four teams already made on its own, under pressure, without the others.

---

## Appendix: common features across sources

The following themes recur independently across all surveyed sources.

| Feature                                    | Sources                                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------------------ |
| Filesystem as universal agent interface    | Mintlify, Anthropic, Cloudflare, Vercel, AIGNE, yarnnn, AGFS, Dead Neurons           |
| Virtual/abstract FS over real sandboxes    | Mintlify (ChromaFS), Fly.io (Litestream VFS), AIGNE (AFS), AGFS                      |
| Code execution > direct tool calling       | Cloudflare (Code Mode), Anthropic (MCP blog), HuggingFace (smolagents), Dead Neurons |
| Progressive disclosure / lazy loading      | Mintlify, Anthropic, Vercel bash-tool, AIGNE (Context Constructor)                   |
| Sandboxed execution with structured access | Cloudflare (V8 isolates), smolagents (E2B), ChromaFS (EROFS), Litestream VFS         |
| RBAC embedded in the abstraction           | Mintlify (tree pruning), AIGNE (governance layer), AGFS (permissions)                |
| Context budget as first-order constraint   | All sources — token efficiency is the central engineering concern                    |
| Persistent memory with FS semantics        | AIGNE (history/memory/scratchpad), yarnnn (three storage domains)                    |
| LLM-as-OS paradigm                         | Dead Neurons, yarnnn, AGFS, AIGNE, AIOS reference                                    |
| Hybrid approaches over dogma               | Vercel (bash+SQL hybrid wins), yarnnn (FS semantics + vector acceleration)           |
| Compounding intelligence over time         | yarnnn (workspace richness), AIGNE (accumulation lifecycle)                          |
