#!/usr/bin/env python3
"""core/log_memory.py（案例记忆）与 core/learned_registers.py（架构规则记忆）
的确定性单测。零外部依赖，只验证签名构建、Jaccard 相似度、读写往返与
confirm 流程这些纯逻辑，不连 Ollama。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import log_memory  # noqa: E402
from core.log_facts import collect_log_facts  # noqa: E402


class LogMemorySignatureTests(unittest.TestCase):
    def test_resolve_memory_path_none_without_source_root(self) -> None:
        self.assertIsNone(log_memory.resolve_memory_path(None))
        self.assertIsNone(log_memory.resolve_memory_path("/path/does/not/exist"))

    def test_resolve_memory_path_creates_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = log_memory.resolve_memory_path(tmp)
            self.assertIsNotNone(path)
            self.assertTrue(os.path.isdir(os.path.join(tmp, ".ai_toolkit")))
            self.assertTrue(path.endswith("log_cases.jsonl"))

    def test_build_signature_from_facts_includes_stack_and_severity(self) -> None:
        log_text = "Assertion failed: x, file drivers/dma_ctrl.c, line 12, function dma_start_transfer"
        facts = collect_log_facts(log_text=log_text)
        hex_tokens, file_locations, severity_kinds = log_memory.build_signature_from_facts(facts)
        self.assertIn("dma_ctrl.c:12", file_locations)
        self.assertIn("ASSERT", severity_kinds)


class LogMemoryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = log_memory.resolve_memory_path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_and_load_roundtrip(self) -> None:
        facts = collect_log_facts(log_text="ERROR: boom at 0xDEAD")
        case = log_memory.new_case(facts, "why?", "root cause is X", iterations=1)
        case_id = log_memory.append_case(self.path, case)
        self.assertEqual(case_id, case.case_id)

        loaded = log_memory.load_cases(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].case_id, case.case_id)
        self.assertFalse(loaded[0].confirmed)

    def test_confirm_case_upgrades_confirmed_flag(self) -> None:
        facts = collect_log_facts(log_text="ERROR: boom at 0xDEAD")
        case = log_memory.new_case(facts, "why?", "root cause is X", iterations=1)
        log_memory.append_case(self.path, case)

        ok = log_memory.confirm_case(self.path, case.case_id)
        self.assertTrue(ok)
        loaded = log_memory.load_cases(self.path)
        self.assertTrue(loaded[0].confirmed)

    def test_confirm_case_returns_false_for_unknown_id(self) -> None:
        self.assertFalse(log_memory.confirm_case(self.path, "does-not-exist"))

    def test_find_similar_scores_by_jaccard_overlap(self) -> None:
        facts_a = collect_log_facts(log_text="ERROR: boom at 0xDEAD dma_ctrl.c:12")
        case_a = log_memory.new_case(facts_a, "q", "a", iterations=1)
        log_memory.append_case(self.path, case_a)

        # 与 case_a 完全同签名的新日志，应该以最高相似度命中
        facts_b = collect_log_facts(log_text="ERROR: boom at 0xDEAD dma_ctrl.c:12")
        cases = log_memory.load_cases(self.path)
        similar = log_memory.find_similar(cases, facts_b, weak_threshold=0.1)
        self.assertEqual(len(similar), 1)
        self.assertGreater(similar[0][1], 0.0)

    def test_find_similar_empty_when_no_signature_overlap(self) -> None:
        facts_a = collect_log_facts(log_text="ERROR: boom at 0xDEAD dma_ctrl.c:12")
        case_a = log_memory.new_case(facts_a, "q", "a", iterations=1)
        log_memory.append_case(self.path, case_a)

        facts_c = collect_log_facts(log_text="heartbeat only, nothing interesting")
        cases = log_memory.load_cases(self.path)
        similar = log_memory.find_similar(cases, facts_c, weak_threshold=0.1)
        self.assertEqual(similar, [])


class LearnedRegistersTests(unittest.TestCase):
    def test_add_register_note_and_reload(self) -> None:
        from core import learned_registers
        with tempfile.TemporaryDirectory() as tmp:
            ok = learned_registers.add_register_note(tmp, "0x3030=FOO_REG 一些描述")
            self.assertTrue(ok)
            regs = learned_registers.load_registers(tmp)
            self.assertEqual(len(regs), 1)
            self.assertEqual(regs[0]["register_name"], "FOO_REG")
            self.assertEqual(regs[0]["address"], "0x3030")

    def test_add_register_note_rejects_malformed_input(self) -> None:
        from core import learned_registers
        with tempfile.TemporaryDirectory() as tmp:
            ok = learned_registers.add_register_note(tmp, "not a valid note")
            self.assertFalse(ok)

    def test_add_register_note_overwrites_same_name(self) -> None:
        from core import learned_registers
        with tempfile.TemporaryDirectory() as tmp:
            learned_registers.add_register_note(tmp, "0x1000=FOO old desc")
            learned_registers.add_register_note(tmp, "0x2000=FOO new desc")
            regs = learned_registers.load_registers(tmp)
            self.assertEqual(len(regs), 1)
            self.assertEqual(regs[0]["address"], "0x2000")


if __name__ == "__main__":
    unittest.main()
