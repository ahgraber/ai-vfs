# Search Specification

> Generated from design document analysis on 2026-04-04
> Source files: docs/specs/2026-04-04-ai-vfs-design.md (Sections 7, 3.3, 3.5)

## Purpose

Pluggable search over file paths and content.
Built-in support for glob, find, and regex grep.
Extensible via SearchProvider protocol for bloom filter acceleration and semantic search.
Search metadata stored per-version as extensible payload in the metadata store.

## Requirements

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

The system SHALL support predicate-based metadata search matching
file name patterns, size ranges, modification times, and type.

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

The system SHALL support multiple concurrent search providers, each declaring its capabilities (glob, find, regex, fulltext, semantic).
The VFS SHALL dispatch search requests to the provider with the matching capability.

#### Scenario: ProviderDispatch

- **GIVEN** a default provider (glob, find, regex) and a bloom provider (regex)
- **WHEN** a regex search is requested
- **THEN** the bloom provider is used (more specific acceleration)

#### Scenario: FallbackToDefault

- **GIVEN** only the default provider is active
- **WHEN** a regex search is requested
- **THEN** the default provider performs brute-force read-and-match

### Requirement: SearchIndexing

The system SHALL call each active search provider's index method during write.
The provider returns search artifacts (e.g., bloom hashes, embedding vectors) stored in the version's search_meta field.

#### Scenario: IndexOnWrite

- **GIVEN** a bloom search provider is active
- **WHEN** a file is written
- **THEN** bloom filter hashes are computed and stored in search_meta.bloom

#### Scenario: DefaultProviderNoIndex

- **GIVEN** only the default provider is active
- **WHEN** a file is written
- **THEN** index returns an empty dict (no indexing overhead)

### Requirement: SearchMetadataExtensible

The system SHALL store search artifacts per-version in an extensible dict field (JSONB in SQL, nested document in NoSQL).
The core schema SHALL NOT prescribe the contents — each provider writes its own keys.

#### Scenario: MultipleProviderArtifacts

- **GIVEN** bloom and semantic providers are both active
- **WHEN** a file is written
- **THEN** search_meta contains both {"bloom": ..., "vector": ...}

### Requirement: CoarseFineFilerPattern

The system SHOULD implement grep optimization using a coarse-filter/fine-filter
pattern: the search index (e.g., bloom filter) identifies candidate files,
then content verification confirms matches.

#### Scenario: BloomAcceleratedGrep

- **GIVEN** 1000 files in scope, bloom filter indicates 5 candidates
- **WHEN** a regex search is performed
- **THEN** only the 5 candidate files have their content read and verified

## Technical Notes

- **Implementation**: src/aifs/search/default.py, src/aifs/protocols/search.py
- **Dependencies**: file-operations (read for content grep), storage (MetadataStore for search_meta)
- **Shell ops layer**: Translates bash command signatures (grep, find, glob) into VFS search operations
