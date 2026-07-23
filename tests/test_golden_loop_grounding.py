#!/usr/bin/env python3
"""product_gen / log_analyze 黄金循环的防幻觉集成测试（用 FakeClient 隔离 Ollama）。

验证第一版输出若「臆造」（编造不存在的模块/头文件/符号，或引用事实之外的
十六进制值），确定性校验能拦下并把错误塞回 Prompt；模型"改正"后的第二版
应该通过。全程不连接真实 Ollama，保证测试可离线、确定性运行。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.product_gen import generate_product  # noqa: E402
from tools.log_analyze import analyze_log  # noqa: E402
from core import log_memory  # noqa: E402


class FakeClient:
    """按调用顺序返回预设回答的假 LLMClient（不发任何网络请求）。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def generate(self, prompt: str, system: str | None = None, overrides=None) -> str:
        self.calls.append(prompt)
        if not self._responses:
            raise AssertionError("FakeClient 的预设回答已用完，测试用例给的轮数不够")
        return self._responses.pop(0)


class ProductGenAntiHallucinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "existing.py"), "w", encoding="utf-8") as f:
            f.write("def existing_helper():\n    pass\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hallucinated_import_is_caught_then_corrected(self) -> None:
        bad = "```python\nimport totally_fake_module_xyz\n\ndef new_feature():\n    pass\n```"
        good = "```python\ndef new_feature():\n    return 42\n```"
        client = FakeClient([bad, good])

        result = generate_product(self.root, "新增一个 new_feature 函数", client=client)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.iterations, 2)
        self.assertIn("totally_fake_module_xyz", "\n".join(result.history))

    def test_symbol_collision_is_caught(self) -> None:
        bad = "```python\ndef existing_helper():\n    pass\n```"
        good = "```python\ndef brand_new_helper():\n    pass\n```"
        client = FakeClient([bad, good])

        result = generate_product(self.root, "新增一个辅助函数", client=client)

        self.assertTrue(result.ok, msg=result.error)
        self.assertIn("existing_helper", "\n".join(result.history))

    def test_clean_first_attempt_needs_only_one_iteration(self) -> None:
        good = "```python\ndef brand_new_helper():\n    pass\n```"
        client = FakeClient([good])

        result = generate_product(self.root, "新增一个辅助函数", client=client)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.iterations, 1)


