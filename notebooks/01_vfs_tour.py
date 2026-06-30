# %% [markdown]
# # ai-vfs — Local VFS Tour
#
# A hands-on walkthrough of the full VFS API using only local backends:
# SQLite for metadata and the local filesystem for blobs — zero infrastructure
# required.
#
# **Run model:** open in VS Code Interactive or Jupyter and execute cells
# top-to-bottom with **Shift+Enter**.  Top-level `await` is valid because the
# kernel owns the event loop — do **not** call `asyncio.run()` inside a running
# kernel.
#
# **Monty cells:** the execution sandbox (Section 7) requires `pydantic-monty`,
# included in the default `uv sync`.  A clear skip message is printed when absent.

# %%
from __future__ import annotations

import importlib.util
import logging
import shutil
import tempfile

from vfs import VFS, ConflictError, NotFoundError, PermissionDeniedError, VFSConfig
from vfs.models import SearchType
from vfs.protocols.execution import ResourceLimits
from vfs.protocols.search import FindPredicates
from vfs.session import Session

logging.basicConfig(level=logging.WARNING)

HAS_MONTY: bool = importlib.util.find_spec("pydantic_monty") is not None

# %% [markdown]
# ## 1. Setup
#
# `demo_setup` is kept as a function because it is boilerplate you rarely need
# to step through.  It builds the VFS with SQLite + local-FS backends, creates
# a namespace, and provisions two principals:
#
# - **admin** — `admin + read + write + delete + execute` on `/`
# - **reader** — `read` only on `/docs/`; all other paths are invisible to it
#
# The call below unpacks the result into notebook-scope variables used by every
# subsequent cell.


# %%
async def demo_setup(tmp_dir: str) -> tuple:
    """Construct VFS, namespace, and principals."""
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_dir}/demo.db",
        blob_store_uri=f"file:///{tmp_dir}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
        # Aggressive retention so the GC section can demonstrate pruning.
        retention_max_recent=3,
    )
    vfs = VFS(config)
    await vfs.initialize()

    ns = await vfs.create_namespace("demo", "system")
    admin = await vfs.create_principal("admin", principal_type="user")

    # bootstrap_admin: one-time grant of `admin` on `/`; closes once any admin exists.
    await vfs.bootstrap_admin(admin.id, ns.id)

    # Admin also needs explicit op grants — `admin` permission ≠ `read`/`write`/etc.
    # Include "admin" here: grant() does an upsert on (principal, namespace, path_prefix),
    # so this call replaces the bootstrap_admin row.  Keeping "admin" in the set preserves
    # the ability to call grant() again later in the demo.
    await vfs.grant(
        admin.id,
        admin.id,
        ns.id,
        "/",
        {"admin", "read", "write", "delete", "execute"},
    )

    # Reader: read-only on /docs/.  Every other path is invisible (silent pruning).
    reader = await vfs.create_principal("reader", principal_type="agent")
    await vfs.grant(admin.id, reader.id, ns.id, "/docs/", {"read"})

    print(f"namespace : {ns.id}")
    print(f"admin     : {admin.id}")
    print(f"reader    : {reader.id}")
    return vfs, ns.id, admin.id, reader.id


# %%
tmp_dir = tempfile.mkdtemp(prefix="vfs-demo-")
vfs, ns_id, admin_id, reader_id = await demo_setup(tmp_dir)

# %% [markdown]
# ## 2. Files & Versions
#
# Every write creates an immutable, content-addressed version.  The VFS stores
# the blob by its BLAKE3 hash so identical content shares the same blob object
# across versions and files.  Version numbers increment monotonically and are
# never reused, even after a rollback or delete.

# %% [markdown]
# ### write — create v1
#
# `write()` hashes the content, PUTs the blob, and records a new `VersionMeta`
# row in the metadata store.  The returned object carries the version number,
# content hash, and size.  Providing `expected_version=` enables optimistic
# locking (see Section 3).

