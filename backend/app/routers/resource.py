"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/data_merge/batches", response_model=dict)
def list_merge_batches(rule_id: int | None = Query(default=None),
                       status: str | None = Query(default=None)) -> dict:
    """归并批次台账：查看正式批次、撤回状态和更正关联。"""
    from app.services.delivery_ledger import get_ledger
    batches = get_ledger().snapshot()["batches"]
    if rule_id is not None:
        batches = [b for b in batches if int(b.get("rule_id", 0)) == rule_id]
    if status:
        batches = [b for b in batches if b.get("status") == status]
    return {"items": batches, "total": len(batches)}


@router.get("/data_merge/receipts", response_model=dict)
def list_withdraw_receipts(batch_no: str | None = Query(default=None)) -> dict:
    """归并数据撤回回执列表，等待出文件的导出任务据此识别关联稿件。"""
    from app.services.delivery_ledger import get_ledger
    receipts = get_ledger().snapshot()["receipts"]
    if batch_no:
        receipts = [r for r in receipts if r.get("batch_no") == batch_no]
    return {"items": receipts, "total": len(receipts)}


@router.get("/data_merge/receipts/{receipt_no}", response_model=dict)
def get_withdraw_receipt(receipt_no: str) -> dict:
    from app.services.delivery_ledger import get_ledger
    receipt = get_ledger().receipts.get(receipt_no)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"撤回回执 {receipt_no} 不存在")
    return receipt


@router.get("/export_task/registrations", response_model=dict)
def list_export_registrations(receipt_no: str | None = Query(default=None),
                              state: str | None = Query(default=None)) -> dict:
    """导出登记的交付状态：列出等待出文件、已撤回和已重办的登记。"""
    from app.services.delivery_ledger import get_ledger
    from app.store import store as data_store
    ledger = get_ledger()
    items = [
        ledger.describe_state(row)
        for row in data_store.rows("export_task")
        if row.get("归并批次") or row.get("原归并批次")
    ]
    if receipt_no:
        items = [item for item in items if item.get("receipt_no") == receipt_no]
    if state:
        items = [item for item in items if item.get("state") == state]
    return {"items": items, "total": len(items)}


@router.get("/export_task/{entry_id}/deliveries", response_model=dict)
def list_entry_deliveries(entry_id: int) -> dict:
    """单个登记的全部交付身份，含已撤回的旧交付和重办后的新交付。"""
    from app.services.delivery_ledger import get_ledger
    from app.store import store as data_store
    entry = data_store.find("export_task", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {entry_id} 不存在或已归档")
    ledger = get_ledger()
    return {
        "registration": ledger.describe_state(entry),
        "items": ledger.deliveries_for_registration(entry_id),
    }


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action_with_remark(
        entry_id, payload.action.strip(), payload.remark)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
