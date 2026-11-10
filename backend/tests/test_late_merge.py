"""迟到补传归并的纯逻辑测试：水位线过滤、批次内归并、鲜度分类、运行对账。

覆盖三条业务规则：
1. 迟到记录（更新时间 ≤ 上次水位线）不再被丢弃，仍能按业务键补入；
2. 每个业务对象（门店单品 / 会员）只保留更新时间最新的一版，旧内容不会盖掉新资料；
3. 运行账目对平：读取数 = 写入数 + 忽略数 + 拒绝数，staging 与写入侧计数一致。

直接运行：python3 backend/tests/test_late_merge.py
或用 pytest：pytest backend/tests/test_late_merge.py
"""
from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# worker/src 在容器里映射为 worker_src 包，测试里手动复刻这一映射
if "worker_src" not in sys.modules:
    worker_src = types.ModuleType("worker_src")
    worker_src.__path__ = [str(REPO_ROOT / "worker" / "src")]
    sys.modules["worker_src"] = worker_src

from src.services import merge_service  # noqa: E402
from src.utils import watermark  # noqa: E402
from worker_src.pipelines import incremental, verify  # noqa: E402


# ---------------------------------------------------------------- 水位线 ----


def test_coerce_native_types():
    assert watermark.coerce(datetime(2026, 11, 9, 12, 0)) == datetime(2026, 11, 9, 12, 0)
    assert watermark.coerce(None) is None
    assert watermark.coerce(1151) == 1151
    assert watermark.coerce("1151") == 1151


def test_coerce_datetime_strings():
    assert watermark.coerce("2026-11-09 12:00:00") == datetime(2026, 11, 9, 12, 0)
    assert watermark.coerce("2026-11-09 12:00:00.123456") == datetime(2026, 11, 9, 12, 0, 0, 123456)
    assert watermark.coerce("2026-11-09") == datetime(2026, 11, 9)


def test_compare_mixed_str_and_datetime():
    # 目标表取出来是 datetime，来源行是字符串：必须可比
    assert watermark.compare("2026-11-09 12:00:00", datetime(2026, 11, 9, 9, 0)) > 0
    assert watermark.compare("2026-11-09 08:00:00", datetime(2026, 11, 9, 9, 0)) < 0
    assert watermark.compare("2026-11-09 09:00:00", datetime(2026, 11, 9, 9, 0)) == 0


# ---------------------------------------------------------------- 增量过滤 ----


def test_filter_rows_keeps_late_rows():
    rows = [
        {"member_id": "M0001", "updated_at": "2026-11-03 10:00:00"},  # 迟到（≤ 水位线）
        {"member_id": "M0002", "updated_at": "2026-11-09 01:00:00"},  # 新鲜
    ]
    fresh, late, next_wm = incremental.filter_rows(
        rows, sync_mode="incremental", watermark_column="updated_at",
        watermark="2026-11-06 00:00:00",
    )
    assert [row["member_id"] for row in fresh] == ["M0002"]
    assert [row["member_id"] for row in late] == ["M0001"]  # 迟到行不再消失
    assert len(fresh) + len(late) == len(rows)
    assert str(next_wm) == "2026-11-09 01:00:00"  # 水位线只前进不后退


def test_filter_rows_full_mode_passthrough():
    rows = [{"member_id": "M0001", "updated_at": "2026-11-03 10:00:00"}]
    fresh, late, _next_wm = incremental.filter_rows(
        rows, sync_mode="full", watermark_column="updated_at", watermark="2026-11-06 00:00:00",
    )
    assert fresh == rows and late == []


def test_resolve_update_column():
    mappings = [
        {"source_field": "member_id", "target_column": "member_id"},
        {"source_field": "updated_at", "target_column": "updated_at"},
    ]
    assert incremental.resolve_update_column("updated_at", mappings) == "updated_at"
    assert incremental.resolve_update_column(None, mappings) is None
    assert incremental.resolve_update_column("not_mapped", mappings) is None


# ---------------------------------------------------------------- 批次内归并 ----


def test_dedupe_overwrite_keeps_freshest_not_last_arrival():
    # 同一会员三版乱序到达：最新版（11-05）必须留下，而不是最后到达的旧版（11-01）
    records = [
        {"member_id": "M0001", "tier": "普卡", "updated_at": "2026-11-03 10:00:00"},
        {"member_id": "M0001", "tier": "金卡", "updated_at": "2026-11-05 08:00:00"},
        {"member_id": "M0001", "tier": "银卡", "updated_at": "2026-11-01 09:00:00"},
    ]
    kept, conflicts, ignored = merge_service.dedupe_batch(
        records, ["member_id"], "overwrite", "updated_at"
    )
    assert len(kept) == 1 and kept[0]["tier"] == "金卡"
    assert conflicts == []
    assert len(ignored) == 2  # 被挤掉的两版计入忽略，账目才能对平


