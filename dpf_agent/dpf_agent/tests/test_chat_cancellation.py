"""取消终态聊天文案回归：主动取消保留原因，不当作未知故障（不启动引擎）。"""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..domain import diagnosis
from ..schemas.enums import ErrorCode, OutputFormat, ResultScope, TaskStatus
from ..web import chat_api


class ChatCancellationTests(unittest.TestCase):
    def _render(self, response: dict, *, failure_prefix: str = "") -> tuple[tuple, dict]:
        service = SimpleNamespace(run=lambda request, cli_callback: response)
        with patch.object(chat_api, "_get_service", return_value=service), \
                patch.object(chat_api.store, "update_message_task") as update, \
                patch.object(chat_api._diagnosis, "suggest", wraps=diagnosis.suggest) as suggest:
            result = chat_api._run_case_into_message(
                SimpleNamespace(id="message-cancel"), "session-cancel", "计算 ieee33", "ieee33",
                result_scopes=[ResultScope.BUS, ResultScope.SYSTEM],
                output_formats=[OutputFormat.HTML],
                success_prefix="成功前缀不应出现在取消回复。", failure_prefix=failure_prefix,
            )
            if response["status"] == "cancelled":
                suggest.assert_not_called()
            return result, update.call_args.kwargs

    def test_cancelled_message_preserves_reason_and_terminal_identity(self) -> None:
        reason = "任务已被用户取消；在 load_case 阶段停止"
        (task_id, status, reply), saved = self._render({
            "task_id": "pf-cancelled", "status": "cancelled",
            "error": {"code": "CANCELLED", "message": reason, "stage": "load_case"},
            "artifacts": {},
        })
        self.assertEqual((task_id, status), ("pf-cancelled", TaskStatus.CANCELLED))
        self.assertIn("已取消", reply)
        self.assertIn(reason, reply)
        self.assertNotIn("未成功", reply)
        self.assertNotIn("未登记", reply)
        self.assertNotIn("排查方向", reply)
        self.assertNotIn("成功前缀", reply)
        self.assertEqual(saved["task_id"], task_id)
        self.assertEqual(saved["task_status"], "cancelled")
        self.assertEqual(saved["task_case_ref"], "ieee33")
        self.assertEqual(saved["content"], reply)

    def test_cancelled_batch_message_keeps_its_case_prefix(self) -> None:
        prefix = "### 1/2 · `caseA`\n\n"
        (_task_id, status, reply), saved = self._render({
            "task_id": "pf-batch-cancel", "status": "cancelled", "error": None,
        }, failure_prefix=prefix)
        self.assertEqual(status, TaskStatus.CANCELLED)
        self.assertTrue(reply.startswith(prefix))
        self.assertIn("任务已被用户取消", reply)
        self.assertEqual(saved["task_status"], "cancelled")

    def test_cancelled_without_error_uses_cancel_reason(self) -> None:
        (_task_id, _status, reply), saved = self._render({
            "task_id": "pf-cancel-empty", "status": "cancelled",
        })
        self.assertIn("已取消", reply)
        self.assertIn("任务已被用户取消", reply)
        self.assertEqual(saved["task_status"], "cancelled")

    def test_domain_cancellation_never_has_failure_directions(self) -> None:
        for code in ("CANCELLED", "cancelled", ErrorCode.CANCELLED):
            self.assertIsNone(diagnosis.suggest(code, "任务已被用户取消"))
        self.assertFalse(diagnosis.is_failure_status("cancelled"))
        self.assertIsNone(diagnosis.code_for_status("cancelled"))

    def test_actual_engine_failure_still_gets_diagnosis(self) -> None:
        (_task_id, status, reply), saved = self._render({
            "task_id": "pf-engine-failure", "status": "system_failed",
            "error": {"code": "ENGINE_ERROR", "message": "引擎健康检查失败"},
        })
        self.assertEqual(status, TaskStatus.SYSTEM_FAILED)
        self.assertIn("未成功", reply)
        self.assertIn("引擎健康检查失败", reply)
        self.assertIn("排查方向", reply)
        self.assertNotIn("已取消", reply)
        self.assertEqual(saved["task_status"], "system_failed")


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ChatCancellationTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
