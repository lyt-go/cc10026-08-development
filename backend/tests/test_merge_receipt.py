"""归并撤回回执与导出重办测试：验证正式批次、撤回回执和交付身份流转。

覆盖以下几件事：
1. 正式批次发布后在台账中冻结来源与列结构；
2. 撤回时签发回执，受影响登记按是否已出文件进入“已撤回/待重办”；
3. 撤回批次不能再生成新交付；
4. 更正来源补齐并发布更正批次后才能重办；
5. 重办后的登记同时关联原归并、实际数据源和新文件，旧交付可对账。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

temp_data = tempfile.mkdtemp(prefix='merge_receipt_test_')
os.environ['DATA_ROOT'] = temp_data


def _fresh():
    """重置 store 与台账，并清空临时数据目录。"""
    import shutil
    from app.seed import SEED_ROWS
    from app.store import store
    from app.services.delivery_ledger import ledger

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    if os.path.exists(temp_data):
        shutil.rmtree(temp_data, ignore_errors=True)
    os.makedirs(temp_data, exist_ok=True)
    ledger.reset(seed_demo=True)
    return store, ledger


def _services():
    from app.services import service_for
    return service_for("data_merge"), service_for("export_task")


class TestMergeWithdrawReceipt:
    def test_confirmed_batch_freezes_source_and_columns(self):
        _, ledger = _fresh()
        batch = ledger.get_batch("MRG-DATA-0003-01")
        assert batch is not None
        assert batch["status"] == "已确认"
        assert batch["primary_api"] == "会员主数据API"
        assert batch["secondary_api"] == "会员扩展资料API"
        assert "member_id" in batch["columns"]
        # 正式批次交付的每一列都来自该批次的冻结列。
        delivery = ledger.deliveries["DLV-EXPO-0003-01"]
        assert delivery["column_source"]["batch_no"] == batch["batch_no"]
        assert delivery["columns"] == batch["columns"]

    def test_withdraw_issues_receipt_and_transitions_registrations(self):
        store, ledger = _fresh()
        merge, _ = _services()
        row_before_2 = store.find("export_task", 2)["登记状态"]
        row_before_3 = store.find("export_task", 3)["登记状态"]
        assert row_before_2 == "已登记"
        assert row_before_3 == "已交付"

        entry, message = merge.run_action_with_remark(
            3, "撤回正式批次", "发出去的归并版引用了错误来源")
        assert entry is not None, message

        receipt = ledger.receipt_for_batch("MRG-DATA-0003-01")
        assert receipt is not None
        assert receipt["reason"] == "发出去的归并版引用了错误来源"
        states = {
            item["registration_id"]: (item["state_before"], item["state_after"])
            for item in receipt["affected"]
        }
        # 已出文件的登记 -> 已撤回；尚未出文件的登记 -> 待重办。
        assert states[3] == ("已交付", "已撤回")
        assert states[2] == ("已登记", "待重办")
        assert store.find("export_task", 3)["撤回回执"] == receipt["receipt_no"]
        assert store.find("export_task", 2)["撤回回执"] == receipt["receipt_no"]
        # 旧交付保留下来供对账，但状态改为已撤回。
        assert ledger.deliveries["DLV-EXPO-0003-01"]["status"] == "已撤回"
        assert ledger.deliveries["DLV-EXPO-0003-01"]["batch_no"] == "MRG-DATA-0003-01"
        assert receipt["old_deliveries"][0]["delivery_id"] == "DLV-EXPO-0003-01"

    def test_withdrawn_batch_cannot_generate_new_delivery(self):
        store, _ = _fresh()
        merge, export = _services()
        merge.run_action(3, "撤回正式批次")
        before = len(store.rows("export_task"))

        result, message = export.run_action(2, "发起导出")
        assert result is None
        assert "已撤回" in message and "不能再出新交付" in message
        assert store.find("export_task", 2)["登记状态"] == "待重办"
        assert len(store.rows("export_task")) == before

    def test_duplicate_withdraw_rejected(self):
        _, ledger = _fresh()
        merge, _ = _services()
        merge.run_action(3, "撤回正式批次")
        first = ledger.receipt_for_batch("MRG-DATA-0003-01")
        entry, message = merge.run_action(3, "撤回正式批次")
        assert entry is None
        assert "不能再次撤回" in message
        assert len(ledger.receipts) == 1
        assert first["receipt_no"] in ledger.receipts

    def test_reissue_blocked_until_correction_source_ready(self):
        store, ledger = _fresh()
        merge, export = _services()
        merge.run_action(3, "撤回正式批次")

        result, message = export.run_action(3, "重办导出")
        assert result is None
        assert "更正来源" in message and "补齐" in message
        assert store.find("export_task", 3)["登记状态"] == "已撤回"
        assert len(ledger.deliveries_for_registration(3)) == 1

        merge.run_action(3, "登记更正来源")
        result, message = export.run_action(3, "重办导出")
        assert result is not None, message
        assert store.find("export_task", 3)["登记状态"] == "已重办"

    def test_reissue_links_original_batch_source_and_new_file(self):
        store, ledger = _fresh()
        merge, export = _services()
        merge.run_action(3, "撤回正式批次")
        merge.run_action(3, "登记更正来源")
        corrected, message = merge.run_action(3, "发布更正批次")
        assert corrected is not None, message
        new_batch_no = corrected["当前批次"]
        assert new_batch_no == "MRG-DATA-0003-02"
        assert corrected["原归并批次"] == "MRG-DATA-0003-01"

        result, message = export.run_action(3, "重办导出")
        assert result is not None, message
        row = store.find("export_task", 3)
        assert row["原归并批次"] == "MRG-DATA-0003-01"
        assert row["归并批次"] == new_batch_no
        assert row["实际数据源"] == "会员主数据API + 会员扩展资料API"
        assert row["交付编号"] == "DLV-EXPO-0003-02"
        assert row["交付文件"].endswith("DLV-EXPO-0003-02.csv")
        assert os.path.exists(os.path.join(temp_data, row["交付文件"]))

        deliveries = ledger.deliveries_for_registration(3)
        assert [d["status"] for d in deliveries] == ["已撤回", "有效"]
        assert deliveries[0]["delivery_id"] == "DLV-EXPO-0003-01"
        assert deliveries[0]["superseded_by"] == "DLV-EXPO-0003-02"
        assert deliveries[1]["batch_no"] == new_batch_no
        # 交付列整体来自更正批次。
        assert deliveries[1]["column_source"]["batch_no"] == new_batch_no
        assert deliveries[1]["columns"] == ledger.get_batch(new_batch_no)["columns"]

    def test_registration_without_file_reissued_with_single_delivery(self):
        store, ledger = _fresh()
        merge, export = _services()
        # 先让 2 号登记出一份文件，再撤回，覆盖“有交付”的路径；
        # 3 号登记的旧交付已经在撤回中被标记为已撤回。
        export.run_action(2, "发起导出")
        assert store.find("export_task", 2)["登记状态"] == "已交付"
        merge.run_action(3, "撤回正式批次")
        assert store.find("export_task", 2)["登记状态"] == "已撤回"

        merge.run_action(3, "登记更正来源")
        merge.run_action(3, "发布更正批次")
        result, message = export.run_action(2, "重办导出")
        assert result is not None, message
        deliveries = ledger.deliveries_for_registration(2)
        assert [d["delivery_id"] for d in deliveries] == [
            "DLV-EXPO-0002-01", "DLV-EXPO-0002-02"]
        assert deliveries[0]["status"] == "已撤回"
        assert deliveries[1]["status"] == "有效"

    def test_old_delivery_files_remain_for_reconciliation(self):
        _, ledger = _fresh()
        merge, export = _services()
        merge.run_action(3, "撤回正式批次")
        merge.run_action(3, "登记更正来源")
        merge.run_action(3, "发布更正批次")
        export.run_action(3, "重办导出")

        old = ledger.deliveries["DLV-EXPO-0003-01"]
        new = ledger.deliveries["DLV-EXPO-0003-02"]
        assert old["status"] == "已撤回"
        assert new["status"] == "有效"
        # 两个交付身份各自引用自己的批次，旧交付的来源关系没有被改写。
        assert old["batch_no"] == "MRG-DATA-0003-01"
        assert old["source_relation"]["secondary_api"] == "会员扩展资料API"
        assert new["batch_no"] == "MRG-DATA-0003-02"
        assert old["delivery_id"] != new["delivery_id"]
        assert old["file"] != new["file"]
        assert os.path.exists(os.path.join(temp_data, old["file"]))
        assert os.path.exists(os.path.join(temp_data, new["file"]))

    def test_publish_requires_effective_rule_and_no_open_batch(self):
        _, _ = _fresh()
        merge, _ = _services()
        entry, message = merge.run_action(1, "发布正式批次")
        assert entry is None and "已生效" in message
        entry, message = merge.run_action(3, "发布正式批次")
        assert entry is None and "先撤回" in message

    def test_corrected_batch_requires_ready_source(self):
        _, _ = _fresh()
        merge, _ = _services()
        merge.run_action(3, "撤回正式批次")
        entry, message = merge.run_action(3, "发布更正批次")
        assert entry is None and "尚未补齐" in message
        entry, message = merge.run_action(3, "登记更正来源")
        assert entry is not None
        entry, message = merge.run_action(3, "发布更正批次")
        assert entry is not None and "MRG-DATA-0003-02" in message
        assert entry["批次状态"] == "已更正"

    def test_receipt_persisted_for_downstream_reads(self):
        _, ledger = _fresh()
        merge, _ = _services()
        merge.run_action(3, "撤回正式批次", )
        from app.services.data_manager import list_receipts, read_merge_ledger, read_delivery_file
        receipts = list_receipts()
        assert receipts and receipts[0]["batch_no"] == "MRG-DATA-0003-01"
        stored = read_merge_ledger()
        assert "MRG-DATA-0003-01" in stored["batches"]
        delivery = read_delivery_file("DLV-EXPO-0003-01")
        assert delivery["batch_no"] == "MRG-DATA-0003-01"

    def test_new_registration_can_bind_batch_without_file(self):
        store, _ = _fresh()
        _, export = _services()
        entry, missing = export.create_entry({
            "任务编号": "EXPO-0009",
            "关联API": "会员主数据API",
            "查询参数": "replacement=all",
            "归并批次": "MRG-DATA-0003-01",
        })
        assert entry is not None and not missing
        assert entry["登记状态"] == "已登记"
        result, message = export.run_action(entry["id"], "发起导出")
        assert result is not None, message
        assert result["登记状态"] == "已交付"
        assert result["交付编号"]
