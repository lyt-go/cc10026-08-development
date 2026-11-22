"""数据归并领域服务：管理多API数据源的归并、冲突处理和去重策略。

跨模块数据流：
- 消费 api_registry 快照 -> 归并源API核验
- 消费 data_filter 快照 -> 各源数据归并前先过滤
- 被 export_task 消费 -> 归并后数据导出
- 归并结果写入共享快照
- 正式批次发布/撤回/更正 -> 签发撤回回执给 export_task 识别关联稿件
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataMergeService(BaseModuleService):
    MODULE = "data_merge"
    ENTITY = "归并规则"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["规则编号", "主API", "从API", "归并键", "冲突策略", "去重策略", "结果排序", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "主API", "从API"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {
        "提交校验": "待校验",
        "标记生效": "已生效",
        "停用规则": "已停用",
        "发布正式批次": "已生效",
        "撤回正式批次": "已生效",
        "登记更正来源": "已生效",
        "发布更正批次": "已生效",
    }
    # 这些动作不改变规则自身状态，而是驱动批次台账及其下游登记。
    BATCH_ACTIONS = (
        "发布正式批次", "发布批次",
        "撤回正式批次", "撤回批次", "撤回",
        "登记更正来源", "补齐更正来源",
        "发布更正批次",
    )
    BATCH_ALIASES = {
        "发布批次": "发布正式批次",
        "撤回批次": "撤回正式批次",
        "撤回": "撤回正式批次",
        "补齐更正来源": "登记更正来源",
    }
    NEGATIVE_ACTIONS = ["停用规则", "撤回正式批次"]

    def merge_sources(self, rule_id: int, source_a_rows: list[dict[str, Any]],
                      source_b_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按归并规则合并两个数据源。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None or rule.get("status") != "已生效":
            return source_a_rows + source_b_rows

        merge_key = rule.get("归并键", "id")
        conflict_policy = rule.get("冲突策略", "主源优先")
        dedup_policy = rule.get("去重策略", "全保留")

        merged = {}
        for row in source_a_rows:
            key = str(row.get(merge_key, ""))
            merged[f"a_{key}"] = {"_source": "primary", **row}

        for row in source_b_rows:
            key = str(row.get(merge_key, ""))
            pk = f"a_{key}"
            if pk in merged:
                if conflict_policy == "主源优先":
                    continue
                elif conflict_policy == "从源覆盖":
                    merged[pk] = {"_source": "secondary", **row}
                elif conflict_policy == "合并字段":
                    merged[pk].update({"from_secondary": row})
            else:
                merged[f"b_{key}"] = {"_source": "secondary", **row}

        result = list(merged.values())

        # 去重
        if dedup_policy == "按键去重":
            seen = set()
            deduped = []
            for row in result:
                key = str(row.get(merge_key, ""))
                if key not in seen:
                    seen.add(key)
                    deduped.append(row)
            result = deduped

        # 排序
        sort_field = rule.get("结果排序", "")
        if sort_field:
            result = sorted(result, key=lambda r: str(r.get(sort_field, "")))

        return result

    def merge_and_audit(self, rule_id: int, source_a_rows: list[dict[str, Any]],
                        source_b_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """归并两个数据源并写入审计记录。

        跨模块增强：归并完成后自动写入call_audit审计。
        """
        merged = self.merge_sources(rule_id, source_a_rows, source_b_rows)
        rule = store.find(self.MODULE, rule_id)

        # 联动增强：归并结果写入审计
        from app.services.data_manager import write_audit, DATA_ROOT
        audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": rule.get("主API", "") if rule else "",
            "caller": "data_merge",
            "event": "merge_completed",
            "primary_count": len(source_a_rows),
            "secondary_count": len(source_b_rows),
            "merged_count": len(merged),
            "rule_no": rule.get("规则编号", "") if rule else "",
        })
        return merged

    def merge_from_exports(self, rule_id: int) -> list[dict[str, Any]]:
        """从导出产物直接读取两个数据源并进行归并。

        跨模块增强：被 export_task 完成后调用，实现导出->归并->审计链路。
        """
        from app.services.data_manager import list_exports
        exports = list_exports()
        exports.sort(key=lambda x: x.get("_created_at", ""), reverse=True)
        source_a = exports[0] if len(exports) > 0 else {}
        source_b = exports[1] if len(exports) > 1 else {}

        rows_a = []
        rows_b = []
        cols_a = source_a.get("columns", [])
        cols_b = source_b.get("columns", [])
        for row_vals in source_a.get("rows", []):
            row = {col: val for col, val in zip(cols_a, row_vals)} if cols_a else {}
            rows_a.append(row)
        for row_vals in source_b.get("rows", []):
            row = {col: val for col, val in zip(cols_b, row_vals)} if cols_b else {}
            rows_b.append(row)

        return self.merge_and_audit(rule_id, rows_a, rows_b)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return self.run_action_with_remark(entry_id, action, None)

    def run_action_with_remark(self, entry_id: int, action: str,
                               remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action in self.BATCH_ACTIONS:
            return self._run_batch_action(entry, action, remark)

        if action == "标记生效":
            from app.services.data_manager import read_api_snapshot
            registry = read_api_snapshot()
            primary_api = entry.get("主API", "")
            secondary_api = entry.get("从API", "")
            api_names = {v.get("api_name") for v in (registry.values() if isinstance(registry, dict) else []) if isinstance(v, dict)}
            if primary_api not in api_names and secondary_api not in api_names:
                return None, "至少一个源API尚未注册"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def _run_batch_action(self, entry: dict[str, Any], action: str,
                          remark: str | None) -> tuple[dict[str, Any] | None, str]:
        from app.services.delivery_ledger import get_ledger

        ledger = get_ledger()
        action = self.BATCH_ALIASES.get(action, action)
        if action == "发布正式批次":
            batch, message = ledger.publish_batch(entry)
            if batch is not None:
                self._write_batch_audit(entry, "merge_batch_published", batch, message)
            return (entry, message) if batch is not None else (None, message)

        if action == "撤回正式批次":
            batch_no = str(entry.get("当前批次") or "")
            receipt, message = ledger.withdraw_batch(entry, remark or "")
            if receipt is not None:
                self._write_batch_audit(entry, "merge_batch_withdrawn", receipt, message)
                return entry, message
            return None, message

        if action == "登记更正来源":
            rule, message = ledger.mark_source_ready(entry, remark or "")
            return (rule, message) if rule is not None else (None, message)

        if action == "发布更正批次":
            batch, message = ledger.publish_corrected_batch(entry)
            if batch is not None:
                self._write_batch_audit(entry, "merge_batch_corrected", batch, message)
                return entry, message
            return None, message

        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def _write_batch_audit(self, rule: dict[str, Any], event: str,
                           payload: dict[str, Any], message: str) -> None:
        from app.services.data_manager import write_audit, DATA_ROOT
        audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": rule.get("主API", ""),
            "caller": "data_merge",
            "event": event,
            "rule_no": rule.get("规则编号", ""),
            "batch_no": payload.get("batch_no") or payload.get("receipt_no", ""),
            "receipt_no": payload.get("receipt_no", ""),
            "message": message,
        })

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        merge_list = []
        for row in rows:
            merge_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "primary_api": row.get("主API"),
                "secondary_api": row.get("从API"),
                "merge_key": row.get("归并键"),
                "conflict_policy": row.get("冲突策略"),
                "dedup_policy": row.get("去重策略"),
                "sort": row.get("结果排序"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
                "current_batch": row.get("当前批次", ""),
                "original_batch": row.get("原归并批次", ""),
                "batch_status": row.get("批次状态", ""),
                "receipt_no": row.get("撤回回执", ""),
                "correction_source_state": row.get("更正来源状态", ""),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(merge_list),
            "merges": merge_list,
            "active_count": sum(1 for r in merge_list if r.get("status") == "已生效"),
            "confirmed_batches": sum(
                1 for r in merge_list if r.get("batch_status") == "已确认"),
            "withdrawn_batches": sum(
                1 for r in merge_list if r.get("batch_status") == "已撤回"),
        }