def test_dedupe_overwrite_tie_keeps_first():
    records = [
        {"member_id": "M0001", "tier": "普卡", "updated_at": "2026-11-03 10:00:00"},
        {"member_id": "M0001", "tier": "金卡", "updated_at": "2026-11-03 10:00:00"},
    ]
    kept, _conflicts, ignored = merge_service.dedupe_batch(
        records, ["member_id"], "overwrite", "updated_at"
    )
    assert kept[0]["tier"] == "普卡"
    assert len(ignored) == 1


def test_dedupe_reject_flags_duplicates():
    records = [
        {"order_id": "1151", "updated_at": "2026-11-10 01:12:00"},
        {"order_id": "1151", "updated_at": "2026-11-10 01:12:00"},
    ]
    kept, conflicts, ignored = merge_service.dedupe_batch(
        records, ["order_id"], "reject", "updated_at"
    )
    assert len(kept) == 1 and len(conflicts) == 1 and ignored == []
    assert conflicts[0]["reason_code"] == "conflict_rejected"


# ---------------------------------------------------------------- 鲜度分类 ----


def test_classify_freshness_against_target():
    existing = {
        ("M0001",): datetime(2026, 11, 1, 9, 0),   # 目标表里是 datetime
        ("M0002",): datetime(2026, 11, 1, 9, 4),
    }
    records = [
        {"member_id": "M0001", "updated_at": "2026-11-03 10:00:00"},  # 迟到但更新 → 补入
        {"member_id": "M0002", "updated_at": "2026-10-15 08:00:00"},  # 迟到旧件 → 忽略
        {"member_id": "M0003", "updated_at": "2026-11-02 12:00:00"},  # 目标没有 → 补入
    ]
    writable, stale = merge_service.classify_freshness(
        records, ["member_id"], "updated_at", existing
    )
    assert [row["member_id"] for row in writable] == ["M0001", "M0003"]
    assert [row["member_id"] for row in stale] == ["M0002"]


def test_classify_freshness_equal_version_is_ignored():
    # 同版本重复到达（夜里重跑同一批）：忽略而不是再写一遍，运行因此幂等
    existing = {("M0001",): datetime(2026, 11, 1, 9, 0)}
    records = [{"member_id": "M0001", "updated_at": "2026-11-01 09:00:00"}]
    writable, stale = merge_service.classify_freshness(
        records, ["member_id"], "updated_at", existing
    )
    assert writable == [] and len(stale) == 1


def test_classify_freshness_missing_update_time_writes():
    # 任一侧缺更新时间：无法证明过期，宁可写不可丢
    existing = {("M0001",): None}
    records = [{"member_id": "M0001", "updated_at": "2026-11-01 09:00:00"}]
    writable, stale = merge_service.classify_freshness(
        records, ["member_id"], "updated_at", existing
    )
    assert len(writable) == 1 and stale == []


# ---------------------------------------------------------------- 对账 ----


def test_verify_run_reconciles():
    # 读 10：转换拒 2 → staging 8；写入侧 写5 + 忽略2（1 迟到旧件 + 1 批内重复）+ 拒1
    result = verify.verify_run(
        rows_read=10,
        staging_rows=8,
        written=5,
        skipped=2,
        rejected=[{"reason_code": "conflict_rejected"}, {"reason_code": "required_missing"},
                  {"reason_code": "required_missing"}],
        transform_rejected=2,
    )
    assert result["ok"] is True
    assert result["totals"] == {"ok": True, "rows_read": 10, "accounted": 10, "delta": 0}
    assert result["staging_expected"] == 8


def test_verify_run_detects_imbalance():
    result = verify.verify_run(
        rows_read=10, staging_rows=8, written=5, skipped=1,
        rejected=[{"reason_code": "conflict_rejected"}], transform_rejected=0,
    )
    assert result["ok"] is False
    assert result["totals"]["delta"] == 10 - 7


# ---------------------------------------------------------------- 场景：会员补传夜批 ----


