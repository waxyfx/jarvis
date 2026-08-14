"""Filesystem tools. Every path passes the guard before anything touches disk."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Any

from atlas_agent.tools.base import ExecutionContext, ToolExecutionError, register_executor
from atlas_shared.tools.catalog import FsOpenArgs, FsSearchArgs

__all__ = ["open_file", "search_files"]

#: Bound on the walk, independent of how many results are asked for. A search
#: rooted at a huge tree must not become an unbounded scan.
_MAX_ENTRIES_SCANNED = 200_000


@register_executor("fs.search")
def search_files(args: FsSearchArgs, context: ExecutionContext) -> dict[str, Any]:
    """Find files by name under an allowed root."""
    root = context.path_guard.check(args.root)
    if not root.resolved.is_dir():
        raise ToolExecutionError("not_found", "search root is not a directory")

    pattern = args.query if any(ch in args.query for ch in "*?[") else f"*{args.query}*"
    pattern = pattern.lower()

    matches: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    # followlinks=False: a junction loop would otherwise walk forever, and a
    # junction out of the tree would silently widen the search.
    for directory, subdirectories, filenames in os.walk(root.resolved, followlinks=False):
        subdirectories[:] = [
            name
            for name in subdirectories
            if context.path_guard.is_allowed(os.path.join(directory, name))
        ]

        for filename in filenames:
            scanned += 1
            if scanned > _MAX_ENTRIES_SCANNED:
                truncated = True
                break
            if not fnmatch(filename.lower(), pattern):
                continue

            full_path = os.path.join(directory, filename)
            if not context.path_guard.is_allowed(full_path):
                continue

            try:
                stat_result = os.stat(full_path)
            except OSError:
                continue

            matches.append(
                {
                    "path": full_path,
                    "size_bytes": stat_result.st_size,
                    "modified": datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat(),
                }
            )
            if len(matches) >= args.max_results:
                truncated = True
                break

        if truncated:
            break

    return {
        "root": str(root.resolved),
        "query": args.query,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
    }


@register_executor("fs.open")
def open_file(args: FsOpenArgs, context: ExecutionContext) -> dict[str, Any]:
    """Open a file with its default application."""
    target = context.path_guard.check(args.path)
    if not target.resolved.exists():
        raise ToolExecutionError("not_found", "file does not exist")

    # os.startfile exists only on Windows; resolving it dynamically keeps this
    # module importable — and type-checkable — everywhere.
    start_file = getattr(os, "startfile", None)
    if start_file is None:  # pragma: no cover - non-Windows
        raise ToolExecutionError("unsupported", "opening files requires Windows")

    try:
        start_file(target.resolved)
    except OSError as exc:
        raise ToolExecutionError("open_failed", f"could not open the file: {exc}") from exc

    return {
        "path": str(target.resolved),
        "followed_reparse_point": target.followed_reparse_point,
    }
