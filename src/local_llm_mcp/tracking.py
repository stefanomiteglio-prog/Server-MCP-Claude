"""Metadati di ogni chiamata di tool: riga di log su stderr e riga nel registro delle chiamate."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .calllog import CallLog
from .client import LLMError

logger = logging.getLogger("local_llm_mcp")


@asynccontextmanager
async def track(
    call_log: CallLog | None,
    tool: str,
    model: str,
    input_chars: int,
    found: Counter[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Logga (e salva nel registro delle chiamate) solo metadati: mai il contenuto."""
    rec: dict[str, Any] = {"tool": tool, "model": model, "input_chars": input_chars}
    if found:
        rec["redacted_secrets"] = sum(found.values())
    start = time.perf_counter()
    try:
        yield rec
        rec.setdefault("outcome", "ok")
    except LLMError as exc:
        rec["outcome"] = f"error:{exc.status or exc.error_type or 'llm'}"
        raise
    except Exception as exc:
        rec["outcome"] = f"error:{type(exc).__name__}"
        raise
    finally:
        rec["duration_ms"] = round((time.perf_counter() - start) * 1000)
        logger.info("tool_call %s", json.dumps(rec, ensure_ascii=False))
        if call_log is not None:
            call_log.record(rec)