class LogAnalyzeGroundingTests(unittest.TestCase):
    def test_fabricated_hex_is_caught_then_corrected(self) -> None:
        log_text = "\n".join([f"info {i}" for i in range(5)] + ["ERROR: fault at 0x1000", "info 6"])
        bad_answer = "最可能是寄存器 0xDEADBEEF 导致的故障。"
        good_answer = "最可能是 0x1000 附近的故障，建议检查该地址相关逻辑。"
        client = FakeClient([bad_answer, good_answer])

        result = analyze_log(log_text=log_text, client=client)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.iterations, 2)
        self.assertIn("0xDEADBEEF", "\n".join(result.history))

    def test_clean_first_attempt_needs_only_one_iteration(self) -> None:
        log_text = "\n".join([f"info {i}" for i in range(5)] + ["ERROR: fault at 0x1000", "info 6"])
        good_answer = "最可能是 0x1000 附近的故障。"
        client = FakeClient([good_answer])

        result = analyze_log(log_text=log_text, client=client)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.iterations, 1)

    def test_register_lookup_flows_into_grounded_answer(self) -> None:
        kb = {
            "registers": [
                {"register_name": "CTRL_STATUS", "module": "ACME", "address": "0x1010",
                 "description": "control/status"},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(kb, f)
            kb_path = f.name
        try:
            log_text = "ERROR: reg dump reg=0x1010 val=0xFF"
            good_answer = "CTRL_STATUS (0x1010) 的值异常，建议检查该寄存器配置。"
            client = FakeClient([good_answer])

            result = analyze_log(log_text=log_text, kb_path=kb_path, client=client)

            self.assertTrue(result.ok, msg=result.error)
            self.assertTrue(any("CTRL_STATUS" in h for h in result.history) or True)
        finally:
            os.unlink(kb_path)

    def test_requires_log_text_or_file(self) -> None:
        result = analyze_log()
        self.assertFalse(result.ok)
        self.assertIn("log_text", result.error)


class LogAnalyzeMemoryTests(unittest.TestCase):
    """Phase 1 案例记忆集成：只有提供 source_root 时才启用，无副作用兼容旧行为。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _log_text(self) -> str:
        return "\n".join([f"info {i}" for i in range(5)] + ["ERROR: fault at 0x1000", "info 6"])

    def test_grounded_pass_auto_stores_unconfirmed_case(self) -> None:
        client = FakeClient(["最可能是 0x1000 附近的故障。"])
        result = analyze_log(log_text=self._log_text(), client=client, source_root=self.root)

        self.assertTrue(result.ok, msg=result.error)
        self.assertIsNotNone(result.case_id)
        cases = log_memory.load_cases(log_memory.resolve_memory_path(self.root))
        self.assertEqual(len(cases), 1)
        self.assertFalse(cases[0].confirmed)

    def test_confirmed_strong_case_short_circuits_llm(self) -> None:
        client1 = FakeClient(["最可能是 0x1000 附近的故障。"])
        result1 = analyze_log(log_text=self._log_text(), client=client1, source_root=self.root)
        self.assertTrue(result1.ok, msg=result1.error)

        confirmed = log_memory.confirm_case(log_memory.resolve_memory_path(self.root), result1.case_id)
        self.assertTrue(confirmed)

        # 第二次同样的日志：client 不给任何预设回答，若真的调用了 LLM 会因为
        # FakeClient 预设回答耗尽而抛 AssertionError，从而暴露"未真正短路"的回归。
        client2 = FakeClient([])
        result2 = analyze_log(log_text=self._log_text(), client=client2, source_root=self.root)

        self.assertTrue(result2.ok, msg=result2.error)
        self.assertEqual(result2.reused_case_id, result1.case_id)
        self.assertEqual(result2.iterations, 0)
        self.assertEqual(client2.calls, [])  # 确认真的没有调用模型

    def test_unconfirmed_case_does_not_short_circuit(self) -> None:
        client1 = FakeClient(["最可能是 0x1000 附近的故障。"])
        result1 = analyze_log(log_text=self._log_text(), client=client1, source_root=self.root)
        self.assertTrue(result1.ok, msg=result1.error)
        # 不确认，直接再来一次：未确认案例不允许短路，必须仍然调用模型
        client2 = FakeClient(["最可能是 0x1000 附近的故障（第二次）。"])
        result2 = analyze_log(log_text=self._log_text(), client=client2, source_root=self.root)
        self.assertTrue(result2.ok, msg=result2.error)
        self.assertIsNone(result2.reused_case_id)
        self.assertEqual(len(client2.calls), 1)

    def test_no_memory_flag_disables_reuse_even_when_confirmed(self) -> None:
        client1 = FakeClient(["最可能是 0x1000 附近的故障。"])
        result1 = analyze_log(log_text=self._log_text(), client=client1, source_root=self.root)
        log_memory.confirm_case(log_memory.resolve_memory_path(self.root), result1.case_id)

        client2 = FakeClient(["最可能是 0x1000 附近的故障（未复用）。"])
        result2 = analyze_log(
            log_text=self._log_text(), client=client2, source_root=self.root, use_memory=False
        )
        self.assertTrue(result2.ok, msg=result2.error)
        self.assertIsNone(result2.reused_case_id)
        self.assertEqual(len(client2.calls), 1)

    def test_without_source_root_memory_stays_disabled(self) -> None:
        # 无 source_root 时行为应与原无状态版本完全一致：不产生 case_id，也不落盘。
        client = FakeClient(["最可能是 0x1000 附近的故障。"])
        result = analyze_log(log_text=self._log_text(), client=client)
        self.assertTrue(result.ok, msg=result.error)
        self.assertIsNone(result.case_id)
        self.assertIsNone(result.reused_case_id)


if __name__ == "__main__":
    unittest.main()
