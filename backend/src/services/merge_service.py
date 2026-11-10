"""业务对象归并：批次内去重与按更新时间的鲜度分类（纯函数，不碰数据库）。

迟到数据治理的两条规则都落在这里：
1. 同一业务对象（冲突键）只保留 `update_column` 最新的一版——批次内如此，与目标表比较也如此。
2. 目标表里已有更新或同等版本的行不再重复写入，计入「忽略」（rows_skipped），
   保证 读取数 = 写入数 + 忽略数 + 拒绝数 始终能对平。
"""
from __future__ import annotations

from typing import Any

from src.constants.enums import ConflictStrategy
from src.utils.watermark import compare


def _key_of(record: dict[str, Any], key_columns: list[str]) -> tuple:
    return tuple(str(record.get(column)) for column in key_columns)


def _is_fresher(challenger: Any, incumbent: Any) -> bool:
    """challenger 是否严格比 incumbent 新；任一侧缺失时无法判新，返回 False。"""
    if challenger in (None, "") or incumbent in (None, ""):
        return False
    return compare(challenger, incumbent) > 0


def dedupe_batch(
    records: list[dict[str, Any]],
    key_columns: list[str],
    strategy: str,
    update_column: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """同批次内重复键处理，返回 (保留行, 冲突拒绝项, 被忽略行)。

    - overwrite：保留更新时间最新的一条（无更新时间列时退化为保留最后一条），
      被挤掉的重复行进「忽略」桶——它们被读过但没写入，必须计入忽略数才能对账；
      更新时间相等时保留先到的一条，保证结果确定。
    - skip / reject：保留第一条，后续重复行产出 conflict_rejected 冲突。
    """
    if not key_columns:
        return records, [], []
    seen: dict[tuple, int] = {}
    kept: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    replace_on_dup = strategy == ConflictStrategy.OVERWRITE.value
    for record in records:
        key = _key_of(record, key_columns)
        if key in seen:
            if replace_on_dup:
                index = seen[key]
                if update_column:
                    # 只被严格更新的版本替换；挑战者没有更新时间时不替换
                    if _is_fresher(record.get(update_column), kept[index].get(update_column)):
                        ignored.append(kept[index])
                        kept[index] = record
                    else:
                        ignored.append(record)
                else:
                    ignored.append(kept[index])
                    kept[index] = record
            else:
                conflicts.append(
                    {
                        "reason_code": "conflict_rejected",
                        "reason_detail": (
                            f"batch_duplicate conflict_key={'+'.join(key_columns)} value={key}"
                        ),
                        "raw_payload": record.get("__raw_payload__", record),
                        "source_row_index": record.get("__source_row_index__", 0),
                    }
                )
            continue
        seen[key] = len(kept)
        kept.append(record)
    return kept, conflicts, ignored


def classify_freshness(
    records: list[dict[str, Any]],
    key_columns: list[str],
    update_column: str,
    existing_states: dict[tuple, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按目标表现状把记录分成 (可写, 过期忽略)。

    可写：键不存在（迟到补传的新对象）、本行更新时间严格更新（迟到修正）、
    或任一侧更新时间缺失（无法证明过期，宁可写不可丢）。
    过期忽略：目标表已有更新或同等版本——迟到旧件不再覆盖新资料，计入忽略数。
    """
    writable: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for record in records:
        key = _key_of(record, key_columns)
        if key not in existing_states:
            writable.append(record)
            continue
        incoming = record.get(update_column)
        stored = existing_states[key]
        if incoming in (None, "") or stored in (None, ""):
            writable.append(record)
            continue
        if compare(incoming, stored) > 0:
            writable.append(record)
        else:
            stale.append(record)
    return writable, stale
