"""查询路由分发业务规则：状态流转、字段校验、查询分发与缓存联动都收在这里。

状态机（只能单向、逐级推进，不可回退、不可跨级）：
    草稿 --启用规则--> 生效中 --降级路由--> 已降级 --关闭规则--> 已关闭

关键约束：
- 关闭规则必须由“已降级”推进，关闭后立即退出查询分发匹配，且不能再回到生效中或再次归档；
- 降级路由必须带明确的、非空的降级目标服务，并留下降级回执；动作失败时
  规则状态、版本号与结果缓存版本整体回滚，不允许留下半份结论；
- 每次有效动作只推进一个版本，两个入口并发切换同一条规则时只有一个请求成功；
- 路由规则的版本与降级目标会同步到结果缓存加速模块，两个页面显示同一份数据。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from app.store import store

MODULE = "router"
CACHE_MODULE = "cache"
REQUIRED_FIELDS = ["规则编号", "匹配条件", "目标服务"]
STATUS_ORDER = ["草稿", "生效中", "已降级", "已关闭"]
# 动作 -> (允许的来源状态, 目标状态)
ACTION_RULES: dict[str, tuple[str, str]] = {
    "启用规则": ("草稿", "生效中"),
    "降级路由": ("生效中", "已降级"),
    "关闭规则": ("已降级", "已关闭"),
}
# 参与查询分发匹配的状态：草稿不可分发，已关闭立即退出匹配。
DISPATCHABLE_STATUSES = ("生效中", "已降级")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_cache_sync(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """把路由规则的最新版本同步到结果缓存加速模块。

    缓存配置以“路由规则编号”关联；不存在时自动建档。同步只在规则动作临界区内调用，
    并在这里按“路由锁 -> 缓存锁”的固定顺序加锁（不存在反向加锁路径，不会死锁）。
    返回 (缓存记录或 None, 错误信息)；失败时调用方负责整体回滚。
    """
    code = entry.get("规则编号")
    if not code:
        return None, "规则编号为空，无法同步结果缓存"
    with store.lock(CACHE_MODULE):
        rows = store.rows(CACHE_MODULE)
        cache_row = next((row for row in rows if row.get("路由规则编号") == code), None)
        created = False
        if cache_row is None:
            cache_row = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            created = True
        cache_row.update({
            "路由规则编号": code,
            "配置编号": cache_row.get("配置编号") or f"CACH-{int(cache_row['id']):04d}",
            "缓存键名": cache_row.get("缓存键名") or f"cache:{str(code).lower().replace('-', ':')}",
            "缓存版本": entry.get("版本号"),
            "降级目标": entry.get("降级服务") or "",
            "路由快照": {
                "规则编号": code,
                "版本号": entry.get("版本号"),
                "状态": entry.get("status"),
                "目标服务": entry.get("目标服务"),
                "降级服务": entry.get("降级服务") or "",
            },
        })
        if created:
            rows.append(cache_row)
    return cache_row, ""


class RouterService:
    def __init__(
        self,
        cache_syncer: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str]] | None = None,
    ) -> None:
        # 允许注入失败的缓存同步器，用于验证“同步失败时整体回滚”。
        self._cache_syncer = cache_syncer or default_cache_sync

    # ------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock(MODULE):
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["版本号"] = 0
            entry["权重比例"] = self._normalize_weight(values.get("权重比例"))
            entry["灰度标记"] = str(values.get("灰度标记") or "")
            entry["降级服务"] = str(values.get("降级服务") or "").strip()
            entry["重试次数"] = values.get("重试次数")
            entry["规则状态"] = STATUS_ORDER[0]
            entry["回执"] = []
            rows.append(entry)
        return entry, []

    # ------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        with store.lock(MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"路由规则 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于查询路由分发可执行范围"

            source_status, target_status = ACTION_RULES[action]
            current_status = str(entry.get("status") or "")
            if current_status == target_status:
                # 重复执行同一动作（例如重复关闭）直接幂等拒绝，不追加任何回执。
                return None, f"规则已是「{target_status}」状态，{action}不可重复执行"
            if current_status != source_status:
                allowed = STATUS_ORDER.index(source_status)
                current = STATUS_ORDER.index(current_status) if current_status in STATUS_ORDER else -1
                if current >= 0 and current > allowed:
                    return None, (
                        f"规则当前为「{current_status}」，状态只能按"
                        f"{'→'.join(STATUS_ORDER)}单向推进，不能回退到「{target_status}」"
                    )
                return None, f"规则当前为「{current_status}」，仅「{source_status}」状态可执行{action}"

            # 降级必须带明确的目标服务：空目标不得提示成功。
            fallback_target = str(
                values.get("降级服务") or entry.get("降级服务") or ""
            ).strip()
            if action == "降级路由" and not fallback_target:
                return None, "降级路由必须指定明确的降级目标服务，目标服务为空时拒绝降级"

            # 落库前先做快照：任何一步失败都按快照整体回滚，不留半份结论。
            rule_code = str(entry.get("规则编号"))
            entry_snapshot = deepcopy(entry)
            cache_snapshot = self._snapshot_cache(rule_code)

            try:
                entry["降级服务"] = fallback_target or entry.get("降级服务") or ""
                new_version = int(entry.get("版本号") or 0) + 1
                entry["版本号"] = new_version
                entry["status"] = target_status
                entry["规则状态"] = target_status
                entry["pending"] = target_status != STATUS_ORDER[-1]
                entry["abnormal"] = target_status == "已降级"
                receipt = self._build_receipt(action, new_version, target_status, values)
                entry.setdefault("回执", []).append(receipt)

                cache_row, cache_error = self._cache_syncer(entry)
                if cache_row is None:
                    raise RuntimeError(cache_error or "结果缓存同步失败")
            except RuntimeError as exc:
                # 回滚规则状态、版本与回执
                entry.clear()
                entry.update(entry_snapshot)
                self._restore_cache(rule_code, cache_snapshot)
                return None, f"{action}失败并已回滚：{exc}"

            return entry, f"路由规则已{action}（版本 {entry['版本号']}）"

    # ------------------------------------------------------------ 历史补齐
    def backfill_degrade_target(self, entry_id: int, fallback_target: str) -> tuple[dict[str, Any] | None, str]:
        """补齐历史规则缺失的降级服务。

        只允许给“已降级但降级服务为空”的历史规则补数据；补齐不推进规则版本，
        但会写入补齐回执并把同一份目标同步到结果缓存，保证两个页面同源。
        """
        fallback_target = str(fallback_target or "").strip()
        if not fallback_target:
            return None, "补齐的降级目标服务不能为空"
        with store.lock(MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"路由规则 {entry_id} 不存在或已归档"
            if entry.get("status") != "已降级":
                return None, "仅已降级的历史规则允许补齐降级目标"
            if str(entry.get("降级服务") or "").strip():
                return None, "该规则已存在降级目标，无需补齐"

            entry_snapshot = deepcopy(entry)
            rule_code = str(entry.get("规则编号"))
            cache_snapshot = self._snapshot_cache(rule_code)
            try:
                entry["降级服务"] = fallback_target
                receipt = {
                    "动作": "补齐降级目标",
                    "时间": _now(),
                    "版本号": entry.get("版本号"),
                    "降级服务": fallback_target,
                    "说明": "历史规则缺失降级服务，按运维补齐流程回填",
                }
                entry.setdefault("回执", []).append(receipt)
                cache_row, cache_error = self._cache_syncer(entry)
                if cache_row is None:
                    raise RuntimeError(cache_error or "结果缓存同步失败")
            except RuntimeError as exc:
                entry.clear()
                entry.update(entry_snapshot)
                self._restore_cache(rule_code, cache_snapshot)
                return None, f"补齐失败并已回滚：{exc}"
            return entry, "历史规则降级目标已补齐并同步结果缓存"

    # ------------------------------------------------------------ 查询分发
    def dispatch_query(self, query: str) -> dict[str, Any]:
        """按查询条件命中可分发规则并给出分发目标与归一化权重。

        - 草稿不参与分发；已关闭规则立即退出匹配；
        - 已降级规则命中其明确的降级目标，降级目标为空的规则视为无效数据剔除，
          因此返回结果里不会再出现无效（空）目标；
        - 生效中规则按权重比例归一化，用于核对 ROUT-0001~0003 的权重比例。
        """
        query = str(query or "").strip()
        effective: list[dict[str, Any]] = []
        degraded: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        with store.lock(MODULE):
            for row in store.rows(MODULE):
                status = str(row.get("status") or "")
                condition = str(row.get("匹配条件") or "")
                if query and query not in condition and condition not in query:
                    continue
                if status not in DISPATCHABLE_STATUSES:
                    excluded.append({"规则编号": str(row.get("规则编号")), "状态": status, "原因": "规则未参与分发匹配"})
                    continue
                if status == "已降级":
                    target = str(row.get("降级服务") or "").strip()
                    if not target:
                        excluded.append({"规则编号": str(row.get("规则编号")), "状态": status, "原因": "降级目标为空，剔除无效目标"})
                        continue
                    degraded.append({
                        "规则编号": row.get("规则编号"),
                        "状态": status,
                        "匹配条件": condition,
                        "降级服务": target,
                        "分发目标": target,
                        "版本号": row.get("版本号"),
                    })
                else:
                    effective.append({
                        "规则编号": row.get("规则编号"),
                        "状态": status,
                        "匹配条件": condition,
                        "目标服务": row.get("目标服务"),
                        "权重比例": self._normalize_weight(row.get("权重比例")),
                        "版本号": row.get("版本号"),
                    })

        total_weight = sum(int(item["权重比例"]) for item in effective)
        for item in effective:
            item["归一化权重"] = round(int(item["权重比例"]) / total_weight, 4) if total_weight else 0.0
        return {
            "query": query,
            "effective": effective,
            "degraded": degraded,
            "excluded": excluded,
            "total_weight": total_weight,
        }

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _normalize_weight(value: Any) -> int:
        try:
            weight = int(value)
        except (TypeError, ValueError):
            weight = 0
        return max(weight, 0)

    @staticmethod
    def _build_receipt(action: str, version: int, target_status: str, values: dict[str, Any]) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "动作": action,
            "时间": _now(),
            "版本号": version,
            "目标状态": target_status,
        }
        if action == "降级路由":
            receipt["降级服务"] = str(values.get("降级服务") or "").strip()
            receipt["说明"] = values.get("remark") or "规则降级，流量切换到降级目标服务"
        elif action == "关闭规则":
            receipt["说明"] = values.get("remark") or "规则关闭，立即退出查询分发匹配"
        else:
            receipt["说明"] = values.get("remark") or "规则启用，加入查询分发匹配"
        return receipt

    def _snapshot_cache(self, rule_code: str) -> dict[str, Any] | None:
        with store.lock(CACHE_MODULE):
            row = next(
                (item for item in store.rows(CACHE_MODULE) if item.get("路由规则编号") == rule_code),
                None,
            )
            return deepcopy(row) if row is not None else None

    @staticmethod
    def _restore_cache(rule_code: str, snapshot: dict[str, Any] | None) -> None:
        """按路由规则编号恢复缓存：原本没有记录就删掉新建的半成品，原本有就整体还原。"""
        with store.lock(CACHE_MODULE):
            rows = store.rows(CACHE_MODULE)
            if snapshot is None:
                rows[:] = [row for row in rows if row.get("路由规则编号") != rule_code]
                return
            for index, row in enumerate(rows):
                if int(row.get("id", 0)) == int(snapshot.get("id", 0)):
                    rows[index] = snapshot
                    return
