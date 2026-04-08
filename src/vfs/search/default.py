"""Default search provider: glob, find, and regex."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
import re

from vfs.models import FileMeta, SearchResult, SearchType
from vfs.protocols.search import ContentFetcher


class DefaultSearchProvider:
    """Built-in search provider supporting glob, find, and regex patterns.

    Glob and find are metadata-only (no blob reads).
    Regex fetches content on demand via the ``fetch_content`` callback.
    """

    def capabilities(self) -> set[SearchType]:
        """Return the set of search types this provider supports."""
        return {SearchType.GLOB, SearchType.FIND, SearchType.REGEX}

    async def index(self, path: str, content: bytes, metadata: FileMeta) -> dict:
        """Return empty metadata; the default provider produces no search artifacts."""
        return {}

    async def search(
        self,
        query: str,
        scope: str,
        search_type: SearchType,
        candidates: list[FileMeta] | None = None,
        fetch_content: ContentFetcher | None = None,
    ) -> list[SearchResult]:
        """Search candidates using glob, find, or regex strategy."""
        if candidates is None:
            return []

        if search_type == SearchType.GLOB:
            return self._glob_search(query, scope, candidates)
        elif search_type == SearchType.FIND:
            return self._find_search(query, scope, candidates)
        elif search_type == SearchType.REGEX:
            return await self._regex_search(query, scope, candidates, fetch_content)
        return []

    def _glob_search(self, pattern: str, scope: str, candidates: list[FileMeta]) -> list[SearchResult]:
        recursive = "**" in pattern
        results = []
        for meta in candidates:
            if not meta.path.startswith(scope):
                continue
            relative = meta.path[len(scope) :]
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
                results.append(SearchResult(path=meta.path))
        return results

    def _find_search(self, query: str, scope: str, candidates: list[FileMeta]) -> list[SearchResult]:
        results = []
        for meta in candidates:
            if not meta.path.startswith(scope):
                continue
            name = PurePosixPath(meta.path).name
            if fnmatch.fnmatch(name, query):
                results.append(SearchResult(path=meta.path))
        return results

    async def _regex_search(
        self,
        pattern: str,
        scope: str,
        candidates: list[FileMeta],
        fetch_content: ContentFetcher | None,
    ) -> list[SearchResult]:
        compiled = re.compile(pattern)
        results = []
        for meta in candidates:
            if not meta.path.startswith(scope):
                continue
            if fetch_content is None:
                continue
            content = await fetch_content(meta.path)
            for line_num, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), start=1):
                if compiled.search(line):
                    results.append(
                        SearchResult(
                            path=meta.path,
                            line_number=line_num,
                            match_context=line.strip(),
                        )
                    )
        return results
