#!/usr/bin/env python3
"""core/project_facts.py 与 core/ut_framework.py 的目录指纹缓存单测（Phase 4）。

只验证「未变化时命中缓存、变化后缓存失效」这一确定性行为，不涉及任何
LLM 调用。缓存文件都在临时目录下的 .ai_toolkit/，测试结束自动清理。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.project_facts import collect_project_facts  # noqa: E402
from core.ut_framework import detect_ut_framework  # noqa: E402


class ProjectFactsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "helper.py"), "w", encoding="utf-8") as f:
            f.write("def foo():\n    pass\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cache_file_created_after_first_scan(self) -> None:
        collect_project_facts(self.root, "python")
        cache_path = os.path.join(self.root, ".ai_toolkit", "project_facts_cache.json")
        self.assertTrue(os.path.isfile(cache_path))

    def test_second_scan_reuses_cache_when_unchanged(self) -> None:
        facts1 = collect_project_facts(self.root, "python")
        # 缓存命中不应重新计算任何字段；直接对比反序列化结果与首次一致
        facts2 = collect_project_facts(self.root, "python")
        self.assertEqual(facts1.existing_symbols, facts2.existing_symbols)
        self.assertEqual(facts1.files_scanned, facts2.files_scanned)

    def test_cache_invalidated_after_new_file_added(self) -> None:
        collect_project_facts(self.root, "python")
        time.sleep(0.05)
        with open(os.path.join(self.root, "helper2.py"), "w", encoding="utf-8") as f:
            f.write("def bar():\n    pass\n")
        facts2 = collect_project_facts(self.root, "python")
        self.assertIn("bar", facts2.existing_symbols)

    def test_use_cache_false_bypasses_cache(self) -> None:
        collect_project_facts(self.root, "python", use_cache=True)
        with open(os.path.join(self.root, "helper.py"), "w", encoding="utf-8") as f:
            f.write("def foo():\n    pass\n\ndef newly_added():\n    pass\n")
        # 同一秒内 mtime 可能不变，use_cache=False 应该无视缓存强制重扫
        facts = collect_project_facts(self.root, "python", use_cache=False)
        self.assertIn("newly_added", facts.existing_symbols)


class UtFrameworkCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "test_sample.py"), "w", encoding="utf-8") as f:
            f.write("import pytest\n\ndef test_ok():\n    assert True\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cache_file_created_after_first_detect(self) -> None:
        detect_ut_framework(self.root, "python")
        cache_path = os.path.join(self.root, ".ai_toolkit", "ut_framework_cache.json")
        self.assertTrue(os.path.isfile(cache_path))

    def test_second_detect_matches_first(self) -> None:
        fw1 = detect_ut_framework(self.root, "python")
        fw2 = detect_ut_framework(self.root, "python")
        self.assertEqual(fw1.name, fw2.name)
        self.assertEqual(fw1.name, "pytest")

    def test_override_still_works_with_cache(self) -> None:
        detect_ut_framework(self.root, "python")
        fw = detect_ut_framework(self.root, "cpp", override="googletest")
        self.assertEqual(fw.name, "googletest")


if __name__ == "__main__":
    unittest.main()
