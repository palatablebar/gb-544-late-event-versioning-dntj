"""校验：行数账目 + 必填已由 transform 保证 + staging 与写入数对账。

对账口径：读取数 = 写入数 + 忽略数 + 拒绝数；
staging 行数 = 写入数 + 忽略数 + 写入侧拒绝数（转换失败的行不进 staging）。
"""
from __future__ import annotations

from typing import Any

from src.services import verify_service
from worker_src.constants.reason_codes import (
    REASON_CONFLICT_REJECTED,
    REASON_TRANSFORM_FAILED,
)


def verify_run(
    *,
    rows_read: int,
    staging_rows: int,
    written: int,
    skipped: int,
    rejected: list[dict[str, Any]],
    transform_rejected: int = 0,
) -> dict[str, Any]:
    load_rejected = len(rejected) - transform_rejected
    totals = verify_service.verify_totals(rows_read, written, skipped, len(rejected))
    codes: dict[str, int] = {}
    for item in rejected:
        key = item.get("reason_code") or REASON_TRANSFORM_FAILED
        codes[key] = codes.get(key, 0) + 1
    conflict_rejects = codes.get(REASON_CONFLICT_REJECTED, 0)
    staging_expected = written + skipped + load_rejected
    return {
        "ok": totals["ok"] and staging_rows == staging_expected,
        "totals": totals,
        "rejected_codes": codes,
        "conflict_rejects": conflict_rejects,
        "staging_rows": staging_rows,
        "staging_expected": staging_expected,
        "written": written,
        "skipped": skipped,
        "rejected": len(rejected),
    }


def final_status(rejected_count: int, error: str | None) -> str:
    from src.constants.enums import RunStatus

    if error:
        return RunStatus.FAILED.value
    if rejected_count > 0:
        return RunStatus.PARTIAL.value
    return RunStatus.SUCCEEDED.value
