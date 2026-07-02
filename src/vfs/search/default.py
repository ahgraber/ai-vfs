"""Default search provider: glob, find, and regex."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from vfs.models import FileMeta, SearchArtifact, SearchResult, SearchType
from vfs.protocols.search import FindPredicates, SearchMetaEntry, SearchRequest, SearchResponse
from vfs.search._regex import RegexCompileError, compile_line_regex


class DefaultSearchProvider:
    """Built-in search provider supporting glob, find, and regex patterns.

    Glob and find are metadata-only (no blob reads).
    Regex fetches content on demand via ``request.read_content``; budget and
    scope enforcement are the reader's responsibility.

    ``index`` returns ``None`` — this provider produces no search artifacts.
    """

    def capabilities(self) -> set[SearchType]:
        """Return the set of search types this provider supports."""
        return {SearchType.GLOB, SearchType.FIND, SearchType.REGEX}

    async def index(self, path: str, content: bytes, metadata: FileMeta) -> SearchArtifact | None:
        """Return None; the default provider produces no search artifacts."""
        return None

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Search entries using glob, find, or regex strategy."""
        entries = request.search_metas
        if not entries:
            return SearchResponse()

        if request.search_type == SearchType.GLOB:
            return SearchResponse(results=self._glob_search(request.query, request.scope, entries))
        elif request.search_type == SearchType.FIND:
            return SearchResponse(
                results=self._find_search(request.query, request.scope, entries, request.find_predicates)
            )
        elif request.search_type == SearchType.REGEX:
            return SearchResponse(results=await self._regex_search(request.query, request.scope, entries, request))
        return SearchResponse()

    def _glob_search(self, pattern: str, scope: str, entries: list[SearchMetaEntry]) -> list[SearchResult]:
        recursive = "**" in pattern
        results = []
        for entry in entries:
            if not entry.path.startswith(scope):
                continue
            relative = entry.path[len(scope) :]
            # Non-recursive: only match direct children (no / in relative)
            if not recursive and "/" in relative:
                continue
            # PurePosixPath.match handles ** for 1+ directory depth but
            # fails when ** matches zero components (e.g. "a.py" vs "**/*.py",
            # or "pkg/b.py" vs "pkg/**/*.py").  Fall back to fnmatch with
            # **/ collapsed out of the pattern to cover the zero-depth case.
            matched = PurePosixPath(relative).match(pattern)
            if not matched and recursive:
                collapsed = pattern.replace("**/", "")
                matched = fnmatch.fnmatch(relative, collapsed)
            if matched:
                results.append(SearchResult(path=entry.path))
        return results

    def _find_search(
        self,
        query: str,
        scope: str,
        entries: list[SearchMetaEntry],
        find_predicates: FindPredicates | None = None,
    ) -> list[SearchResult]:
        """Match entries against the name query and optional find_predicates conjunctively.

        The ``query`` is the primary name fnmatch pattern (Phase 1 behavior, always applied).
        ``find_predicates`` supplies additional optional filters — name, size range,
        modification-time bounds, and live/tombstone type — that are combined conjunctively
        with each other and with the ``query`` name match.
        """
        results = []
        for entry in entries:
            if not entry.path.startswith(scope):
                continue
            name = PurePosixPath(entry.path).name
            # Primary name match (Phase 1 backward-compatible behavior)
            if not fnmatch.fnmatch(name, query):
                continue
            # Additional predicates from find_predicates (all optional, conjunctive)
            if find_predicates is not None:
                if find_predicates.name is not None and not fnmatch.fnmatch(name, find_predicates.name):
                    continue
                if find_predicates.size_min is not None and entry.size < find_predicates.size_min:
                    continue
                if find_predicates.size_max is not None and entry.size > find_predicates.size_max:
                    continue
                if find_predicates.mtime_after is not None and not (entry.updated_at > find_predicates.mtime_after):
                    continue
                if find_predicates.mtime_before is not None and not (entry.updated_at < find_predicates.mtime_before):
                    continue
                if find_predicates.type is not None:
                    if find_predicates.type == "file" and entry.is_deleted:
                        continue
                    if find_predicates.type == "tombstone" and not entry.is_deleted:
                        continue
            results.append(SearchResult(path=entry.path))
        return results

    async def _regex_search(
        self,
        pattern: str,
        scope: str,
        entries: list[SearchMetaEntry],
        request: SearchRequest,
    ) -> list[SearchResult]:
        try:
            compiled = compile_line_regex(pattern)
        except RegexCompileError:
            return []
        results = []
        for entry in entries:
            if not entry.path.startswith(scope):
                continue
            content = await request.read_content.read(entry.path)
            for line_num, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), start=1):
                if compiled.search(line):
                    results.append(
                        SearchResult(
                            path=entry.path,
                            line_number=line_num,
                            match_context=line.strip(),
                        )
                    )
        return results
