# Storage — Delta Spec

> Change: `object-store-text-index`
> Date: 2026-06-11

## ADDED Requirements

### Requirement: ObjectStoreTextIndexSubstrate

The system MAY persist a native text-search index split across the configured `BlobStore` and the metadata store, gated by configuration and disabled by default.
Postings SHALL be stored as **immutable, size-bounded, content-addressed segment objects** in the `BlobStore` (keyed by the BLAKE3 hash of the segment bytes, reusing the sharded blob layout and idempotent put), where each segment carries its own term dictionary, per-term postings, and per-document term frequencies, and stays within a configured byte budget so that whole-segment reads remain bounded.
The **live-segment manifest** — naming the segments currently live for a `(namespace_id, params_hash)` partition — SHALL be persisted in the **metadata store** under compare-and-swap, **not** in the content-addressed `BlobStore` (whose `put` is hash-keyed and idempotent-no-ops on an existing key, and whose cache assumes immutable values).
A sealed segment SHALL NOT be mutated after creation; publishing new content and compaction SHALL write new segments and CAS-republish the manifest, never edit a sealed segment in place.
Segment publication SHALL follow the order **write the segment object → CAS-update the manifest → backfill version artifacts**, so a manifest update that loses a CAS race is retried against the current manifest and no live segment is dropped.
Segment objects SHALL be excluded from the blob-orphan GC sweep — they are not content blobs — and SHALL be reclaimed only by a dedicated index sweep.
The index sweep SHALL leave **no live segment indexing orphaned content**: a segment all of whose content hashes are orphaned (no retained version references) or whose `params_hash` is retired SHALL be dropped, and a segment with a mix of live and orphaned content SHALL be **compacted** (rewritten without the orphaned postings and republished) within the same sweep — so orphaned text does not persist in the index past a GC pass.
The stored index text and postings SHALL be treated at the same confidentiality level as blob content.

#### Scenario: DisabledByDefault

- **GIVEN** no object-store text index is configured
- **WHEN** the VFS is initialized
- **THEN** no index substrate is provisioned and native-capability resolution falls back to the metadata store alone

#### Scenario: ManifestInMetadataStoreUnderCas

- **GIVEN** an enabled object-store text index
- **WHEN** a segment is published for a `(namespace_id, params_hash)` partition
- **THEN** the live-segment manifest is updated in the metadata store via compare-and-swap, and is never written to the content-addressed `BlobStore`

#### Scenario: SegmentsAreContentAddressedAndSizeBounded

- **GIVEN** content being sealed into the index
- **WHEN** a segment is written to the `BlobStore`
- **THEN** it is keyed by the hash of its own bytes under the sharded layout, is immutable, and respects the configured segment byte budget

#### Scenario: ConcurrentPublishNoLostSegment

- **GIVEN** two `materialize` passes each sealing a segment for the same partition
- **WHEN** both attempt to publish
- **THEN** the manifest CAS serializes them; the loser retries against the updated manifest and both segments end up live — neither is dropped, so no published content becomes a permanent false negative

#### Scenario: IndexObjectsExcludedFromBlobGc

- **GIVEN** index segment objects and content blobs coexist in one `BlobStore`
- **WHEN** the blob-orphan GC sweep enumerates and reclaims orphaned content blobs
- **THEN** index segment objects are not enumerated as content blobs and are never reclaimed by the blob-orphan sweep

#### Scenario: FullyOrphanedSegmentDropped

- **GIVEN** a sealed segment all of whose content hashes have no retained version references
- **WHEN** the index sweep runs
- **THEN** the segment is dropped and removed from the manifest

#### Scenario: PartiallyOrphanedSegmentCompactedForErasure

- **GIVEN** a sealed segment indexing both live content and content whose last referencing version was reclaimed
- **WHEN** the index sweep runs
- **THEN** the segment is compacted — rewritten without the orphaned content's postings and republished — so no live segment indexes the orphaned content after the sweep

#### Scenario: SegmentReclaimedWhenParamsRetired

- **GIVEN** sealed segments whose `params_hash` belongs to a retired index profile
- **WHEN** the index sweep runs
- **THEN** those segments are dropped regardless of whether their content hashes are still referenced under the active profile
