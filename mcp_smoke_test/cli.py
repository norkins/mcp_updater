from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from .client import SmokeTestError, load_smoke_config, run_smoke_test
from .result import SmokeToolConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MCP smoke tests.")
    parser.add_argument("--config", help="Path to JSON smoke test config", default=None)
    parser.add_argument("--url", required=False, help="MCP server URL")
    parser.add_argument("--timeout", required=False, type=int, help="Timeout in seconds")
    parser.add_argument("--index-code", action="store_true", help="Require codesearch checks")
    parser.add_argument("--diagnostic", action="store_true", help="Print progress diagnostics to stderr")
    parser.add_argument("--metadata-tool", default="metadatasearch")
    parser.add_argument("--metadata-query-argument", default="query")
    parser.add_argument("--metadata-query", action="append", default=[])
    parser.add_argument(
        "--allow-metadata-fallback",
        action="store_true",
        help="Allow metadata search fallback layers instead of requiring vector+bm25.",
    )
    parser.add_argument("--code-tool", default="codesearch")
    parser.add_argument("--code-query-argument", default="query")
    parser.add_argument("--code-query", action="append", default=[])
    parser.add_argument(
        "--allow-code-fallback",
        action="store_true",
        help="Allow code search fallback layers instead of requiring vector+bm25.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _resolve_config(args)
        if args.diagnostic:
            config.diagnostic = True
        result = asyncio.run(run_smoke_test(config))
        print(
            json.dumps(
                {
                    "tools": result.listed_tools,
                    "statsOk": result.stats_ok,
                    "metadataOk": result.metadata_ok,
                    "codeOk": result.code_ok,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except SmokeTestError as exc:
        print(str(exc))
        return 13
    except ExceptionGroup as exc:
        smoke_error = _find_smoke_test_error(exc)
        if smoke_error is None:
            raise
        print(str(smoke_error))
        return 13
    except TimeoutError:
        print("MCP tool smoke-test timed out.")
        return 13


def _resolve_config(args) -> SmokeToolConfig:
    if args.config:
        return load_smoke_config(Path(args.config))

    if not args.url or args.timeout is None:
        raise SmokeTestError("Either --config or both --url and --timeout must be provided.")

    return SmokeToolConfig(
        url=args.url,
        timeout_seconds=args.timeout,
        overall_timeout_seconds=args.timeout,
        index_code=bool(args.index_code),
        diagnostic=bool(args.diagnostic),
        metadata_tool_name=args.metadata_tool,
        metadata_query_argument=args.metadata_query_argument,
        metadata_queries=list(args.metadata_query),
        code_tool_name=args.code_tool,
        code_query_argument=args.code_query_argument,
        code_queries=list(args.code_query),
        require_metadata_vector_index=not bool(args.allow_metadata_fallback),
        require_code_vector_index=not bool(args.allow_code_fallback),
    )


def _find_smoke_test_error(exc: BaseException) -> SmokeTestError | None:
    if isinstance(exc, SmokeTestError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _find_smoke_test_error(nested)
            if found is not None:
                return found
    return None
