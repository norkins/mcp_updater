from __future__ import annotations

import pytest

from mcp_smoke_test import cli
from mcp_smoke_test.client import SmokeTestError


async def _raise_wrapped_smoke_error(config):
    raise ExceptionGroup(
        "transport cleanup",
        [
            ExceptionGroup(
                "session cleanup",
                [
                    SmokeTestError(
                        "Metadata tool 'metadatasearch' used fallback search_layer=grep; expected vector+bm25."
                    )
                ],
            )
        ],
    )


async def _raise_unrelated_exception_group(config):
    raise ExceptionGroup("transport cleanup", [RuntimeError("boom")])


def test_main_returns_retryable_code_for_wrapped_smoke_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr("mcp_smoke_test.cli.run_smoke_test", _raise_wrapped_smoke_error)

    result = cli.main(["--url", "http://localhost:18100/mcp", "--timeout", "1"])

    captured = capsys.readouterr()
    assert result == 13
    assert "fallback search_layer=grep" in captured.out
    assert "ExceptionGroup" not in captured.out


def test_main_reraises_unrelated_exception_group(monkeypatch) -> None:
    monkeypatch.setattr("mcp_smoke_test.cli.run_smoke_test", _raise_unrelated_exception_group)

    with pytest.raises(ExceptionGroup) as exc:
        cli.main(["--url", "http://localhost:18100/mcp", "--timeout", "1"])

    assert isinstance(exc.value.exceptions[0], RuntimeError)
