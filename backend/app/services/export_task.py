"""导出任务领域服务：管理数据导出任务的排队、执行和过期清理。

跨模块增强：
- complete 触发 call_audit 审计记录（业务能力增强）
- 多源归并 detect：export 完成后自动通知 data_merge

跨模块数据流：
- 消费 api_registry 快照 -> 核验导出API合法性
- 消费 data_filter 快照 -> 导出数据应用过滤规则
- 消费 field_mask 快照 -> 导出数据应用脱敏规则
- 消费 result_format 快照 -> 导出数据按模板格式化
- 写入 data/exports/ 共享目录 -> call_audit 消费导出审计
- 被 data_merge 消费 -> 多源导出归并
- 消费 data_merge 撤回回执 -> 撤回错误稿、按更正批次重办有效交付
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExportTaskService(BaseModuleService):
    MODULE = "export_task"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["任务编号", "关联API", "查询参数", "导出格式", "数据行数", "文件大小", "过期时间", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联API", "查询参数"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "已失败"]
    ACTION_RULES = {
        "发起导出": "导出中",
        "确认完成": "已完成",
        "标记失败": "已失败",
        "重办导出": "已完成",
        "重新导出": "已完成",
        "重办": "已完成",
    }
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记导出任务时允许直接绑定归并批次，便于等待出文件的登记挂到稿件上。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        from app.services.delivery_ledger import get_ledger, REGISTERED
        batch_no = str(values.get("归并批次") or "").strip()
        if batch_no:
            get_ledger()
            entry["归并批次"] = batch_no
            entry["原归并批次"] = str(values.get("原归并批次") or "")
            entry["登记状态"] = str(values.get("登记状态") or REGISTERED)
            entry["交付编号"] = str(values.get("交付编号") or "")
            entry["交付文件"] = ""
            entry["实际数据源"] = ""
            entry["撤回回执"] = ""
        return entry, missing

    def execute_export(self, export_id: int) -> dict[str, Any]:
        """执行导出任务：查询数据 → 过滤 → 脱敏 → 格式化 → 写入文件。"""
        entry = store.find(self.MODULE, export_id)
        if entry is None:
            return {"ok": False, "message": "任务不存在"}

        api_name = entry.get("关联API", "")

        # 守住归并来源：列必须整体来自同一份已确认的归并批次。
        from app.services.delivery_ledger import get_ledger
        ledger = get_ledger()
        batch, reason = ledger.can_generate(entry)
        if batch is None and reason:
            return {"ok": False, "message": reason}

        if batch is not None:
            # 绑定正式批次时，行列整体取自该批次冻结的归并结果。
            source_rows = ledger.build_batch_rows(batch)
        else:
            # 未绑定批次的历史任务沿用模拟查询结果。
            source_rows = self._mock_query(api_name)

        # 应用 data_filter 过滤
        from app.services import service_for
        filter_svc = service_for("data_filter")
        if filter_svc and hasattr(filter_svc, "apply_filters"):
            source_rows = filter_svc.apply_filters(api_name, source_rows)

        # 应用 field_mask 脱敏
        mask_svc = service_for("field_mask")
        if mask_svc and hasattr(mask_svc, "mask_fields"):
            source_rows = mask_svc.mask_fields(api_name, source_rows)

        # 写入导出文件
        from app.services.data_manager import write_export
        if batch is not None:
            columns = list(batch.get("columns", []))
        else:
            columns = list(source_rows[0].keys()) if source_rows else []
        export_data = {
            "export_id": export_id,
            "task_no": entry.get("任务编号"),
            "api_name": api_name,
            "query_params": entry.get("查询参数"),
            "export_format": entry.get("导出格式", "json"),
            "rows_count": len(source_rows),
            "columns": columns,
            "rows": [[str(r.get(c, "")) for c in columns] for r in source_rows],
            "status": "已完成",
        }
        if batch is not None:
            export_data["batch_no"] = batch["batch_no"]
        write_export(export_id, export_data)

        # 写审计记录
        from app.services.data_manager import write_audit
        audit_id = self._next_audit_id()
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": api_name,
            "caller": "export_task",
            "event": "export_completed",
            "export_id": export_id,
            "rows_count": len(source_rows),
        })

        entry["status"] = "已完成"
        entry["数据行数"] = str(len(source_rows))

        delivery = None
        if batch is not None:
            # 交付登记：一份文件对应一份交付身份，列来源固定指向该批次。
            delivery = ledger.new_delivery(entry, batch, len(source_rows))
            entry["交付编号"] = delivery["delivery_id"]
            entry["交付文件"] = delivery["file"]
            entry["登记状态"] = "已交付"
            entry["实际数据源"] = (
                f"{batch.get('primary_api')} + {batch.get('secondary_api')}"
            )
        return {
            "ok": True,
            "export_id": export_id,
            "rows_count": len(source_rows),
            "delivery_id": (delivery or {}).get("delivery_id", ""),
        }

    def reissue_export(self, export_id: int) -> dict[str, Any]:
        """按撤回回执找到更正批次，为受限登记重办一份有效交付。"""
        entry = store.find(self.MODULE, export_id)
        if entry is None:
            return {"ok": False, "message": "任务不存在"}

        from app.services.delivery_ledger import get_ledger
        ledger = get_ledger()
        batch, receipt_no = ledger.can_reissue(entry)
        if batch is None:
            return {"ok": False, "message": receipt_no}

        columns = list(batch.get("columns", []))
        source_rows = ledger.build_batch_rows(batch)
        from app.services.data_manager import write_export
        export_data = {
            "export_id": export_id,
            "task_no": entry.get("任务编号"),
            "api_name": entry.get("关联API"),
            "query_params": entry.get("查询参数"),
            "export_format": entry.get("导出格式", "csv"),
            "batch_no": batch["batch_no"],
            "original_batch_no": batch.get("corrected_from", ""),
            "receipt_no": receipt_no,
            "rows_count": len(source_rows),
            "columns": columns,
            "rows": [[str(row.get(c, "")) for c in columns] for row in source_rows],
            "status": "已完成",
        }
        write_export(export_id, export_data)

        # 已经发出过的登记要保留旧交付做对账，重办时把旧交付标记为被替代。
        previous = entry.get("交付编号")
        delivery = ledger.new_delivery(entry, batch, len(source_rows))
        if previous:
            old = ledger.deliveries.get(str(previous))
            if old is not None:
                old["superseded_by"] = delivery["delivery_id"]
        entry["登记状态"] = "已重办"
        entry["状态流转"] = (
            f"已撤回 -> 待重办 -> 已重办（回执 {receipt_no}，"
            f"原归并 {batch.get('corrected_from')}，更正批次 {batch['batch_no']}）"
        )
        entry["status"] = "已完成"
        entry["pending"] = False

        from app.services.data_manager import write_audit
        audit_id = self._next_audit_id()
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": entry.get("关联API"),
            "caller": "export_task",
            "event": "export_reissued",
            "export_id": export_id,
            "receipt_no": receipt_no,
            "delivery_id": delivery["delivery_id"],
            "batch_no": batch["batch_no"],
            "original_batch_no": batch.get("corrected_from", ""),
            "rows_count": len(source_rows),
        })
        return {
            "ok": True,
            "export_id": export_id,
            "rows_count": len(source_rows),
            "delivery_id": delivery["delivery_id"],
            "batch_no": batch["batch_no"],
        }

    def _mock_query(self, api_name: str) -> list[dict[str, Any]]:
        """模拟从数据源查询结果。"""
        return [
            {"id": 1, "name": "张三", "phone": "13800138000", "email": "zhangsan@example.com", "amount": 1250.50},
            {"id": 2, "name": "李四", "phone": "13900139000", "email": "lisi@example.com", "amount": 2340.00},
            {"id": 3, "name": "王五", "phone": "13700137000", "email": "wangwu@example.com", "amount": 890.75},
        ]

    def _next_audit_id(self) -> int:
        from app.services.data_manager import DATA_ROOT
        existing = list(DATA_ROOT.glob("audit/[0-9]*.json"))
        return max((int(f.stem) for f in existing), default=0) + 1

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发起导出":
            result = self.execute_export(entry_id)
            if not result.get("ok"):
                return None, result.get("message", "导出失败")
            entry["status"] = "已完成"
            entry["pending"] = False
            from app.services.data_manager import write_module_snapshot
            write_module_snapshot(self.MODULE, self.domain_snapshot())
            message = f"导出任务已完成，共{result.get('rows_count', 0)}行"
            if result.get("delivery_id"):
                message += f"，交付 {result['delivery_id']}"
            return entry, message

        if action in ("重办导出", "重新导出", "重办"):
            result = self.reissue_export(entry_id)
            if not result.get("ok"):
                return None, result.get("message", "重办失败")
            from app.services.data_manager import write_module_snapshot
            write_module_snapshot(self.MODULE, self.domain_snapshot())
            return entry, (
                f"已按回执重办，交付 {result['delivery_id']}，"
                f"批次 {result['batch_no']}，共{result.get('rows_count', 0)}行"
            )

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        export_list = []
        for row in rows:
            export_list.append({
                "id": row.get("id"),
                "task_no": row.get("任务编号"),
                "api": row.get("关联API"),
                "query_params": row.get("查询参数"),
                "format": row.get("导出格式"),
                "rows": row.get("数据行数"),
                "file_size": row.get("文件大小"),
                "expire_at": row.get("过期时间"),
                "task_status": row.get("任务状态"),
                "status": row.get("status"),
                "batch_no": row.get("归并批次", ""),
                "original_batch_no": row.get("原归并批次", ""),
                "delivery_no": row.get("交付编号", ""),
                "delivery_file": row.get("交付文件", ""),
                "registration_state": row.get("登记状态", ""),
                "receipt_no": row.get("撤回回执", ""),
                "actual_source": row.get("实际数据源", ""),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(export_list),
            "exports": export_list,
            "completed_count": sum(1 for r in export_list if r.get("status") == "已完成"),
            "failed_count": sum(1 for r in export_list if r.get("status") == "已失败"),
            "withdrawn_count": sum(
                1 for r in export_list if r.get("registration_state") == "已撤回"),
            "pending_redo_count": sum(
                1 for r in export_list if r.get("registration_state") == "待重办"),
            "redone_count": sum(
                1 for r in export_list if r.get("registration_state") == "已重办"),
        }