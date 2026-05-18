# Search — Delta Spec

> Change: `phase1-core`
> Date: 2026-04-04

## ADDED Requirements

### Requirement: GlobSearch

The system SHALL support glob pattern matching against file paths within a scoped directory, with optional recursion.

#### Scenario: NonRecursiveGlob

- **GIVEN** files /src/main.py, /src/utils.py, /src/tests/test_main.py
- **WHEN** a principal searches with glob pattern "\*.py" in scope /src/
- **THEN** /src/main.py and /src/utils.py are returned (not the nested file)

#### Scenario: RecursiveGlob

- **GIVEN** files /src/main.py, /src/tests/test_main.py
- **WHEN** a principal searches with glob pattern "\*\*/\*.py" in scope /src/
- **THEN** both files are returned

### Requirement: FindSearch

The system SHALL support predicate-based metadata search matching file name patterns.

> **Phase 1 scope:** Phase 1 implements only the name-pattern predicate (fnmatch against the file basename).
> Richer predicates (size ranges, modification times, type) are deferred to `phase2-adapters` alongside the `SearchRequest` protocol redesign, where the multi-predicate input shape can be defined once.

#### Scenario: FindByNamePattern

- **GIVEN** files /src/main.py and /src/config.yaml
- **WHEN** a principal searches with find predicate name="\*.py"
- **THEN** only /src/main.py is returned

### Requirement: RegexContentSearch

The system SHALL support regex pattern matching against file content,
returning matching paths with context (matched line and line number).

#### Scenario: GrepMatchesContent

- **GIVEN** file /src/main.py contains "# TODO: fix this" on line 3
- **WHEN** a principal searches with regex "TODO" in scope /src/
- **THEN** a result with path=/src/main.py, line_number=3, and match_context containing "fix this" is returned

#### Scenario: GrepNoMatch

- **GIVEN** no files in scope contain the pattern
- **WHEN** a principal searches with regex "NONEXISTENT"
- **THEN** an empty result list is returned

### Requirement: PluggableSearchProviders

The system SHALL define a `SearchProvider` protocol so that search implementations are pluggable by type rather than hard-coded into the VFS.
The VFS SHALL dispatch a search request to the active provider when that provider declares the requested capability, and SHALL raise an error otherwise.

Providers that need file content (e.g. regex grep, bloom-prefiltered search) SHALL receive a `fetch_content` callback from the VFS rather than pre-loaded content.
The callback lazily fetches blob bytes for a given path, allowing providers to control which files they read and when.
Metadata-only strategies (glob, find) ignore the callback entirely.

> **Phase 1 scope:** Phase 1 ships a single bundled `DefaultSearchProvider` (glob, find, regex).
> Multi-provider runtime dispatch — registering bloom, semantic, or fulltext providers alongside the default and routing each search request to the most specific match — is deferred to `phase2-adapters/PluggableSearchProviders`, which lands together with the bloom and semantic providers.

#### Scenario: SingleProviderDispatch

- **GIVEN** the default provider is active
- **WHEN** a regex search is requested
- **THEN** the default provider performs brute-force read-and-match via `fetch_content`

#### Scenario: UnknownCapabilityRejected

- **GIVEN** the active provider does not declare the SEMANTIC capability
- **WHEN** a principal requests a semantic search
- **THEN** a `ValueError` is raised indicating no provider supports the requested search type

### Requirement: SearchIndexing

The system SHALL call each active search provider's index method during write.
The provider returns search artifacts (e.g., bloom hashes, embedding vectors) stored in the version's search_meta field.

#### Scenario: DefaultProviderNoIndex

- **GIVEN** only the default provider is active
- **WHEN** a file is written
- **THEN** index returns an empty dict (no indexing overhead)

### Requirement: SearchMetadataExtensible

The system SHALL store search artifacts per-version in an extensible dict field (JSON text in SQLite).
The core schema SHALL NOT prescribe the contents — each provider writes its own keys.

#### Scenario: EmptySearchMetaByDefault

- **GIVEN** only the default provider is active
- **WHEN** a file is written
- **THEN** search_meta is an empty dict `{}`
