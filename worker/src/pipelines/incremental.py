"""增量过滤与水位线推进：真实比较 watermark_column。

迟到的补传行（更新时间 ≤ 上次水位线）不再被丢弃：与新鲜行一起返回，
由 load 按冲突键 + 更新时间归并——目标表里没有或版本更旧的迟到行照样补入，
已经同步过的同版本行在写入侧计入「忽略」，运行账目因此能对平。
"""
from __future__ import annotations

from typing import Any

from src.utils.watermark import compare, max_watermark, should_advance


def watermark_from(configured: Any, target_max: Any) -> Any:
    if configured not in (None, ""):
        return configured
    return target_max


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    sync_mode: str,
    watermark_column: str | None,
    watermark: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    """返回 (新鲜行, 迟到候选行, 本批最大水位线)。任何行都不会在这里被丢弃。"""
    if sync_mode != "incremental" or not watermark_column:
        values = [row.get(watermark_column) for row in rows] if watermark_column else []
        return rows, [], max_watermark(values) if watermark_column else None
    fresh: list[dict[str, Any]] = []
    late: list[dict[str, Any]] = []
    for row in rows:
        if should_advance(row.get(watermark_column), watermark):
            fresh.append(row)
        else:
            late.append(row)
    next_watermark = max_watermark([row.get(watermark_column) for row in fresh], watermark)
    return fresh, late, next_watermark


def resolve_update_column(
    watermark_column: str | None,
    mappings: list[dict[str, Any]],
) -> str | None:
    """把源侧水位线列翻译成目标表列名，作为写入侧的更新时间列。"""
    if not watermark_column:
        return None
    for mapping in mappings:
        if mapping.get("source_field") == watermark_column:
            return mapping.get("target_column")
    return None


def describe(watermark: Any, next_watermark: Any) -> str:
    from src.utils.formatters import watermark_text

    return watermark_text(watermark, next_watermark)


def is_later(left: Any, right: Any) -> bool:
    return compare(left, right) > 0
