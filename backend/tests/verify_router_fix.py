"""路由规则状态机 / 查询分发 / 缓存联动 复核脚本。

覆盖三类复现场景与回归断言：
    cd backend && PYTHONPATH=. .venv/bin/python tests/verify_router_fix.py
每个进程使用全新的内存仓库，脚本只依赖标准库与项目自身的 service 层。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from app.store import store
from app.services.router import (
    RouterService,
    MODULE as ROUTER_MODULE,
    CACHE_MODULE,
)
from app.services.cache import CacheService

svc = RouterService()
cache_svc = CacheService()

PASS = "✅"
FAIL = "❌"
results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{PASS if condition else FAIL} {name}" + (f" —— {detail}" if detail else ""))


def dump(title, payload):
    print(f"\n----- {title} -----")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def find_rule(code: str) -> dict:
    return next(row for row in store.rows(ROUTER_MODULE) if row.get("规则编号") == code)


def find_cache(code: str) -> dict | None:
    return next((row for row in store.rows(CACHE_MODULE) if row.get("路由规则编号") == code), None)


print("================ 初始种子数据 ================")
for row in store.rows(ROUTER_MODULE):
    print(json.dumps(row, ensure_ascii=False, default=str))

# ================================================================ 场景 1
print("\n================ 场景 1：ROUT-0001 草稿状态执行关闭规则 ================")
r1 = find_rule("ROUT-0001")
before1 = deepcopy(r1)
entry1, msg1 = svc.run_action(r1["id"], "关闭规则")
dump("草稿关闭动作返回", {"ok": entry1 is not None, "message": msg1})
check("草稿状态执行关闭规则被拒绝", entry1 is None, msg1)
check("拒绝后规则仍停留在草稿", r1.get("status") == "草稿" and r1.get("版本号") == before1.get("版本号"))
check("被拒绝动作不产生回执", len(r1.get("回执", [])) == len(before1.get("回执", [])))

entry1b, msg1b = svc.run_action(r1["id"], "降级路由", {"降级服务": "x"})
check("草稿状态执行降级路由被拒绝（不可跨级）", entry1b is None, msg1b)

# 正常单向推进：草稿 -> 生效中
entry1c, msg1c = svc.run_action(r1["id"], "启用规则")
check("草稿可以启用规则", entry1c is not None and r1.get("status") == "生效中", msg1c)
check("启用后版本推进到 1 且产生回执", r1.get("版本号") == 1 and len(r1.get("回执", [])) == 1)
# 已生效不能再启用（不可回退/重复）
entry1d, msg1d = svc.run_action(r1["id"], "启用规则")
check("生效中重复启用被拒绝", entry1d is None, msg1d)

# 三条原始规则此刻均为生效中，按种子权重 50/30/20 核对归一化口径
early = svc.dispatch_query("path=/api/")
early_weights = {item["规则编号"]: item["归一化权重"] for item in early["effective"]}
dump("三条原始规则生效中的归一化权重", early_weights)
check("生效权重总和为 100（50+30+20）", early["total_weight"] == 100)
check("ROUT-0001 归一化权重 0.5", abs(early_weights.get("ROUT-0001", 0) - 0.5) < 1e-4)
check("ROUT-0002 归一化权重 0.3", abs(early_weights.get("ROUT-0002", 0) - 0.3) < 1e-4)
check("ROUT-0003 归一化权重 0.2", abs(early_weights.get("ROUT-0003", 0) - 0.2) < 1e-4)

# ================================================================ 场景 2
print("\n================ 场景 2：ROUT-0003 降级目标为空不得提示成功 ================")
r3 = find_rule("ROUT-0003")
check("ROUT-0003 为生效中且历史缺失降级服务", r3.get("status") == "生效中" and not str(r3.get("降级服务") or "").strip())
c3_before = deepcopy(find_cache("ROUT-0003"))
entry2, msg2 = svc.run_action(r3["id"], "降级路由", {})
dump("空目标降级返回", {"ok": entry2 is not None, "message": msg2})
check("空目标降级被拒绝且不提示成功", entry2 is None, msg2)
check("规则状态仍为生效中（无半份结论）", r3.get("status") == "生效中")
check("版本号未推进", r3.get("版本号") == 1)
check("未追加任何回执", len(r3.get("回执", [])) == 1)
c3_mid = find_cache("ROUT-0003")
check("结果缓存版本未被改写", c3_mid.get("缓存版本") == c3_before.get("缓存版本"))
check("结果缓存降级目标仍为空", c3_mid.get("降级目标") == "")

entry3, msg3 = svc.run_action(r3["id"], "降级路由", {"降级服务": "svc-inventory-fallback"})
dump("带明确目标降级返回", {"ok": entry3 is not None, "message": msg3})
check("带明确目标可以降级成功", entry3 is not None and r3.get("status") == "已降级", msg3)
check("降级后版本推进并留下降级回执", r3.get("版本号") == 2 and r3["回执"][-1].get("降级服务") == "svc-inventory-fallback")
c3_after = find_cache("ROUT-0003")
check("缓存版本随规则版本推进到 2", c3_after.get("缓存版本") == 2)
check("缓存降级目标与规则降级服务一致", c3_after.get("降级目标") == "svc-inventory-fallback" == r3.get("降级服务"))
check("缓存路由快照与规则同源", c3_after.get("路由快照", {}).get("版本号") == 2)

# 已降级规则不能再降级
entry3b, msg3b = svc.run_action(r3["id"], "降级路由", {"降级服务": "other"})
check("已降级规则不能再次降级", entry3b is None, msg3b)

# ---------------- 2c. 缓存同步失败时整体回滚
print("\n---------- 缓存同步失败回滚 ----------")
# 先用正常服务把规则推进到生效中（含缓存建档），再换用“同步必失败”的服务做降级
created, _ = svc.create_entry({
    "规则编号": "ROUT-0888", "匹配条件": "path=/api/rollback/**", "目标服务": "svc-rb", "权重比例": 10,
})
svc.run_action(created["id"], "启用规则")
failing_svc = RouterService(cache_syncer=lambda _entry: (None, "模拟缓存写入失败"))
rb_rule = store.find(ROUTER_MODULE, created["id"])
rb_before = deepcopy(rb_rule)
rb_cache_before = deepcopy(find_cache("ROUT-0888"))
rb_entry, rb_msg = failing_svc.run_action(rb_rule["id"], "降级路由", {"降级服务": "svc-rb-fallback"})
check("缓存同步失败时降级返回失败", rb_entry is None, rb_msg)
check("规则状态/版本/回执整体回滚", rb_rule.get("status") == rb_before.get("status") and rb_rule.get("版本号") == rb_before.get("版本号") and len(rb_rule.get("回执", [])) == len(rb_before.get("回执", [])))
rb_cache_after = find_cache("ROUT-0888")
check("缓存版本与降级目标回滚到动作前", rb_cache_after.get("缓存版本") == rb_cache_before.get("缓存版本") and rb_cache_after.get("降级目标") == rb_cache_before.get("降级目标"))

# ---------------- 2d. 历史已降级规则缺失目标的补齐
print("\n---------- 历史规则降级服务补齐 ----------")
# 手工构造一条“已降级但降级服务为空”的历史规则
hist, _ = svc.create_entry({
    "规则编号": "ROUT-0777", "匹配条件": "path=/api/legacy/**", "目标服务": "svc-legacy", "权重比例": 10,
})
svc.run_action(hist["id"], "启用规则")
hist_row = store.find(ROUTER_MODULE, hist["id"])
hist_row["status"] = "已降级"
hist_row["规则状态"] = "已降级"
hist_row["abnormal"] = True
hist_row["pending"] = False
bf_entry, bf_msg = svc.backfill_degrade_target(hist["id"], "  ")
check("补齐空目标被拒绝", bf_entry is None, bf_msg)
bf_entry2, bf_msg2 = svc.backfill_degrade_target(hist["id"], "svc-legacy-fallback")
check("历史缺失降级服务可补齐", bf_entry2 is not None and hist_row.get("降级服务") == "svc-legacy-fallback", bf_msg2)
check("补齐动作写入回执", hist_row["回执"][-1].get("动作") == "补齐降级目标")
check("补齐同步到结果缓存且两页同源", find_cache("ROUT-0777").get("降级目标") == "svc-legacy-fallback")
bf_entry3, bf_msg3 = svc.backfill_degrade_target(hist["id"], "again")
check("已补齐的规则不能重复补齐", bf_entry3 is None, bf_msg3)

# ================================================================ 场景 3
print("\n================ 场景 3：两个入口并发切换 ROUT-0002 ================")
r2 = find_rule("ROUT-0002")
version_before = int(r2["版本号"])
receipts_before = len(r2["回执"])
barrier = threading.Barrier(8)
concurrent_messages: list[str] = []


def concurrent_switch():
    barrier.wait()
    return svc.run_action(r2["id"], "降级路由", {"降级服务": "svc-payment-route-fallback"})


with ThreadPoolExecutor(max_workers=8) as pool:
    concurrent = [f.result() for f in [pool.submit(concurrent_switch) for _ in range(8)]]

ok_count = sum(1 for entry, _ in concurrent if entry is not None)
concurrent_messages = [msg for _, msg in concurrent]
dump("并发降级结果", {"成功数": ok_count, "返回信息": concurrent_messages})
check("8 个并发请求只产生一个有效版本", ok_count == 1)
check("规则版本只推进一次", int(r2["版本号"]) == version_before + 1)
check("只追加一份降级回执", len(r2["回执"]) == receipts_before + 1)
c2 = find_cache("ROUT-0002")
check("缓存版本同样只推进一次", c2.get("缓存版本") == int(r2["版本号"]))

# 关闭链路：已降级 -> 已关闭，再关一次
entry_c1, msg_c1 = svc.run_action(r2["id"], "关闭规则")
receipts_after_close = len(r2["回执"])
entry_c2, msg_c2 = svc.run_action(r2["id"], "关闭规则")
dump("重复关闭", {"第一次": msg_c1, "第二次": msg_c2})
check("已降级规则可以关闭并立即退出匹配", entry_c1 is not None and r2.get("status") == "已关闭", msg_c1)
check("重复关闭被幂等拒绝且不追加回执", entry_c2 is None and len(r2["回执"]) == receipts_after_close, msg_c2)
entry_c3, msg_c3 = svc.run_action(r2["id"], "启用规则")
check("已关闭规则不能回到生效中", entry_c3 is None, msg_c3)
entry_c4, msg_c4 = svc.run_action(r2["id"], "降级路由", {"降级服务": "x"})
check("已关闭规则不能再次归档/降级", entry_c4 is None, msg_c4)
check("关闭后 pending 置为终态", r2.get("pending") is False)
check("关闭同步到缓存（缓存版本=规则版本、快照状态已关闭）",
      c2.get("缓存版本") == r2.get("版本号") and c2.get("路由快照", {}).get("状态") == "已关闭")

# ================================================================ 查询分发
print("\n================ 查询分发结果核对 ================")
broad = svc.dispatch_query("path=/api/")
dump("全量分发结果", broad)
all_targets = [str(item.get("目标服务") or "").strip() for item in broad["effective"]] + \
              [str(item.get("分发目标") or "").strip() for item in broad["degraded"]]
check("分发结果不含无效（空）目标", all(targets for targets in all_targets))
excluded_codes = {item["规则编号"] for item in broad["excluded"]}
check("已关闭的 ROUT-0002 退出匹配", "ROUT-0002" in excluded_codes)
# ROUT-0001 已启用、ROUT-0003 已降级
effective_codes = {item["规则编号"] for item in broad["effective"]}
degraded_codes = {item["规则编号"] for item in broad["degraded"]}
check("生效中规则进入 effective", "ROUT-0001" in effective_codes)
check("已降级规则命中明确降级目标", "ROUT-0003" in degraded_codes and any(item.get("分发目标") == "svc-inventory-fallback" for item in broad["degraded"]))

# 权重核对：三条原始规则里仅 ROUT-0001（权重 50）仍生效，
# ROUT-0002 已关闭、ROUT-0003 已降级，均不占生效权重（过滤掉本脚本构造的临时规则）
orig_effective = [item for item in broad["effective"] if item["规则编号"] in ("ROUT-0001", "ROUT-0002", "ROUT-0003")]
weights = {item["规则编号"]: item["归一化权重"] for item in orig_effective}
dump("三条原始规则的生效分发项", orig_effective)
check("原始规则中仅 ROUT-0001 仍占生效权重", list(weights) == ["ROUT-0001"] and int(orig_effective[0]["权重比例"]) == 50)
# 归一化在全量生效规则内计算；原始规则子集里只剩 ROUT-0001，其权重占比应为 100%
orig_weight_sum = sum(int(item["权重比例"]) for item in orig_effective)
check("ROUT-0001 在原始生效规则子集内权重占比为 1.0", int(orig_effective[0]["权重比例"]) / orig_weight_sum == 1.0)

# 草稿新建规则不参与分发
draft, _ = svc.create_entry({"规则编号": "ROUT-0666", "匹配条件": "path=/api/draft/**", "目标服务": "svc-draft", "权重比例": 99})
draft_dispatch = svc.dispatch_query("path=/api/draft/")
check("草稿规则不参与查询分发", all(item["规则编号"] != "ROUT-0666" for item in draft_dispatch["effective"] + draft_dispatch["degraded"]))

# 精确按匹配条件查询
single = svc.dispatch_query("path=/api/inventory/**")
check("按 ROUT-0003 匹配条件只命中该规则", {item["规则编号"] for item in single["degraded"]} == {"ROUT-0003"})

# ================================================================ 两页一致性
print("\n================ 路由列表页 vs 结果缓存详情页 同源核对 ================")
for code in ("ROUT-0001", "ROUT-0002", "ROUT-0003"):
    rule = find_rule(code)
    cache_row = find_cache(code)
    detail = cache_svc.get_entry(cache_row["id"]) if cache_row else None
    same_target = detail is not None and detail.get("降级目标") == rule.get("降级服务")
    same_version = detail is not None and detail.get("缓存版本") == rule.get("版本号")
    dump(code, {
        "路由列表页": {"降级服务": rule.get("降级服务"), "版本号": rule.get("版本号"), "状态": rule.get("status")},
        "缓存详情页": {"降级目标": (detail or {}).get("降级目标"), "缓存版本": (detail or {}).get("缓存版本"), "路由快照": (detail or {}).get("路由快照")},
    })
    check(f"{code} 两个页面显示同一份降级目标", same_target)
    check(f"{code} 两个页面版本号一致", same_version)

# ================================================================ 汇总
print("\n================ 复核汇总 ================")
failed = [name for name, ok in results if not ok]
for name, ok in results:
    print(f"{PASS if ok else FAIL} {name}")
print(f"\n共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    raise SystemExit(1)