def _simulate_overwrite_run(source_rows, wm_from, target_states, key_columns, update_column):
    """用纯逻辑复刻 sync_task 的一次运行：过滤 → 批次内归并 → 鲜度分类 → 记账。"""
    fresh, late, next_wm = incremental.filter_rows(
        source_rows, sync_mode="incremental",
        watermark_column=update_column, watermark=wm_from,
    )
    batch = fresh + late
    kept, conflicts, batch_ignored = merge_service.dedupe_batch(
        batch, key_columns, "overwrite", update_column
    )
    writable, stale = merge_service.classify_freshness(
        kept, key_columns, update_column, target_states
    )
    written = len(writable)
    skipped = len(batch_ignored) + len(stale)
    rejected = len(conflicts)
    new_target = dict(target_states)
    for row in writable:
        new_target[tuple(str(row.get(column)) for column in key_columns)] = row[update_column]
    return {
        "read": len(batch), "written": written, "skipped": skipped, "rejected": rejected,
        "next_watermark": next_wm, "target": new_target,
    }


def test_member_late_backfill_merges_and_reconciles():
    """会员中心夜里补传：迟到行按 member_id 补入，每会员只留最新版，账目对平。"""
    target = {
        ("M0001",): datetime(2026, 11, 1, 9, 0),
        ("M0002",): datetime(2026, 11, 1, 9, 4),
    }
    source_rows = [
        {"member_id": "M0001", "tier": "普卡", "updated_at": "2026-11-03 10:00:00"},  # 迟到修正
        {"member_id": "M0001", "tier": "金卡", "updated_at": "2026-11-05 08:00:00"},  # 同对象更新版
        {"member_id": "M0002", "tier": "银卡", "updated_at": "2026-10-15 08:00:00"},  # 迟到旧件
        {"member_id": "M0003", "tier": "金卡", "updated_at": "2026-11-02 12:00:00"},  # 迟到新对象
        {"member_id": "M0004", "tier": "银卡", "updated_at": "2026-11-09 01:00:00"},  # 新鲜行
    ]
    result = _simulate_overwrite_run(
        source_rows, "2026-11-06 00:00:00", target, ["member_id"], "updated_at"
    )
    # 迟到行没有消失：读 5 = 写 3 + 忽略 2（批内挤掉 1 + 迟到旧件 1）+ 拒 0
    assert result["read"] == 5
    assert result["written"] == 3
    assert result["skipped"] == 2
    assert result["rejected"] == 0
    assert result["read"] == result["written"] + result["skipped"] + result["rejected"]
    # 每个会员都是最新一版：M0001 是 11-05 的金卡，M0002 没被 10-15 的旧件盖掉
    assert result["target"][("M0001",)] == "2026-11-05 08:00:00"
    assert result["target"][("M0002",)] == datetime(2026, 11, 1, 9, 4)
    assert result["target"][("M0003",)] == "2026-11-02 12:00:00"
    assert str(result["next_watermark"]) == "2026-11-09 01:00:00"

    # 第二晚原样重跑同一批：全部同版本 → 全部忽略，写 0，运行幂等
    rerun = _simulate_overwrite_run(
        source_rows, str(result["next_watermark"]), result["target"], ["member_id"], "updated_at"
    )
    assert rerun["written"] == 0
    assert rerun["rejected"] == 0
    assert rerun["read"] == rerun["skipped"] == 5


def test_store_item_late_backfill_merges_and_reconciles():
    """门店销售补传：按 门店+单品 归并，迟到旧件不盖新资料。"""
    target = {
        ("S003", "SKU0001"): datetime(2026, 11, 9, 9, 3),
        ("S004", "SKU0001"): datetime(2026, 11, 9, 10, 0),
    }
    source_rows = [
        # 迟到但比目标新 → 补入
        {"store_no": "S003", "sku": "SKU0001", "amount": "99.0", "updated_at": "2026-11-09 12:00:00"},
        # 目标没有的新单品 → 补入
        {"store_no": "S003", "sku": "SKU0002", "amount": "15.5", "updated_at": "2026-11-08 23:00:00"},
        # 比目标旧 → 忽略，目标保持 10:00 那版
        {"store_no": "S004", "sku": "SKU0001", "amount": "42.0", "updated_at": "2026-11-09 08:00:00"},
    ]
    result = _simulate_overwrite_run(
        source_rows, "2026-11-09 18:57:00", target, ["store_no", "sku"], "updated_at"
    )
    assert result["read"] == 3
    assert result["written"] == 2
    assert result["skipped"] == 1
    assert result["read"] == result["written"] + result["skipped"] + result["rejected"]
    assert result["target"][("S003", "SKU0001")] == "2026-11-09 12:00:00"
    assert result["target"][("S004", "SKU0001")] == datetime(2026, 11, 9, 10, 0)
    assert result["target"][("S003", "SKU0002")] == "2026-11-08 23:00:00"


# ---------------------------------------------------------------- 入口 ----


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
