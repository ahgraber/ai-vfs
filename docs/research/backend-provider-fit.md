# Backend provider fit — research catalog

> **Purpose.** Research input for a later design decision, not a recommendation to adopt or drop any provider.
> Each entry scores one candidate against the contracts the specs already define: match, partial, or non-match per role, and whether the fit is within the current spec or needs a spec change.
>
> **Baseline.** Scored against the `specs/` baseline after the 2026-06-22 search realignment, catalogued 2026-06-25.
> The search contract is still settling, so re-check search verdicts when it moves.
> Links point to named requirements, which outlast line numbers.
>
> **Method.** The comparison is against the contracts, not against how SQLite or Postgres implement them.
> Service capabilities are read from architecture, not a tested integration; verify the specifics (transactions, wire version, index behavior, quotas) against current vendor docs before building an adapter, as the storage spec already requires for document stores ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)).

## Three roles, one contract each

A backend plays up to three roles, each with its own contract: it stores **metadata** (the file tree and its history), stores **blobs** (the file bytes), or serves **search**.
Most candidates fill one or two.
The roles are scored independently because a store can be excellent at one and useless at another.

### Metadata: the source of truth for files and versions

The metadata store holds everything except the bytes — paths, versions, permissions, the audit trail.
Its consistency guarantees matter more than its throughput, and the contract names three.

- **Compare-and-swap on version writes** ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)).
  When two writers race to create version 6 of a file, the store admits one and rejects the other by checking "still at version 5?"
  in the same step that writes 6.
  This atomic check-then-write is the primitive the system coordinates on, so it sits in the floor.
- **Literal path-prefix matching** ([PrefixQueryLiteralMatching](../../.specs/specs/storage/spec.md#requirement-prefixqueryliteralmatching)).
  Listing `/my_dir/` matches that exact prefix; `_` and `%` are text, not wildcards, so `my_dir` never matches `myXdir`.
- **Substitutability across two families** — SQL and MongoDB-style document stores.
  The contract is the _intersection_ of what both can do, so either can stand in for the other.
  SQL has multi-step transactions; standalone MongoDB does not, so multi-step atomicity is a bonus, not a requirement ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)) — only single-step compare-and-swap is mandatory.
  This is what runs the same code on a laptop (SQLite) and a cluster (Postgres or Mongo).

A new metadata store either joins one of the two families and slots in through an existing scheme, or it forces a new contract.

### Blob: content-addressed bytes, mostly an "S3?" question