# %%
v1 = await vfs.write(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    content=b"Hello, VFS!\n",
    principal_id=admin_id,
)
print(f"write v{v1.version_number}  size={v1.size}  hash={v1.content_hash[:12]}…")

# %% [markdown]
# ### read
#
# `read()` resolves the current version's content hash, fetches the blob, and
# returns raw bytes.  Reading a tombstoned (deleted) file raises `NotFoundError`.
# Pass `version_number=` to read a specific historical version.

# %%
content = await vfs.read(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print(f"read      : {content!r}")

# %% [markdown]
# ### stat
#
# `stat()` returns `FileMeta` — the current version number, path, and
# timestamps — without fetching any blob content.  Use it to check existence or
# capture the current version before a CAS write.

# %%
meta = await vfs.stat(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print("stat:")
print(meta.model_dump_json(indent=2))

# %% [markdown]
# ### list
#
# `list()` returns all files under `path_prefix`.  Entries the acting principal
# cannot read are silently omitted (invisible pruning) — the result shape never
# reveals inaccessible paths.  Add `recursive=True` to traverse the full subtree.

# %%
files = await vfs.list(
    namespace_id=ns_id,
    path_prefix="/docs/",
    principal_id=admin_id,
)
print(f"list      : {[f.path for f in files]}")

# %% [markdown]
# ### write v2 — content change
#
# Changing the content produces a new version with a different BLAKE3 hash.
# If the bytes were identical the blob would be de-duplicated on disk but a new
# version row would still be written.  You can freely edit and re-run this cell
# to observe how the version number increments each time.

# %%
v2 = await vfs.write(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    content=b"Hello, updated VFS!\n",
    principal_id=admin_id,
)
print(f"write v{v2.version_number}  (content changed, new blob)")
print(f"v1 hash   : {v1.content_hash[:12]}…")
print(f"v2 hash   : {v2.content_hash[:12]}…")

# %% [markdown]
# ### versions
#
# `versions()` returns the full history of a file, newest-first.  Each entry
# carries the version number, content hash, size, and creation timestamp.  The
# list is the basis for rollbacks and audit inspection.

# %%
history = await vfs.versions(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print(f"versions  : {[(h.version_number, h.content_hash[:12] + '…') for h in history]}  (newest first)")

# %% [markdown]
# ### rollback
#
# `rollback()` is non-destructive: it creates a *new* version whose content hash
# matches the chosen historical version.  No rows are removed.  The blob is
# already in the store by content-address so rollback is a metadata-only
# operation with zero blob I/O.

# %%
rolled = await vfs.rollback(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    target_version=1,
    principal_id=admin_id,
)
content_back = await vfs.read(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print(f"rollback  : v{rolled.version_number} restores v1 content → {content_back!r}")

history = await vfs.versions(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print(
    f"rollback  : (v{rolled.version_number}, {rolled.content_hash[:12]}…) == (v{history[-1].version_number}, {history[-1].content_hash[:12]}…)"
)

# %% [markdown]
# ### delete — tombstone
#
# `delete()` writes a tombstone version that marks the file as deleted.  No
# blobs or prior version rows are removed.  Blobs are only reclaimed by the
# garbage collector (`run_gc()`) after the configured retention window has
# passed (see Section 8).

# %%
tomb = await vfs.delete(
    namespace_id=ns_id,
    path="/docs/hello.txt",
    principal_id=admin_id,
)
print(f"delete    : tombstone v{tomb.version_number}  is_tombstone={tomb.is_tombstone}")

# %% [markdown]
# ### copy
#
# `copy()` creates a new version at `dst` that shares the source's content hash —
# no blob bytes are transferred.  The source file is unchanged.  CAS semantics
# apply at the destination if you pass `expected_version=`.

# %%
await vfs.write(
    namespace_id=ns_id,
    path="/docs/src.txt",
    content=b"source content",
    principal_id=admin_id,
)
cp = await vfs.copy(
    namespace_id=ns_id,
    src="/docs/src.txt",
    dst="/docs/dst.txt",
    principal_id=admin_id,
)
print(f"copy      : /docs/src.txt → /docs/dst.txt  v{cp.version_number}")

# read dst.txt
content = await vfs.read(
    namespace_id=ns_id,
    path="/docs/dst.txt",
    principal_id=admin_id,
)
print(f"read      : {content!r}")

# %% [markdown]
# ### move
#
# `move()` atomically tombstones `src` and creates `dst` with the same content
# hash.  On a transactional metadata store both writes are one atomic block.  On
# best-effort stores (MongoDB) the destination is written first so that a
# mid-move failure leaves a duplicate rather than a data loss.
#
# Two related but distinct facts worth distinguishing: `VersionMeta.is_tombstone`
# is the version-level flag set on the tombstone row itself; `FileMeta.is_deleted`
# is the file-level projection — it is `True` whenever the file's current version
# is a tombstone.  `delete()` returns its tombstone `VersionMeta` directly;
# `move()` returns the **destination** version, so to inspect the source tombstone
# you call `versions(src)[0]` (history is newest-first).

# %%
mv = await vfs.move(
    namespace_id=ns_id,
    src="/docs/dst.txt",
    dst="/docs/moved.txt",
    principal_id=admin_id,
)
# move() returns the DESTINATION version — is_tombstone is False.
print(f"move dst  : v{mv.version_number}  is_tombstone={mv.is_tombstone}")

# Read back the destination to prove the content landed.
dst_content = await vfs.read(
    namespace_id=ns_id,
    path="/docs/moved.txt",
    principal_id=admin_id,
)
print(f"read dst  : {dst_content!r}")

# The source path gets a tombstone version appended — newest-first, so index [0].
src_versions = await vfs.versions(
    namespace_id=ns_id,
    path="/docs/dst.txt",
    principal_id=admin_id,
)
print(f"src v[0]  : is_tombstone={src_versions[0].is_tombstone}  (newest first)")

# FileMeta.is_deleted is the file-level projection: True when current version is tombstone.
src_meta = await vfs.stat(
    namespace_id=ns_id,
    path="/docs/dst.txt",
    principal_id=admin_id,
)
print(f"src stat  : is_deleted={src_meta.is_deleted}")

# read(src) raises NotFoundError — the tombstone makes the file invisible.
try:
    await vfs.read(
        namespace_id=ns_id,
        path="/docs/dst.txt",
        principal_id=admin_id,
    )
    print("unexpected: read succeeded")
except NotFoundError:
    print("NotFoundError: /docs/dst.txt tombstoned — read() raises")

# %% [markdown]
# ## 3. Optimistic Concurrency
#
# Every `write` accepts an optional `expected_version`.  When supplied the store
# validates that the file is currently at that version before committing.  A
# racing writer that already advanced the version triggers `ConflictError`.
# Without `expected_version`, writes use internal retry on ULID collisions but
# never block or fail on a logical conflict.

# %%
# Setup: write an initial version and capture its number as our "lock".
await vfs.write(
    namespace_id=ns_id,
    path="/docs/cas.txt",
    content=b"initial",
    principal_id=admin_id,
)
cas_meta = await vfs.stat(
    namespace_id=ns_id,
    path="/docs/cas.txt",
    principal_id=admin_id,
)
stale_version = cas_meta.current_version_number  # 1
print(f"captured version {stale_version} as our expected_version guard")

# %%
# A racing write advances the file to v2 while we were "thinking".
await vfs.write(
    namespace_id=ns_id,
    path="/docs/cas.txt",
    content=b"racing update",
    principal_id=admin_id,
)
print("racing write committed → file is now at v2")

# %% [markdown]
# ### CAS conflict
#
# Our write carries `expected_version=1`, but the file is now at v2.  The store
# rejects it with `ConflictError`.  The caller must re-read and merge before
# retrying — the VFS never silently overwrites a racing change.

# %%
try:
    await vfs.write(
        namespace_id=ns_id,
        path="/docs/cas.txt",
        content=b"my conflicting write",
        principal_id=admin_id,
        expected_version=stale_version,
    )
    print("unexpected: write succeeded (should have conflicted)")
except ConflictError as exc:
    print(f"ConflictError (expected): {exc}")

# %% [markdown]
# ## 4. Permissions
#
# The VFS enforces a default-deny policy: every operation checks that the acting
# principal holds the required operation on a path prefix that covers the target.
# `list` and `search` apply **invisible pruning** — entries the principal cannot
# read are silently omitted so the result shape never leaks inaccessible paths.

# %%
# Setup: one file the reader can read (/docs/) and one it cannot (/private/).
await vfs.write(
    namespace_id=ns_id,
    path="/docs/public.txt",
    content=b"public content",
    principal_id=admin_id,
)
await vfs.write(
    namespace_id=ns_id,
    path="/private/secret.txt",
    content=b"secret",
    principal_id=admin_id,
)
print("seeded /docs/public.txt and /private/secret.txt")

# %% [markdown]
# ### reader: read an allowed path
#
# The `reader` principal holds `read` on `/docs/`.  Reading a file within that
# subtree succeeds normally.

# %%
pub_content = await vfs.read(
    namespace_id=ns_id,
    path="/docs/public.txt",
    principal_id=reader_id,
)
print(f"reader read /docs/public.txt: {pub_content!r}")

# %% [markdown]
# ### invisible pruning
#
# Listing from `/` with the `reader` principal returns only files under `/docs/`.
# The `/private/` subtree is entirely absent from the result — not "access
# denied", just invisibly omitted.

# %%
visible = await vfs.list(
    namespace_id=ns_id,
    path_prefix="/",
    principal_id=reader_id,
    recursive=True,
)
print(f"reader sees: {[f.path for f in visible]}")
print("reader cannot see /private/ — invisible pruning omits it from list results")

# %% [markdown]
# ### write denied — `PermissionDeniedError`
#
# The `reader` grant covers `read` only.  Any write attempt raises
# `PermissionDeniedError` before the store is touched.

# %%
try:
    await vfs.write(
        namespace_id=ns_id,
        path="/docs/public.txt",
        content=b"unauthorized",
        principal_id=reader_id,
    )
    print("unexpected: write succeeded")
except PermissionDeniedError:
    print("PermissionDeniedError: reader cannot write to /docs/")

# %% [markdown]
# ### read private — `PermissionDeniedError`
#
# The `reader` principal has no grant that covers `/private/`.  Reading that
# path raises `PermissionDeniedError` rather than `NotFoundError` — the file's
# existence is not disclosed.

# %%
try:
    await vfs.read(
        namespace_id=ns_id,
        path="/private/secret.txt",
        principal_id=reader_id,
    )
    print("unexpected: read succeeded")
except PermissionDeniedError:
    print("PermissionDeniedError: reader cannot read /private/")

# %% [markdown]
# ## 5. Search
#
# Four search types are available:
#
# | Type       | Backend           | Blob reads          |
# |------------|-------------------|---------------------|
# | `GLOB`     | Metadata only     | 0                   |
# | `FIND`     | Metadata + preds  | 0                   |
# | `REGEX`    | SQLite FTS5       | 0 (fresh index)     |
# | `FULLTEXT` | SQLite FTS5 BM25  | 0 (fresh index)     |
#
# SQLite FTS5 with the trigram tokenizer is activated automatically during
# `initialize()` when SQLite ≥ 3.34.  Content is indexed atomically inside each
# `write()` call — every file is immediately searchable, no separate reindex step.

# %%
# Seed files with varied content to exercise all four search types.
await vfs.write(
    namespace_id=ns_id,
    path="/src/main.py",
    content=b"def main():\n    print('hello world')\n",
    principal_id=admin_id,
)
await vfs.write(
    namespace_id=ns_id,
    path="/src/utils.py",
    content=b"def helper():\n    return 42\n",
    principal_id=admin_id,
)
await vfs.write(
    namespace_id=ns_id,
    path="/src/tests/test_main.py",
    content=b"def test_main():\n    assert True\n",
    principal_id=admin_id,
)
await vfs.write(
    namespace_id=ns_id,
    path="/docs/readme.txt",
    content=b"Welcome to the demo VFS\nhello from readme\n",
    principal_id=admin_id,
)
print("seeded /src/main.py, /src/utils.py, /src/tests/test_main.py, /docs/readme.txt")

# %% [markdown]
# ### GLOB search
#
# `GLOB` matches file paths against a shell-style pattern — metadata only, zero
# blob reads.  The pattern is evaluated against the full path string, not just
# the filename.  Try changing `**/*.py` to `**/*test*` and re-running the cell.

# %%
glob_results = await vfs.search(
    namespace_id=ns_id,
    query="**/*.py",
    scope="/src/",
    search_type=SearchType.GLOB,
    principal_id=admin_id,
)
print(f"GLOB **/*.py under /src/: {sorted(r.path for r in glob_results)}")

# %% [markdown]
# ### FIND search
#
# `FIND` matches against metadata predicates (`FindPredicates`): filename glob,
# size range, and modification time range.  Still metadata-only, zero blob reads.
# Try changing `name="test_*.py"` to `name="*.py"` to widen the match.

# %%
find_results = await vfs.search(
    namespace_id=ns_id,
    query="*",
    scope="/src/",
    search_type=SearchType.FIND,
    principal_id=admin_id,
    find_predicates=FindPredicates(name="test_*.py"),
)
print(f"FIND name=test_*.py: {[r.path for r in find_results]}")

# %% [markdown]
# ### REGEX search
#
# `REGEX` searches file content using a regular expression.  With SQLite FTS5
# active the index is consulted without reading any blobs.  Results include the
# matching line number and a snippet of the matched line.

# %%
regex_results = await vfs.search(
    namespace_id=ns_id,
    query=r"def \w+\(\)",
    scope="/src/",
    search_type=SearchType.REGEX,
    principal_id=admin_id,
)
print(f"REGEX 'def \\w+()':  {len(regex_results)} hit(s)")
for r in sorted(regex_results, key=lambda x: (x.path, x.line_number or 0)):
    print(f"  {r.path}:{r.line_number}  {r.match_context!r}")

# %% [markdown]
# ### FULLTEXT search (BM25)
#
# `FULLTEXT` ranks results by BM25 relevance using the FTS5 index — still zero
# blob reads.  Results are returned best-match first.  Try changing the query to
# `"hello"` to see both `main.py` (via `print('hello world')`) and `readme.txt`
# (via `"hello from readme"`) in the results.

# %%
ft_results = await vfs.search(
    namespace_id=ns_id,
    query="hello world",
    scope="/",
    search_type=SearchType.FULLTEXT,
    principal_id=admin_id,
)
print(f"FULLTEXT 'hello world': {[r.path for r in ft_results]}  (BM25-ranked)")

# %% [markdown]
# ## 6. Session — CWD and Relative Paths
#
# `Session` is a stateful wrapper around a VFS that tracks a current working
# directory (`cwd`).  Path arguments are resolved through `cwd` before the
# underlying VFS call — relative paths work exactly like a POSIX shell.  `cd`
# validates read permission on the target before changing `cwd`, so permission
# errors leave `cwd` unchanged.

# %%
session = Session(vfs, ns_id, admin_id)
print(f"cwd        : {session.pwd()!r}")

# %% [markdown]
# ### cd + list relative
#
# After `cd`, `list` with a relative path (like `"./"`) resolves against the
# new `cwd`.  The trailing slash is significant: it tells the VFS the argument
# is a directory prefix, which is required for list/search prefix matching.

# %%
await session.cd(path="/src/")
print(f"after cd /src/: {session.pwd()!r}")

src_files = await session.list(path_prefix="./")
print(f"list ./    : {sorted(f.path for f in src_files)}")

# %% [markdown]
# ### write via relative path
#
# `session.write` resolves `"session_new.py"` against `cwd` (`/src/`) to produce
# `/src/session_new.py`.  The write is otherwise identical to a direct `vfs.write`.

# %%
await session.write(path="session_new.py", content=b"# written via session relative path\n")
print("wrote session_new.py via relative path → /src/session_new.py")

# %% [markdown]
# ### cd .. then stat absolute
#
# `cd("..")` navigates up one directory.  After returning to `/`, we use an
# absolute path to confirm the file was written correctly.

# %%
await session.cd(path="..")
print(f"after cd ..: {session.pwd()!r}")

new_file = await session.stat(path="/src/session_new.py")
print(f"stat /src/session_new.py: version={new_file.current_version_number}")

# %% [markdown]
# ## 7. Execution (Monty sandbox)
#
# `vfs.execute` dispatches code to a sandboxed provider.  The only currently
# shipped provider is `"monty"` (`pydantic-monty` — a minimal Python interpreter
# written in Rust).  Shell operations (`ls`, `cat`, `grep`, `edit`, …) are
# injected as async external functions so the sandbox can read and write the VFS
# without direct storage access.
#
# **Error contract — two tiers:**
# - **Tier 1** (raises before dispatch): `ValueError`, `ImportError`, `PermissionDeniedError`.
# - **Tier 2** (returns `ExecutionResult(success=False, …)`): any exception during
#   execution is translated to a structured `error_type` string — no raw traceback.

# %%
# Guard: seed the workspace file used by the cells below.
if HAS_MONTY:
    limits = ResourceLimits(timeout_seconds=15.0)
    await vfs.write(
        namespace_id=ns_id,
        path="/ws/greet.py",
        content=b"# greeting module\ndef greet(name):\n    return f'Hello, {name}!'\n",
        principal_id=admin_id,
    )
    print("Monty sandbox ready — /ws/greet.py seeded")
else:
    print("pydantic-monty not installed — skipping execution cells")
    print("  Install with: uv sync  (monty is in the default dev group)")

# %% [markdown]
# ### simple expression
#
# The simplest sanity check: evaluate `1 + 2` in the sandbox and inspect the
# returned `ExecutionResult`.  `result.output` carries the final evaluated value.

# %%
if HAS_MONTY:
    result = await vfs.execute(
        code="1 + 2",
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=limits,
    )
    print(f"1 + 2 = {result.output}")

# %% [markdown]
# ### ls — list the VFS from inside the sandbox
#
# `ls()` is an async shell function injected into the sandbox.  It calls
# `session.list` with directory synthesis: paths with deeper nesting produce
# synthetic directory entries (`is_dir=True`).  The result is a plain JSON-like
# dict so the sandbox can marshal it without import restrictions.

# %%
if HAS_MONTY:
    result = await vfs.execute(
        code="await ls('/')",
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=limits,
    )
    names = [e["name"] for e in result.output["entries"]]
    print(f"ls /: {names}")

# %% [markdown]
# ### grep — regex search from inside the sandbox
#
# `grep(pattern, path)` is backed by `session.search(SearchType.REGEX)`.  On a
# fresh SQLite index it reads zero blobs.  Each hit includes the path, 1-based
# line number, and matching line context.

# %%
if HAS_MONTY:
    result = await vfs.execute(
        code="await grep('def greet', '/ws/')",
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=limits,
    )
    if result.success:
        print(f"grep 'def greet':  {len(result.output['results'])} hit(s)")
        for hit in result.output["results"]:
            print(f"  {hit['path']}:{hit['line_number']}  {hit['match_context']!r}")
    else:
        print(f"grep: {result.error_type} — {result.error_message}")

# %% [markdown]
# ### cat — read a file from inside the sandbox
#
# `cat(path)` returns `{"lines": [...], "error": None}` — the decoded UTF-8 lines
# of the file (or a structured error dict for binary/oversize content).

# %%
if HAS_MONTY:
    result = await vfs.execute(
        code="await cat('/ws/greet.py')",
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=limits,
    )
    if result.success:
        print(f"cat /ws/greet.py: {len(result.output['lines'])} line(s)")
        for line in result.output["lines"]:
            print(f"  {line!r}")
    else:
        print(f"cat: {result.error_type} — {result.error_message}")

# %% [markdown]
# ### write — native file editing from inside the sandbox
#
# Editing is plain Python I/O: the sandbox opens the file and writes new content.
# The write flows through the VFS with full versioning and permission checks.

# %%
if HAS_MONTY:
    code = "from pathlib import Path\nPath('/ws/greet.py').write_text('def greet(name, greeting=\"Hello\"):\\n')\n"
    result = await vfs.execute(
        code=code,
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=limits,
    )
    if result.success:
        print("native write: /ws/greet.py updated via Path.write_text")
    else:
        print(f"native write: {result.error_type} — {result.error_message}")

# %% [markdown]
# ### permission gate — Tier 1
#
# The `reader` principal lacks the `execute` permission on `/`.  The permission
# check happens in `vfs.execute` **before** any provider or sandbox is
# constructed — this is a Tier 1 error that raises `PermissionDeniedError` rather
# than returning an `ExecutionResult(success=False)`.

# %%
if HAS_MONTY:
    try:
        await vfs.execute(
            code="1 + 1",
            namespace_id=ns_id,
            principal_id=reader_id,
            provider_name="monty",
            resource_limits=limits,
        )
        print("unexpected: reader executed successfully")
    except PermissionDeniedError:
        print("PermissionDeniedError: reader lacks execute (Tier 1 gate, before dispatch)")

# %% [markdown]
# ### budget exceeded — Tier 2
#
# `ResourceLimits(max_operations=2)` allows only two shell-op calls.  Three
# `ls()` calls exhaust the budget.  The sandbox catches the internal
# `OperationBudgetExceededError` and translates it into a structured
# `ExecutionResult(success=False, error_type="budget_exceeded")` — no traceback
# propagates to the caller.

# %%
if HAS_MONTY:
    tight = ResourceLimits(timeout_seconds=10.0, max_operations=2)
    result = await vfs.execute(
        code="await ls('/')\nawait ls('/')\nawait ls('/')",
        namespace_id=ns_id,
        principal_id=admin_id,
        provider_name="monty",
        resource_limits=tight,
    )
    print(f"budget exceeded: success={result.success}  error_type={result.error_type!r}")

# %% [markdown]
# ## 8. Garbage Collection & Retention
#
# The GC prunes old versions according to `retention_max_recent` (set to 3 in
# `demo_setup`).  The policy keeps the current version plus up to
# `retention_max_recent` additional recent versions; older rows are reclaimed.
# Blob objects are only removed when no remaining version anywhere in the
# namespace references their content hash.

# %%
# Write 6 versions so GC has something to prune.
gc_path = "/gc_test/file.txt"
for i in range(6):
    await vfs.write(
        namespace_id=ns_id,
        path=gc_path,
        content=f"version {i}".encode(),
        principal_id=admin_id,
    )

before_gc = await vfs.versions(namespace_id=ns_id, path=gc_path, principal_id=admin_id)
print(f"before GC : {len(before_gc)} versions  {[v.version_number for v in before_gc]}")

# %%
gc = await vfs.run_gc(namespace_id=ns_id)
print(f"GC result : versions_reclaimed={gc.versions_reclaimed}  blobs_reclaimed={gc.blobs_reclaimed}")

after_gc = await vfs.versions(namespace_id=ns_id, path=gc_path, principal_id=admin_id)
print(f"after GC  : {len(after_gc)} versions remaining  {[v.version_number for v in after_gc]}")

# %% [markdown]
# ## Cleanup
#
# Run this cell when you are done exploring.  It closes the SQLite connections
# cleanly and removes the temp directory created in the Setup cell.

# %%
await vfs.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
print(f"cleaned up: {tmp_dir}")

# %%
