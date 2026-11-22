"""归并交付台账：归并批次、撤回回执与交付文件的唯一事实来源。

正式归并批次一旦发布，就冻结当时的来源关系。导出任务只能在“已确认”的
批次上生成交付；批次被撤回后：

- 已发出的旧交付不删除，改为“已撤回”并保留原批次引用，供事后对账；
- 尚未生成文件的登记进入“待重办”；
- 更正来源补齐并发布更正批次后，登记才允许“重办导出”，写出新的交付身份。

台账同时把状态落到 data/merge_ledger/ 和 data/receipts/ 下，路由层只做转发。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.data_manager import write_merge_ledger, write_receipt
from app.store import store

TZ = timezone(timedelta(hours=8))

# 登记状态流转：已登记 -> 已交付 -> 已撤回 -> 已重办；未出文件时 已登记 -> 待重办 -> 已重办
REGISTERED = "已登记"
DELIVERED = "已交付"
PENDING_REDO = "待重办"
WITHDRAWN = "已撤回"
REDONE = "已重办"

CONFIRMED = "已确认"
BATCH_WITHDRAWN = "已撤回"
BATCH_CORRECTED = "已更正"

_DEMO_BATCH = "MRG-DATA-0003-01"


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


class DeliveryLedger:
    """内存台账 + 共享目录快照；单进程内保证读写一致。"""

    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self._demo_loaded = False

    # ---------- 基础读取 ----------

    def reset(self, *, seed_demo: bool = False) -> None:
        self.batches.clear()
        self.receipts.clear()
        self.deliveries.clear()
        self._demo_loaded = False
        if seed_demo:
            self.ensure_demo()

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        return self.batches.get(batch_no)

    def batches_for_rule(self, rule_id: int) -> list[dict[str, Any]]:
        rows = [b for b in self.batches.values() if int(b.get("rule_id", 0)) == rule_id]
        return sorted(rows, key=lambda b: str(b.get("batch_no", "")))

    def current_batch(self, rule_id: int) -> dict[str, Any] | None:
        confirmed = [b for b in self.batches_for_rule(rule_id) if b.get("status") == CONFIRMED]
        return confirmed[-1] if confirmed else None

    def latest_corrected_batch(self, original_batch: str) -> dict[str, Any] | None:
        candidates = [
            b for b in self.batches.values()
            if b.get("corrected_from") == original_batch and b.get("status") == CONFIRMED
        ]
        candidates.sort(key=lambda b: str(b.get("batch_no", "")))
        return candidates[-1] if candidates else None

    def deliveries_for_registration(self, registration_id: int) -> list[dict[str, Any]]:
        rows = [
            d for d in self.deliveries.values()
            if int(d.get("registration_id", 0)) == int(registration_id)
        ]
        return sorted(rows, key=lambda d: (str(d.get("created_at", "")), str(d.get("delivery_id", ""))))

    def valid_delivery(self, registration_id: int) -> dict[str, Any] | None:
        for delivery in self.deliveries_for_registration(registration_id):
            if delivery.get("status") == "有效":
                return delivery
        return None

    def deliveries_for_batch(self, batch_no: str) -> list[dict[str, Any]]:
        rows = [d for d in self.deliveries.values() if d.get("batch_no") == batch_no]
        return sorted(rows, key=lambda d: str(d.get("delivery_id", "")))

    def receipt_for_batch(self, batch_no: str) -> dict[str, Any] | None:
        for receipt in self.receipts.values():
            if receipt.get("batch_no") == batch_no:
                return receipt
        return None

    # ---------- 演示场景 ----------

    def ensure_demo(self) -> None:
        """把种子里的正式批次和已发交付装进台账，方便直接演示撤回流程。"""
        if self._demo_loaded:
            return
        self._demo_loaded = True
        rule = next((r for r in store.rows("data_merge")
                     if r.get("当前批次") == _DEMO_BATCH), None)
        if rule is None or _DEMO_BATCH in self.batches:
            return

        batch = self._build_batch(rule, batch_no=_DEMO_BATCH, corrected_from="")
        self.batches[_DEMO_BATCH] = batch

        for row in store.rows("export_task"):
            if row.get("归并批次") != _DEMO_BATCH or row.get("登记状态") != DELIVERED:
                continue
            delivery_id = str(row.get("交付编号") or self._next_delivery_id(row))
            delivery = self._create_delivery(
                row=row, batch=batch, delivery_id=delivery_id)
            delivery["status"] = "有效"
            self.deliveries[delivery_id] = delivery
            # 种子里的行数/文件大小只是占位，启动时对齐到真实交付文件。
            row["数据行数"] = str(delivery["rows_count"])
            row["文件大小"] = str(delivery["file_size"])
            row["交付文件"] = delivery["file"]
        self._persist()

    # ---------- 批次发布 / 撤回 / 更正 ----------

    def publish_batch(self, rule: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """为“已生效”的归并规则发布正式批次，冻结来源关系。"""
        if rule.get("status") != "已生效":
            return None, "只有已生效的归并规则才能发布正式批次"

        existing = self.current_batch(int(rule.get("id", 0)))
        if existing is not None:
            return None, f"规则已有生效批次 {existing['batch_no']}，如需重新发布请先撤回"
        history = self.batches_for_rule(int(rule.get("id", 0)))
        if history and history[-1].get("status") == BATCH_WITHDRAWN:
            return None, (
                f"最近批次 {history[-1]['batch_no']} 已撤回，"
                "请补齐更正来源后发布更正批次，保留与原批次的关联"
            )

        batch = self._build_batch(rule)
        self.batches[batch["batch_no"]] = batch
        rule["当前批次"] = batch["batch_no"]
        rule["批次状态"] = CONFIRMED
        rule.pop("更正来源状态", None)
        self._write_rule_snapshot(rule)
        self._persist()
        return batch, (
            f"归并批次 {batch['batch_no']} 已发布，"
            f"来源 {batch['primary_api']} + {batch['secondary_api']}"
        )

    def withdraw_batch(self, rule: dict[str, Any], reason: str,
                       operator: str = "数据工程值班") -> tuple[dict[str, Any] | None, str]:
        """撤回正式批次并签发回执，级联更新受影响的导出登记与旧交付。"""
        batch_no = str(rule.get("当前批次") or "")
        batch = self.batches.get(batch_no)
        if batch is None:
            return None, "当前规则没有可撤回的正式批次"
        if batch.get("status") != CONFIRMED:
            return None, f"批次 {batch_no} 当前状态为 {batch.get('status')}，不能再次撤回"

        reason = reason.strip() or "归并版本引用了错误来源"
        affected: list[dict[str, Any]] = []
        old_deliveries: list[dict[str, Any]] = []

        for row in store.rows("export_task"):
            if row.get("归并批次") != batch_no:
                continue
            before = str(row.get("登记状态") or REGISTERED)
            delivery = self.valid_delivery(int(row.get("id", 0)))
            if delivery is not None:
                delivery["status"] = "已撤回"
                delivery["withdrawn_at"] = _now_iso()
                after = WITHDRAWN
                old_deliveries.append({
                    "delivery_id": delivery.get("delivery_id"),
                    "batch_no": delivery.get("batch_no"),
                    "file": delivery.get("file"),
                    "columns": delivery.get("columns", []),
                    "status": "已撤回",
                })
                row["已撤回交付"] = delivery.get("delivery_id")
            else:
                after = PENDING_REDO

            row["登记状态"] = after
            row["已撤回批次"] = batch_no
            row["撤回回执"] = ""  # 回执号生成后统一回填
            affected.append({
                "registration_id": row.get("id"),
                "task_no": row.get("任务编号"),
                "api": row.get("关联API"),
                "state_before": before,
                "state_after": after,
                "batch_no": batch_no,
                "delivery_id": (delivery or {}).get("delivery_id", row.get("交付编号") or ""),
                "file": (delivery or {}).get("file", row.get("交付文件") or ""),
                "next_action": (
                    "补齐更正来源并发布更正批次后重办导出"
                    if after in (WITHDRAWN, PENDING_REDO) else "无需处理"
                ),
            })

        receipt_no = self._next_receipt_no()
        for item in affected:
            row = store.find("export_task", int(item["registration_id"]))
            if row is not None:
                row["撤回回执"] = receipt_no

        receipt = {
            "receipt_no": receipt_no,
            "event": "merge_batch_withdrawn",
            "batch_no": batch_no,
            "merge_rule_id": batch.get("rule_id"),
            "merge_rule_no": batch.get("rule_no"),
            "merge_version": batch.get("version"),
            "reason": reason,
            "operator": operator,
            "withdrawn_at": _now_iso(),
            "sources": batch.get("sources", {}),
            "affected": affected,
            "old_deliveries": old_deliveries,
            "next_step": "待更正来源补齐后发布更正批次，再按回执重办受影响的导出登记",
        }

        batch["status"] = BATCH_WITHDRAWN
        batch["withdrawn_at"] = receipt["withdrawn_at"]
        batch["receipt_no"] = receipt_no
        self.receipts[receipt_no] = receipt

        rule["批次状态"] = BATCH_WITHDRAWN
        rule["撤回回执"] = receipt_no
        rule["更正来源状态"] = "待补齐"
        rule["撤回原因"] = reason
        self._write_rule_snapshot(rule)
        write_receipt(receipt_no, receipt)
        self._persist()
        return receipt, (
            f"批次 {batch_no} 已撤回，回执 {receipt_no}，"
            f"影响 {len(affected)} 笔登记（旧交付保留对账）"
        )

    def mark_source_ready(self, rule: dict[str, Any], note: str = "") -> tuple[dict[str, Any] | None, str]:
        """登记更正来源已补齐；只有补齐后才允许发布更正批次。"""
        batch_no = str(rule.get("当前批次") or "")
        batch = self.batches.get(batch_no)
        if batch is None or batch.get("status") != BATCH_WITHDRAWN:
            return None, "当前没有处于撤回状态的批次，无需补齐更正来源"
        rule["更正来源状态"] = "已补齐"
        rule["更正来源说明"] = note.strip() or f"{batch.get('secondary_api')} 已补回并完成核验"
        self._write_rule_snapshot(rule)
        self._persist()
        return rule, f"更正来源已补齐：{rule['更正来源说明']}"

    def publish_corrected_batch(self, rule: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """在更正来源补齐后发布更正批次，并保留与原批次的关联。"""
        original_no = str(rule.get("当前批次") or "")
        original = self.batches.get(original_no)
        if original is None or original.get("status") != BATCH_WITHDRAWN:
            return None, "当前没有处于撤回状态的批次，不能发布更正批次"
        if rule.get("更正来源状态") != "已补齐":
            return None, "更正来源尚未补齐，不能发布更正批次"

        batch = self._build_batch(rule, corrected_from=original_no)
        self.batches[batch["batch_no"]] = batch
        rule["原归并批次"] = original_no
        rule["当前批次"] = batch["batch_no"]
        rule["批次状态"] = BATCH_CORRECTED
        self._write_rule_snapshot(rule)
        self._persist()
        return batch, (
            f"更正批次 {batch['batch_no']} 已发布，关联原归并 {original_no}，"
            f"实际来源 {batch['primary_api']} + {batch['secondary_api']}"
        )

    # ---------- 交付生成 / 重办 ----------

    def can_generate(self, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """校验登记是否可以把列落到某一批次上生成新交付。"""
        batch_no = str(row.get("归并批次") or "")
        if not batch_no:
            return None, ""
        batch = self.batches.get(batch_no)
        if batch is None:
            return None, f"归并批次 {batch_no} 未登记，不能生成交付"
        receipt_no = batch.get("receipt_no")
        if batch.get("status") == BATCH_WITHDRAWN:
            return None, (
                f"归并批次 {batch_no} 已撤回（回执 {receipt_no}），错误稿不能再出新交付；"
                "请按回执重办登记"
            )
        if batch.get("status") != CONFIRMED:
            return None, f"归并批次 {batch_no} 状态为 {batch.get('status')}，不能生成交付"
        if row.get("登记状态") in (WITHDRAWN, PENDING_REDO):
            return None, (
                f"登记当前状态为 {row.get('登记状态')}，请通过“重办导出”恢复有效交付"
            )
        if self.valid_delivery(int(row.get("id", 0))) is not None:
            return None, "该登记已有有效交付，不能重复生成"
        return batch, ""

    def can_reissue(self, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """依据撤回回执找到更正批次，并校验重办条件。"""
        state = str(row.get("登记状态") or "")
        if state not in (WITHDRAWN, PENDING_REDO):
            return None, f"登记当前状态为 {state or REGISTERED}，不需要重办"
        original_no = str(row.get("已撤回批次") or row.get("归并批次") or "")
        original = self.batches.get(original_no)
        receipt_no = str(row.get("撤回回执") or (original or {}).get("receipt_no") or "")
        if not receipt_no:
            return None, "登记没有关联撤回回执，无法重办"
        corrected = self.latest_corrected_batch(original_no)
        rule = store.find("data_merge", int((original or {}).get("rule_id", 0)))
        if corrected is None and rule is not None and rule.get("更正来源状态") == "已补齐":
            # 来源已补齐但还没显式发布更正批次时，重办动作按回执补发布一次。
            corrected, message = self.publish_corrected_batch(rule)
            if corrected is None:
                return None, message
        if corrected is None:
            hint = (rule or {}).get("更正来源状态", "待补齐")
            return None, (
                f"回执 {receipt_no} 对应的更正来源{hint}，"
                "请先补齐来源并发布更正批次后再重办"
            )
        return corrected, receipt_no

    def new_delivery(self, row: dict[str, Any], batch: dict[str, Any],
                     rows_count: int = 0) -> dict[str, Any]:
        """为登记写出新的交付身份，关联原归并、实际数据源与新文件。"""
        delivery_id = self._next_delivery_id(row)
        delivery = self._create_delivery(row=row, batch=batch,
                                         delivery_id=delivery_id)
        self.deliveries[delivery_id] = delivery

        if not row.get("原归并批次"):
            row["原归并批次"] = str(row.get("已撤回批次") or row.get("归并批次") or "")
        row["归并批次"] = batch["batch_no"]
        row["交付编号"] = delivery_id
        row["实际数据源"] = f"{batch['primary_api']} + {batch['secondary_api']}"
        row["交付文件"] = delivery["file"]
        row["登记状态"] = REDONE
        row.pop("已撤回批次", None)
        self._persist()
        return delivery

    def build_batch_rows(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """按已冻结批次中的来源与策略构造数据，列只来自这一份归并。"""
        primary = batch.get("source_rows", {}).get("primary", [])
        secondary = batch.get("source_rows", {}).get("secondary", [])
        merge_key = batch.get("merge_key", "id") or "id"
        conflict = batch.get("conflict_policy", "主源优先")
        dedup = batch.get("dedup_policy", "全保留")
        sort_field = batch.get("sort", "")

        merged: dict[str, dict[str, Any]] = {}
        for row in primary:
            key = str(row.get(merge_key, ""))
            merged[f"p_{key}"] = {"_source": "primary", **row}
        for row in secondary:
            key = str(row.get(merge_key, ""))
            pk = f"p_{key}"
            if pk in merged:
                if conflict == "从源覆盖":
                    merged[pk] = {"_source": "secondary", **row}
                elif conflict == "合并字段":
                    merged[pk] = {**merged[pk], **row, "_source": "merged"}
            else:
                merged[f"s_{key}"] = {"_source": "secondary", **row}

        rows = list(merged.values())
        if dedup == "按键去重":
            seen: set[str] = set()
            unique = []
            for row in rows:
                key = str(row.get(merge_key, ""))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            rows = unique
        if sort_field:
            rows = sorted(rows, key=lambda r: str(r.get(sort_field, "")))
        return rows

    def describe_state(self, row: dict[str, Any]) -> dict[str, Any]:
        delivs = self.deliveries_for_registration(int(row.get("id", 0)))
        valid = next((d for d in delivs if d.get("status") == "有效"), None)
        return {
            "registration_id": row.get("id"),
            "task_no": row.get("任务编号"),
            "state": row.get("登记状态", REGISTERED),
            "batch_no": row.get("归并批次", ""),
            "original_batch_no": row.get("原归并批次", ""),
            "delivery_no": row.get("交付编号", ""),
            "delivery_file": (valid or {}).get("file", "") or row.get("交付文件", ""),
            "receipt_no": row.get("撤回回执", ""),
            "deliveries": [
                {
                    "delivery_id": d.get("delivery_id"),
                    "batch_no": d.get("batch_no"),
                    "status": d.get("status"),
                    "file": d.get("file"),
                    "rows_count": d.get("rows_count"),
                    "created_at": d.get("created_at"),
                }
                for d in delivs
            ],
        }

    # ---------- 内部工具 ----------

    def _build_batch(self, rule: dict[str, Any], *, batch_no: str = "",
                     corrected_from: str = "") -> dict[str, Any]:
        rule_id = int(rule.get("id", 0))
        rule_no = str(rule.get("规则编号") or f"RULE-{rule_id:04d}")
        sequence = len(self.batches_for_rule(rule_id)) + 1
        if not batch_no:
            batch_no = f"MRG-{rule_no}-{sequence:02d}"

        primary_api = str(rule.get("主API") or "")
        secondary_api = str(rule.get("从API") or "")
        primary_rows, secondary_rows = self._mock_sources(primary_api, secondary_api)
        columns = self._columns(primary_rows, secondary_rows)

        return {
            "batch_no": batch_no,
            "rule_id": rule_id,
            "rule_no": rule_no,
            "version": rule.get("规则版本", ""),
            "status": CONFIRMED,
            "primary_api": primary_api,
            "secondary_api": secondary_api,
            "merge_key": rule.get("归并键", "id"),
            "conflict_policy": rule.get("冲突策略", "主源优先"),
            "dedup_policy": rule.get("去重策略", "全保留"),
            "sort": rule.get("结果排序", ""),
            "columns": columns,
            "sources": {
                "primary": {"api": primary_api, "rows": len(primary_rows)},
                "secondary": {"api": secondary_api, "rows": len(secondary_rows)},
            },
            "source_rows": {"primary": primary_rows, "secondary": secondary_rows},
            "published_at": _now_iso(),
            "published_by": "数据工程值班",
            "corrected_from": corrected_from,
            "receipt_no": "",
            "withdrawn_at": "",
        }

    def _create_delivery(self, *, row: dict[str, Any], batch: dict[str, Any],
                         delivery_id: str) -> dict[str, Any]:
        from app.services.data_manager import write_delivery_file

        columns = list(batch.get("columns", []))
        source_rows = self.build_batch_rows(batch)
        payload_rows = [
            [str(source_row.get(column, "")) for column in columns]
            for source_row in source_rows
        ]
        payload = {
            "delivery_id": delivery_id,
            "batch_no": batch["batch_no"],
            "merge_rule_no": batch.get("rule_no"),
            "task_no": row.get("任务编号"),
            "api_name": row.get("关联API"),
            "query_params": row.get("查询参数"),
            "export_format": row.get("导出格式", "csv"),
            "columns": columns,
            "rows": payload_rows,
            "status": "有效",
        }
        files = write_delivery_file(delivery_id, payload)
        return {
            "delivery_id": delivery_id,
            "registration_id": int(row.get("id", 0)),
            "task_no": row.get("任务编号"),
            "api": row.get("关联API"),
            "batch_no": batch["batch_no"],
            "merge_rule_no": batch.get("rule_no"),
            "source_relation": {
                "primary_api": batch.get("primary_api"),
                "secondary_api": batch.get("secondary_api"),
            },
            "column_source": {"batch_no": batch["batch_no"], "columns": columns},
            "columns": columns,
            "rows_count": len(payload_rows),
            "file": files["csv"],
            "file_json": files["json"],
            "file_size": files["size"],
            "status": "有效",
            "created_at": _now_iso(),
            "withdrawn_at": "",
            "superseded_by": "",
        }

    def _next_delivery_id(self, row: dict[str, Any]) -> str:
        task_no = str(row.get("任务编号") or f"EXPO-{int(row.get('id', 0)):04d}")
        existing = self.deliveries_for_registration(int(row.get("id", 0)))
        sequence = len(existing) + 1
        delivery_id = f"DLV-{task_no}-{sequence:02d}"
        while delivery_id in self.deliveries:
            sequence += 1
            delivery_id = f"DLV-{task_no}-{sequence:02d}"
        return delivery_id

    def _next_receipt_no(self) -> str:
        sequence = len(self.receipts) + 1
        receipt_no = f"RCPT-{sequence:04d}"
        while receipt_no in self.receipts:
            sequence += 1
            receipt_no = f"RCPT-{sequence:04d}"
        return receipt_no

    def _columns(self, primary: list[dict[str, Any]],
                 secondary: list[dict[str, Any]]) -> list[str]:
        columns: list[str] = []
        for row in [*primary, *secondary]:
            for key in row:
                if key != "_source" and key not in columns:
                    columns.append(key)
        return columns

    def _mock_sources(self, primary_api: str,
                      secondary_api: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """生成稳定可见的来源示例数据，保证同一来源每次发布列结构一致。"""
        primary = [
            {"member_id": "M001", "name": "张三", "phone": "13800138000",
             "email": "zhangsan@example.com", "amount": "1250.50"},
            {"member_id": "M002", "name": "李四", "phone": "13900139000",
             "email": "lisi@example.com", "amount": "2340.00"},
            {"member_id": "M003", "name": "王五", "phone": "13700137000",
             "email": "wangwu@example.com", "amount": "890.75"},
        ]
        secondary = [
            {"member_id": "M001", "level": "金卡", "region": "华东"},
            {"member_id": "M002", "level": "银卡", "region": "华南"},
            {"member_id": "M004", "level": "铂金", "region": "华北"},
        ]
        return primary, secondary

    def _write_rule_snapshot(self, rule: dict[str, Any]) -> None:
        from app.services.data_manager import write_module_snapshot
        service = _merge_service()
        if service is not None:
            write_module_snapshot("data_merge", service.domain_snapshot())

    def _persist(self) -> None:
        write_merge_ledger({
            "batches": self.batches,
            "receipts": self.receipts,
            "deliveries": self.deliveries,
        })

    def snapshot(self) -> dict[str, Any]:
        return {
            "batches": sorted(
                ({k: v for k, v in batch.items() if k != "source_rows"}
                 for batch in self.batches.values()),
                key=lambda b: str(b.get("batch_no", "")),
            ),
            "receipts": sorted(self.receipts.values(), key=lambda r: str(r.get("receipt_no", ""))),
            "deliveries": sorted(self.deliveries.values(), key=lambda d: str(d.get("delivery_id", ""))),
        }


def _merge_service():
    from app.services import service_for
    return service_for("data_merge")


ledger = DeliveryLedger()


def get_ledger() -> DeliveryLedger:
    ledger.ensure_demo()
    return ledger


__all__ = [
    "DeliveryLedger",
    "get_ledger",
    "ledger",
    "REGISTERED",
    "DELIVERED",
    "PENDING_REDO",
    "WITHDRAWN",
    "REDONE",
    "CONFIRMED",
    "BATCH_WITHDRAWN",
    "BATCH_CORRECTED",
]