The blob store keeps file bytes under a content hash, so identical content is stored once.
The contract is small: put, get, delete, exists, and list-all-hashes for garbage collection, with bytes returned verbatim ([BlobStoreProtocol](../../.specs/specs/storage/spec.md#requirement-blobstoreprotocol), [BlobEnumeration](../../.specs/specs/storage/spec.md#requirement-blobenumeration)), under a sharded `{hash[0:2]}/{hash[2:4]}/{hash}` key ([BlobPrefixDirectoryStructure](../../.specs/specs/storage/spec.md#requirement-blobprefixdirectorystructure)).
Object stores already do this, so the role reduces to one question: does the candidate speak S3?

### Search: the index must commit inside the metadata transaction

Search decides most of the verdicts, because the spec allows only one way to do it well, and that way rules out every external engine.

Files can be searched five ways: **glob** (path patterns), **find** (metadata such as size, mtime, live-versus-deleted), **regex** (a substring in content), **fulltext** (whole words, ranked), and **semantic** (vector similarity).
Semantic is reserved but unbuilt, unsupported on every backend ([PluggableSearchProviders](../../.specs/specs/search/spec.md#requirement-pluggablesearchproviders)), so a vector-first candidate is scored against an empty slot.

Glob and find read only metadata, so every backend serves them.
Regex and fulltext need the contents indexed, and the spec offers two paths.

The **native** path indexes text in the same transaction that writes the version ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
A SQL store derives two indexes from one stored copy of the text: trigram for regex and substring, non-stemming word-tokens for fulltext ([FulltextWordRepresentation](../../.specs/specs/search/spec.md#requirement-fulltextwordrepresentation)).
A write is searchable the instant it commits, search answers from stored text with no blob reads, and a missing or stale index makes search fail loud and demand a reindex rather than guess ([ColdIndexFailsLoud](../../.specs/specs/search/spec.md#requirement-coldindexfailsloud)).
An agent that writes a file and immediately searches for it must find it, and must never get a half-right answer.

The **fallback** path covers stores that cannot index, such as a standalone MongoDB ([PluggableSearchProviders](../../.specs/specs/search/spec.md#requirement-pluggablesearchproviders)): regex reads files one at a time up to a budget, and fulltext is unsupported.

Two facts drive every search verdict.
The index lives inside the metadata store and commits with the version, so the freshness model assumes the entry exists whenever it should ([SearchArtifactEnvelope](../../.specs/specs/search/spec.md#requirement-searchartifactenvelope)).
And search receives the permission-filtered set of visible versions and expands each match across it ([NativeTextSearchCapability](../../.specs/specs/search/spec.md#requirement-nativetextsearchcapability)), so the access check is part of search.

An external engine breaks both.
It runs as a separate server that updates after the write, so it lags and a search can miss a just-written file.
And it returns its own hits, which must be re-filtered against the caller's visible set in code, a place to leak access.
Supporting one means a third, weaker regime the spec lacks.
The spec restricts the native capability to relational stores and bars document stores from it ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)); an outside service sits further still from the model.

## Summary

Role fit is match, partial, or none.
"Fits current spec" is yes when the candidate is substitutable under an existing contract, no when it needs a new or changed one.

| Provider / family                                                                                                               | Role assessed                | Contract it maps to                                          | Fits current spec?                                                  | Verdict                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| **Postgres and Postgres-wire** (Cockroach, Yugabyte, Cosmos-PG, AlloyDB, Neon, Supabase)                                        | Metadata, plus native search | Relational floor; native search via `tsvector` and `pg_trgm` | Yes for metadata; native search yes only where the extensions exist | Metadata: match. Native search: partial, engine-dependent                                     |
| **SQLite**                                                                                                                      | Metadata, plus native search | Relational floor; FTS5 native search                         | Yes, shipped exemplar                                               | Match (the reference)                                                                         |
| **MySQL / MariaDB**                                                                                                             | Metadata, plus native search | Relational floor, new adapter                                | Metadata yes; native search no, semantics differ                    | Metadata: match. Native search: non-match as specified                                        |
| **MongoDB and Mongo-wire** (Cosmos-Mongo, DocumentDB, FerretDB)                                                                 | Metadata                     | Document floor                                               | Yes, shipped exemplar                                               | Metadata: match. Native search: not applicable by design                                      |
| **DuckDB**                                                                                                                      | Metadata, or embedded search | Neither cleanly                                              | No                                                                  | Metadata: non-match (analytics engine, not point writes). Search: partial, only all-in-DuckDB |
| **S3 and S3-compatible** (MinIO, Ceph, SeaweedFS, Garage, R2, B2, Wasabi)                                                       | Blob                         | `BlobStoreProtocol`                                          | Yes, covered by `s3://`                                             | Match                                                                                         |
| **Azure Blob / GCS**                                                                                                            | Blob                         | `BlobStoreProtocol`, non-S3 wire                             | Contract yes; needs a new adapter                                   | Match, thin adapter                                                                           |
| **OPFS** (browser, Pyodide)                                                                                                     | Blob                         | `BlobStoreProtocol`, WASM adapter                            | Contract yes; needs an adapter and an in-browser stack              | Match, browser profile                                                                        |
| **pgvector / pgvectorscale**                                                                                                    | Search (semantic)            | In-transaction native path, extends Postgres                 | No semantic contract yet, but no new regime needed                  | Cleanest semantic path                                                                        |
| **sqlite-vec**                                                                                                                  | Search (semantic)            | In-transaction native path, extends SQLite                   | No semantic contract yet                                            | Local semantic, limited at scale                                                              |
| **Azure AI Search**                                                                                                             | Search (fulltext, semantic)  | Out-of-band regime that does not exist yet                   | No, plus cloud-only                                                 | Fulltext and semantic capable; native in-transaction non-match                                |
| **External engines** (Qdrant, Weaviate, Milvus, Chroma, LanceDB, Elasticsearch/OpenSearch, Typesense, Meilisearch, Redis Stack) | Search (fulltext, semantic)  | Out-of-band regime that does not exist yet                   | No                                                                  | Capable, but not substitutable for the native path                                            |

The final section lists weaker fits and why each was set aside.

---

## Metadata candidates

### Postgres and Postgres-wire-compatible

Postgres meets the metadata floor in full: ACID writes, compare-and-swap through `WHERE version_number = ?` ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)), a real transaction context, and literal prefix queries.
It also carries native search, with a `pg_trgm` index for regex and a `tsvector` for fulltext, both computed inline so no migration is needed ([FulltextMatchMode](../../.specs/specs/search/spec.md#requirement-fulltextmatchmode)).

The Postgres-wire family rides the same `postgresql://` adapter: CockroachDB, YugabyteDB, Cosmos DB for PostgreSQL, AlloyDB, Neon, Supabase, TimescaleDB.
They satisfy the metadata contract with little or no new code, the way Cosmos for MongoDB rides the Mongo scheme ([URIBasedStoreResolution](../../.specs/specs/storage/spec.md#requirement-uribasedstoreresolution)).
Two per-engine checks shape the verdict.
The distributed engines (Cockroach, Yugabyte) default to serializable isolation and pay cross-node latency; compare-and-swap holds, but throughput and retries differ from single-node Postgres.
Native search needs both `pg_trgm` and `tsvector`: stock Postgres, Citus, AlloyDB, Neon, and Supabase have them; Cockroach added them only recently; an engine missing either is a metadata store with no native search, falling back to brute-force regex and unsupported fulltext.

Verdict: metadata, match.
Native search, match on stock Postgres and Citus, partial on the distributed forks pending an extension check.

### SQLite

The default local store and the search reference.
Embedded, no operator, meets the relational floor, and provides both index representations through FTS5: a trigram table for substring and regex, and a `unicode61` word-token table for fulltext ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
Everything else is measured against it.

Verdict: match, the reference.

### MySQL / MariaDB

Metadata fits cleanly.
InnoDB gives ACID writes, row locking, real transactions, and the compare-and-swap shape the floor needs.
The work is a new async adapter (`aiomysql` or `asyncmy`) and a `mysql://` scheme, with no change to the floor.
MariaDB also ships a native vector type, which only matters to the absent semantic slot.

Native search is where the fit breaks.
The spec wants non-stemming word tokens with no minimum length, so `s3` is matchable, plus a trigram index for substring and regex ([FulltextWordRepresentation](../../.specs/specs/search/spec.md#requirement-fulltextwordrepresentation), [NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
MySQL and MariaDB fulltext indexes default to a minimum token length, carry stopwords, and split natural-language from boolean matching, and they have no `pg_trgm` equivalent for arbitrary substring search.
Mapping those onto the spec's two representations would change what a query matches.
So as specified, the cleanest fit is a metadata store with no native search: regex falls back to brute force, fulltext is unsupported, unless the search spec grows a MySQL-shaped representation.

Verdict: metadata, match with a new adapter.
Native search, non-match under the current representation contract.

### MongoDB and Mongo-wire-compatible

The document-family reference, defining the other half of the floor.
Compare-and-swap runs through `find_one_and_update` with a version match ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)), and transactions are best-effort, real only on a replica set, which is why multi-document atomicity sits outside the floor ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)).
By design it exposes no native search: `native_text_search()` returns `None`, regex falls back to brute force, fulltext is unsupported.
Mongo has a text index and Atlas Search, but the spec keeps them out to hold the document family at the floor.

The Mongo-wire family rides `mongodb://`: Azure Cosmos DB for MongoDB (request-unit limits, version-specific transaction caps), Amazon DocumentDB, and the Postgres-backed FerretDB.
Verify transaction support and wire version per target.

Verdict: metadata, match on the document floor.
Native search, not applicable by design.

### DuckDB

DuckDB fits neither role cleanly.
It is an embedded analytics engine: columnar, tuned for bulk scans, single-writer with coarse concurrency.
The metadata workload is the opposite — a row per version, concurrent writers, compare-and-swap on every write — so per-version point writes are an anti-pattern and it is a poor metadata store.
Its search angle is real but narrow: a fulltext extension and a vector-similarity extension exist, but only in an all-in-DuckDB design where it is both store and index, which is a different architecture, not a drop-in for either role.

Verdict: metadata, non-match (analytics, not point writes).
Search, partial, only inside an all-DuckDB design the current regimes do not cover.

---

## Blob candidates

### S3 and S3-compatible

The blob reference.
S3 maps straight onto the contract: verbatim bytes under the sharded key layout, idempotent put, enumeration for garbage collection ([BlobStoreProtocol](../../.specs/specs/storage/spec.md#requirement-blobstoreprotocol)), with the diskcache wrapper enabled automatically for remote stores ([BlobCaching](../../.specs/specs/storage/spec.md#requirement-blobcaching)).

Most self-hosted object stores are free here.
MinIO, Ceph (RADOS gateway), SeaweedFS, Garage, Cloudflare R2, Backblaze B2, and Wasabi all expose the S3 API, so the existing `s3://` adapter covers them with no new code — the blob counterpart to the Postgres-wire and Mongo-wire groupings.
Confirm signature version and multipart quirks per implementation.

Verdict: match.

### Local filesystem

The default blob exemplar (`file:///`), same content-hash layout on disk, the baseline the remote stores are measured against.

Verdict: match.

### Azure Blob and GCS

Both fit the contract; only the wire protocol differs.
Each stores verbatim bytes under an arbitrary key, so the semantics map cleanly — idempotent put, exists, enumerate, sharded keys.
Neither speaks S3, so each needs a thin adapter and scheme (`az://`, `gs://`).
GCS offers an S3-compatible XML API that could ride `s3://` in a pinch, but a native adapter is cleaner.
Purely additive, no change to the floor.

Verdict: match, thin new adapter.

### OPFS (Origin Private File System)

The blob target for a browser or WASM profile, connected to the Pyodide and OPFS sandboxing research.
OPFS gives an origin-scoped sandboxed file store reachable from WASM, and verbatim bytes keyed by content hash satisfy the contract.
The frictions are about deployment, not the contract: async-only, quota-limited, origin-scoped, no server-side sharing, and useful only if the rest of the stack also runs in the browser (for example SQLite-WASM for metadata).
A workable adapter, but part of a larger client-side deployment story rather than a standalone swap.

Verdict: match for a browser profile, new adapter, tied to a WASM deployment design.

---

## Search candidates

### In-transaction vector: pgvector and sqlite-vec

Only an engine that _is_ the metadata store can index in the version's transaction ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)), which means a Postgres or SQLite extension.
These extend search without inventing a new consistency regime.
They still need the semantic contract written, and they add an embedding step: embeddings are derived artifacts, so they must be reproducible, with the model and dimensions folded into the index's parameter hash, and they default to CPU unless a GPU cost case is made.

**pgvector / pgvectorscale** runs vector similarity inside Postgres (HNSW and IVFFlat indexes; pgvectorscale adds a disk-based ANN index for larger corpora).
This is the cleanest path to semantic search: an embedding keyed by content hash commits in the version transaction and behaves as a sibling of the native capability, inheriting its freshness model unchanged — content-addressed identity, a fresh index that answers authoritatively, a stale one that fails loud.
No new regime.
The costs are the semantic contract and the embedding pipeline, and it exists only where Postgres holds the metadata.

**sqlite-vec** is the same idea for the local profile: vector search as a SQLite extension, written in transaction.
It does flat brute-force nearest-neighbor search with no ANN graph, so it holds up at small scale and weakens as the corpus grows.
It keeps the property that a laptop runs the same contract as production.

The current Postgres `tsvector`/`pg_trgm` and SQLite FTS5 indexes are this tier's reference, not new candidates.

Verdict: the substitutable semantic path, pending a semantic contract and an embedding pipeline; pgvector for servers, sqlite-vec for local, scale-limited.

### External engines: capable, but each needs a new regime

Qdrant, Weaviate, Milvus, Chroma, LanceDB, Elasticsearch and OpenSearch, Typesense, Meilisearch, and Redis Stack all run as their own store, separate from the metadata store.
None can be the native capability, which is in-store and in-transaction by definition ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
Each would plug into the async search-provider slot ([SearchProviderProtocol](../../.specs/specs/search/spec.md#requirement-searchproviderprotocol)) that the metadata-only default provider occupies today, which opens the same three gaps for every engine.

- **Freshness.**
  The native model assumes the index entry is present whenever a file's artifact is current, because they were written together ([SearchArtifactEnvelope](../../.specs/specs/search/spec.md#requirement-searchartifactenvelope)).
  An eventually consistent index breaks read-after-write and lags silently instead of failing loud.
  Closing this needs a new provider regime with an explicit weaker contract, or a readability check plus a pending artifact state.
- **Access control.**
  The native path expands a match across the permission-filtered versions it was handed ([NativeTextSearchCapability](../../.specs/specs/search/spec.md#requirement-nativetextsearchcapability)).
  An external engine returns its own hits, which must be re-filtered against the visible set in code, or filtered by pushing visible version IDs into the query and risking the engine's filter-size limits.
  Either way is a place a visibility leak can occur, and access control is the system's first priority.
- **Regex.**
  The spec's regex is exact substring matching over stored bytes on a trigram index.
  Token-based engines approximate this awkwardly through keyword-tokenized fields and slow, complexity-limited wildcard or regex queries, the mismatch documented for Azure AI Search.

In return they offer what the spec does not yet require: hybrid ranking that fuses keyword and vector scores, and approximate nearest-neighbor search at scale.
That makes them the natural home for a future semantic or hybrid contract, not a substitute for the native fulltext and regex path.

| Engine                       | Self-host (license)             | Embedded mode            | Lexical    | Vector / ANN        | Hybrid                | Notes for contract fit                                                                                                   |
| ---------------------------- | ------------------------------- | ------------------------ | ---------- | ------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **Qdrant**                   | Yes (Apache-2.0)                | No, server               | Weak       | Yes (HNSW)          | Yes, sparse and dense | Strong payload filtering eases in-code permission filtering                                                              |
| **Weaviate**                 | Yes (BSD-3)                     | No, server               | Yes (BM25) | Yes                 | Yes                   | Modules for embedding generation                                                                                         |
| **Milvus**                   | Yes (Apache-2.0)                | Lite (embedded)          | Weak       | Yes, many ANN types | Partial               | Heavy distributed operations at scale; vector-first                                                                      |
| **Chroma**                   | Yes (Apache-2.0)                | Yes, in-process          | Weak       | Yes                 | Partial               | Simplest; small scale; embedded mode fits the local profile                                                              |
| **LanceDB**                  | Yes (Apache-2.0)                | Yes, object-store-native | Yes (FTS)  | Yes                 | Yes                   | Distinct: Lance columnar on local or S3 fits the laptop-to-S3 story, but still out-of-band from the metadata transaction |
| **Elasticsearch**            | Yes (SSPL/Elastic, AGPL option) | No, server               | Strong     | Yes (kNN)           | Yes                   | Closest self-hosted analogue to Azure AI Search; heavy operations                                                        |
| **OpenSearch**               | Yes (Apache-2.0)                | No, server               | Strong     | Yes (kNN)           | Yes                   | The permissively licensed Elasticsearch fork                                                                             |
| **Typesense**                | Yes (GPL-3.0)                   | No, server               | Yes        | Yes                 | Yes                   | Lightweight and fast; small footprint                                                                                    |
| **Meilisearch**              | Yes (MIT)                       | No, server               | Yes        | Yes, newer          | Yes                   | Lightweight; vector and hybrid still maturing                                                                            |
| **Redis Stack (RediSearch)** | Yes (RSALv2/SSPL)               | No, server               | Yes        | Yes                 | Partial               | Attractive only when Redis is already in the stack                                                                       |

Verdict: individually capable, and the right substrate for a future hybrid or semantic contract, but not substitutable for the native, in-transaction fulltext and regex path.
Each needs the same new out-of-band regime, and vector use needs the semantic contract.

### Azure AI Search

Azure AI Search has a separate deep-dive; in catalog terms it is an out-of-band, cloud-only member of the group above, with one extra constraint: no self-hosted or embedded mode, so it can never be the local profile.
Its fulltext (BM25, all-terms and any-term modes) maps well and arguably exceeds the contract.
Its semantic search (vectors, hybrid ranking, a reranker) is its strength and the main reason to reach for it if a semantic contract gets written.
Its regex is an impedance mismatch, and it cannot be the native capability because it is non-transactional and eventually consistent.

Verdict: fulltext and semantic capable; native in-transaction non-match; needs the new regime; cloud-only.

---

## Weaker fits, set aside

| Candidate                            | Considered as       | Set aside because                                                                                                                                                                                                                                             |
| ------------------------------------ | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **DuckDB as metadata**               | Metadata store      | Columnar, single-writer, tuned for analytics; per-version point writes are an anti-pattern. Fails the workload, not the protocol shape. Its search angle is noted above.                                                                                      |
| **Redis as durable metadata**        | Metadata store      | In-memory first, so durability and consistency do not meet the metadata floor. Works only as the cache layer, or through RediSearch as an out-of-band search engine.                                                                                          |
| **Cassandra / ScyllaDB**             | Metadata store      | Wide-column model does not fit path-prefix listing. Compare-and-swap exists through lightweight transactions, but adopting it adds a third metadata family and renegotiates the floor.                                                                        |
| **DynamoDB**                         | Metadata store      | Conditional writes give compare-and-swap, but it is cloud-only key-value, neither Postgres- nor Mongo-wire, so a new family and adapter with no native-search story.                                                                                          |
| **FoundationDB**                     | Metadata store      | A strong transactional ordered key-value store, but no SQL or Mongo wire, so projecting files, versions, and permissions onto raw key-value is a large adapter.                                                                                               |
| **Firestore**                        | Metadata store      | Cloud document store, not Mongo-wire, so a new family, with eventual-consistency edges to verify.                                                                                                                                                             |
| **Neo4j and graph stores**           | Metadata store      | Graph model does not fit the file, version, and permission shape.                                                                                                                                                                                             |
| **SurrealDB / ArangoDB / Couchbase** | Metadata and search | Multi-model engines tempt one store into two roles, the floor-violating union the spec warns against. Each role is weaker than a dedicated store, and adopting one renegotiates the metadata family. Revisit only if single-engine deployment becomes a goal. |
| **Pinecone**                         | Search (vector)     | Cloud-only managed vector store, ruled out by the self-hostable preference; otherwise an out-of-band group member.                                                                                                                                            |
| **Vespa**                            | Search              | Powerful but operationally heavy; folds into the out-of-band group without changing the verdict.                                                                                                                                                              |
| **Manticore / Sphinx**               | Search              | Lexical engine with no contract advantage over OpenSearch or Typesense; an out-of-band group member.                                                                                                                                                          |

---

## Bottom line: search is the only constrained role

Metadata has two families and a literal floor.
New relational engines (MySQL, MariaDB) and wire-compatible ones (the Postgres-likes, the Mongo-likes) slot in without renegotiating the floor; everything else — wide-column, graph, key-value, cloud document — adds a third family.

Blob is almost entirely whether a store speaks S3.
The self-hosted object stores come for free, and Azure Blob, GCS, and OPFS are thin additive adapters.

Search is the constraint.
The spec defines a native in-transaction path and a brute-force fallback, and no third path.
Only Postgres and SQLite extensions (pgvector, sqlite-vec) extend search natively, and even they need the semantic contract written first.
Every external engine — Azure AI Search and the whole out-of-band group — is a capable index that needs a new, weaker provider regime the spec lacks, plus in-code permission filtering to hold the access boundary.

The decision this sets up: adding vector or semantic search through a Postgres or SQLite extension stays inside the existing consistency model, while any external engine introduces a new one.
That difference, more than raw capability, is what the specs care about.
