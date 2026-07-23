#!/usr/bin/env python3
"""project_facts / log_facts / validator 新增防臆造校验的确定性单测。

零外部依赖（不连 Ollama、不装 chip-manual-kit），只验证「事实抽取」与
「grounded 校验」这两层纯函数逻辑，因为这层才是整套防幻觉机制的地基。
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

from core import validator  # noqa: E402
from core.project_facts import collect_project_facts  # noqa: E402
from core.log_facts import collect_log_facts  # noqa: E402


class ProjectFactsPythonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "pkg_utils"))
        with open(os.path.join(self.root, "pkg_utils", "__init__.py"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(self.root, "pkg_utils", "helper.py"), "w", encoding="utf-8") as f:
            f.write(
                "import os\n"
                "import json\n"
                "\n"
                "def retry_call(fn):\n"
                "    return fn()\n"
                "\n"
                "class RetryPolicy:\n"
                "    pass\n"
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collects_modules_symbols_and_style(self) -> None:
        facts = collect_project_facts(self.root, "python")
        self.assertIn("pkg_utils", facts.available_py_modules)
        self.assertIn("retry_call", facts.existing_symbols)
        self.assertIn("RetryPolicy", facts.existing_symbols)
        self.assertEqual(facts.naming_style, "snake_case")
        self.assertTrue(any("import os" in inc or "import json" in inc for inc in facts.common_includes))


class ProjectFactsCppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "widget.hpp"), "w", encoding="utf-8") as f:
            f.write("#pragma once\nvoid widget_init(int x);\n")
        with open(os.path.join(self.root, "widget.cpp"), "w", encoding="utf-8") as f:
            f.write(
                '#include "widget.hpp"\n'
                "void widget_init(int x) {\n"
                "    (void)x;\n"
                "}\n"
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collects_headers_and_symbols(self) -> None:
        facts = collect_project_facts(self.root, "cpp")
        self.assertIn("widget.hpp", facts.available_headers)
        self.assertIn("widget_init", facts.existing_symbols)


class ValidatorAntiHallucinationTests(unittest.TestCase):
    def test_python_import_resolve_flags_fake_module(self) -> None:
        code = "import totally_made_up_module_xyz\n"
        result = validator.check_python_imports_resolve(code, known_modules=set())
        self.assertFalse(result.ok)
        self.assertIn("totally_made_up_module_xyz", result.error)

    def test_python_import_resolve_accepts_known_and_stdlib(self) -> None:
        code = "import os\nimport mypkg\n"
        result = validator.check_python_imports_resolve(code, known_modules={"mypkg"})
        self.assertTrue(result.ok)

    def test_python_import_resolve_skips_relative_imports(self) -> None:
        code = "from . import sibling\n"
        result = validator.check_python_imports_resolve(code, known_modules=set())
        self.assertTrue(result.ok)

    def test_cpp_includes_exist_flags_fake_header(self) -> None:
        code = '#include "not_real.h"\nvoid f() {}\n'
        result = validator.check_cpp_includes_exist(code, available_headers={"real.h"})
        self.assertFalse(result.ok)
        self.assertIn("not_real.h", result.error)

    def test_cpp_includes_exist_ignores_angle_bracket_system_headers(self) -> None:
        code = "#include <vector>\nvoid f() {}\n"
        result = validator.check_cpp_includes_exist(code, available_headers={"real.h"})
        self.assertTrue(result.ok)

    def test_cpp_includes_exist_skips_when_no_headers_known(self) -> None:
        code = '#include "whatever.h"\n'
        result = validator.check_cpp_includes_exist(code, available_headers=set())
        self.assertTrue(result.ok)

    def test_no_symbol_redefinition_flags_clash(self) -> None:
        code = "def retry_call():\n    pass\n"
        result = validator.check_no_symbol_redefinition(code, "python", {"retry_call"})
        self.assertFalse(result.ok)
        self.assertIn("retry_call", result.error)

    def test_no_symbol_redefinition_passes_when_unique(self) -> None:
        code = "def brand_new_helper():\n    pass\n"
        result = validator.check_no_symbol_redefinition(code, "python", {"retry_call"})
        self.assertTrue(result.ok)

    def test_grounded_references_flags_unknown_hex(self) -> None:
        result = validator.check_grounded_references(
            "寄存器值为 0xDEAD，参考事实中的 0x1000。", allowed_hex={"0x1000"}
        )
        self.assertFalse(result.ok)
        self.assertIn("0xDEAD", result.error)

    def test_grounded_references_passes_when_subset(self) -> None:
        result = validator.check_grounded_references(
            "参考 0x1000 与 0x1000 分析。", allowed_hex={"0x1000", "0x2000"}
        )
        self.assertTrue(result.ok)

    def test_grounded_references_flags_unknown_file_line(self) -> None:
        result = validator.check_grounded_references(
            "问题出在 fake_file.c:99 附近。", allowed_files={"real_file.c"}
        )
        self.assertFalse(result.ok)
        self.assertIn("fake_file.c:99", result.error)

    def test_grounded_references_skips_when_no_facts_given(self) -> None:
        result = validator.check_grounded_references("提到了 0xABCD 但没有给约束。")
        self.assertTrue(result.ok)

    def test_cites_primary_evidence_flags_unknown_line(self) -> None:
        facts = collect_log_facts(
            log_text="\n".join(f"line{i}" for i in range(5)) + "\nERROR: boom",
            context_lines=1,
        )
        result = validator.check_answer_cites_primary_evidence("问题出在第 999 行。", facts)
        self.assertFalse(result.ok)
        self.assertIn("999", result.error)

    def test_cites_primary_evidence_passes_for_real_excerpt_line(self) -> None:
        lines = [f"line{i}" for i in range(5)]
        lines[3] = "ERROR: boom"
        facts = collect_log_facts(log_text="\n".join(lines), context_lines=1)
        result = validator.check_answer_cites_primary_evidence("问题出在第 4 行。", facts)
        self.assertTrue(result.ok)

    def test_cites_primary_evidence_skips_when_no_line_facts(self) -> None:
        facts = collect_log_facts(log_text="")
        result = validator.check_answer_cites_primary_evidence("第 12345 行有问题。", facts)
        self.assertTrue(result.ok)


class LogFactsTests(unittest.TestCase):
    def test_excerpt_centers_on_keyword_with_context(self) -> None:
        lines = [f"info line {i}" for i in range(10)]
        lines[5] = "ERROR: something broke at 0x1010"
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        self.assertIn("ERROR", facts.excerpt)
        self.assertIn("0x1010", facts.hex_tokens)
        # 上下文窗口只应包含第 3~7 行（1-indexed），不应把全部 10 行都摘进来
        self.assertIn("4:", facts.excerpt)
        self.assertNotIn("info line 0", facts.excerpt)

    def test_abbreviated_severity_tag_is_detected(self) -> None:
        # 真实 l1sw 日志的常见格式：不拼全 "ERROR"，用 "43/ERR:" 这种缩写，
        # 曾经完全漏检（回归用例，来自真实 journal 日志踩过的坑）。
        lines = [f"noise {i} INF stuff" for i in range(10)]
        lines[6] = "2026-05-28T07:08:31Z ip_mgmtd[1322]: F6/ERR ip_mgmt: laser disable failed for SFP 20"
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        self.assertIn("laser disable failed", facts.excerpt)

    def test_truncated_file_reports_absolute_line_numbers(self) -> None:
        # 大文件只扫描尾部时，excerpt 里标的行号必须是【原始文件】的真实行号，
        # 不能是"截断后缓冲区"里从 1 开始重新计的相对行号——否则用户按行号去
        # 原始文件核对时会对不上（这是本工具在真实 24MB l1sw 日志上踩过的坑）。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.log")
            n = 5000
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                for i in range(n):
                    f.write(f"filler padding padding padding {i}\n")
                f.write("ERROR: real fault near the end\n")
                for i in range(5):
                    f.write(f"trailer {i}\n")
            # 用一个足够小的扫描预算强制触发"只扫描尾部"截断
            facts = collect_log_facts(log_file=path, max_scan_bytes=2000, context_lines=1)
            self.assertTrue(facts.truncated)
            self.assertGreater(facts.line_offset, 0)
            # 报错行是第 n+1 行（1-indexed），excerpt 里必须标注这个绝对行号
            self.assertIn(f"{n + 1}: ERROR: real fault near the end", facts.excerpt)

    def test_zero_value_counter_is_not_treated_as_failure(self) -> None:
        # "recvReq failures:0" 是健康遥测（0 次失败），不该被当成异常摘进去，
        # 否则一堆健康计数器会把摘录预算全占满，挤掉真正的错误行。
        lines = [f"printStatistic recvReq failures:0 line{i}" for i in range(50)]
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        # 没有真正的高危/中危信号，应退化为尾部摘录，而不是把 0 值计数器当命中
        self.assertIn("line49", facts.excerpt)

    def test_all_zero_parenthesized_tuple_is_not_treated_as_failure(self) -> None:
        # 真实电信日志常见形态："Error=(0 0 0 0 0 0 0 0)"——全零元组代表【没有】
        # 发生，之前只处理裸的 ":0"/"=0"，漏了这种括号元组，导致成千上万条健康
        # 遥测行被误判为高危，把真正的报错挤出摘录预算（回归用例）。
        lines = [
            f"printEcpriStatisticPerPort port {i}: Error=(0 0 0 0 0 0 0 0)"
            for i in range(50)
        ]
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        self.assertIn("port 49", facts.excerpt)  # 退化为尾部摘录，不是把全零行当命中

    def test_parenthesized_tuple_with_nonzero_value_is_kept(self) -> None:
        lines = [f"noise {i}" for i in range(10)]
        lines[5] = "ERROR: printEcpriStatisticPerPort port 3: Error=(1 0 0 0 0 0 0 0)"
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        self.assertIn("Error=(1 0 0 0 0 0 0 0)", facts.excerpt)

    def test_later_window_kept_when_budget_too_small_for_all(self) -> None:
        # 预算不够装下全部命中窗口时，应保留【更靠后】的（现场问题多在末尾），
        # 而不是简单从头截断丢掉最新的报错。
        lines = [f"filler line {i}" for i in range(2000)]
        lines[100] = "ERROR: first fault"
        lines[1900] = "ERROR: second fault, the real one"
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=1, excerpt_token_budget=15)
        self.assertIn("second fault", facts.excerpt)
        self.assertNotIn("first fault", facts.excerpt)

    def test_no_keyword_falls_back_to_tail(self) -> None:
        lines = [f"heartbeat {i}" for i in range(100)]
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=2)
        self.assertIn("heartbeat 99", facts.excerpt)

    def test_register_lookup_by_address(self) -> None:
        kb = {
            "registers": [
                {"register_name": "CTRL_STATUS", "module": "ACME", "address": "0x1010",
                 "description": "control/status register"},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(kb, f)
            kb_path = f.name
        try:
            facts = collect_log_facts(
                log_text="dump: reg=0x1010 val=0xFF", kb_path=kb_path
            )
            names = [h.register_name for h in facts.register_hits]
            self.assertIn("CTRL_STATUS", names)
        finally:
            os.unlink(kb_path)

    def test_source_location_resolved_from_local_root(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            src_dir = os.path.join(tmp.name, "app", "drivers")
            os.makedirs(src_dir)
            src_file = os.path.join(src_dir, "widget.c")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write("\n".join(f"line{i}" for i in range(1, 21)))

            log_text = "Assertion failed: x, file /host/workdir/proj/app/drivers/widget.c, line 10, function do_thing"
            facts = collect_log_facts(log_text=log_text, source_root=tmp.name, context_lines=1)
            self.assertEqual(len(facts.source_locations), 1)
            loc = facts.source_locations[0]
            self.assertEqual(loc.line, 10)
            self.assertTrue(loc.resolved_path.endswith("widget.c"))
            self.assertIn("line9", loc.context)
            self.assertIn("line10", loc.context)
        finally:
            tmp.cleanup()

    def test_ambiguous_source_location_not_resolved(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            for sub in ("a", "b"):
                d = os.path.join(tmp.name, sub)
                os.makedirs(d)
                with open(os.path.join(d, "dup.c"), "w", encoding="utf-8") as f:
                    f.write("x\n")
            log_text = "at dup.c:1"
            facts = collect_log_facts(log_text=log_text, source_root=tmp.name)
            self.assertEqual(len(facts.source_locations), 1)
            self.assertTrue(facts.source_locations[0].ambiguous)
            self.assertEqual(facts.source_locations[0].context, "")
        finally:
            tmp.cleanup()

    def test_repeated_error_folds_into_error_group(self) -> None:
        # 同一归一化模板的高危报错重复 >= min_repeat_to_fold 次时，应折叠为
        # 一条 ErrorGroup（只保留首末位置），而不是把整批命中窗口都摘进 excerpt。
        lines = []
        for i in range(20):
            lines.append(f"2026-05-28T07:0{i % 6}:00Z ERROR: retry failed for port {i} at 0xAA{i:02d}")
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=1, min_repeat_to_fold=3)
        self.assertEqual(len(facts.error_groups), 1)
        group = facts.error_groups[0]
        self.assertEqual(group.first_line, 1)
        self.assertEqual(group.last_line, 20)
        self.assertEqual(group.count, 20)

    def test_repeat_below_threshold_not_folded(self) -> None:
        lines = [f"noise {i}" for i in range(10)]
        lines[3] = "ERROR: fault A"
        lines[7] = "ERROR: fault B"
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=1, min_repeat_to_fold=3)
        self.assertEqual(facts.error_groups, [])

    def test_gdb_style_stack_trace_parsed(self) -> None:
        lines = [
            "some preceding log line",
            "#0  0x0000aaaa in dma_start_transfer (len=99) at drivers/dma_ctrl.c:12",
            "#1  0x0000bbbb in dma_ioctl_handler () at drivers/dma_ctrl.c:40",
            "some trailing log line",
        ]
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=1)
        self.assertEqual(len(facts.stack_traces), 1)
        trace = facts.stack_traces[0]
        self.assertEqual([fr.index for fr in trace.frames], [0, 1])
        self.assertEqual(trace.frames[0].function, "dma_start_transfer")
        self.assertEqual(trace.frames[0].file, "drivers/dma_ctrl.c")
        self.assertEqual(trace.frames[0].line, 12)
        self.assertEqual(trace.anchor_line, 2)

    def test_python_traceback_parsed(self) -> None:
        lines = [
            "Traceback (most recent call last):",
            '  File "app/main.py", line 10, in run',
            '  File "app/worker.py", line 42, in process',
            "ValueError: bad value",
        ]
        text = "\n".join(lines)
        facts = collect_log_facts(log_text=text, context_lines=1)
        self.assertEqual(len(facts.stack_traces), 1)
        trace = facts.stack_traces[0]
        self.assertEqual(len(trace.frames), 2)
        self.assertEqual(trace.frames[0].function, "run")
        self.assertEqual(trace.frames[0].file, "app/main.py")
        self.assertEqual(trace.frames[0].line, 10)
        self.assertEqual(trace.frames[1].function, "process")

    def test_glibc_assert_produces_single_frame_stack_trace(self) -> None:
        log_text = "Assertion failed: len <= MAX, file drivers/dma_ctrl.c, line 12, function dma_start_transfer"
        facts = collect_log_facts(log_text=log_text)
        self.assertEqual(len(facts.stack_traces), 1)
        trace = facts.stack_traces[0]
        self.assertEqual(len(trace.frames), 1)
        self.assertEqual(trace.frames[0].function, "dma_start_transfer")
        self.assertEqual(trace.frames[0].file, "drivers/dma_ctrl.c")
        self.assertEqual(trace.frames[0].line, 12)

    def test_learned_registers_merge_with_kb(self) -> None:
        # Phase 3：learned_registers.json（人工确认的架构规则）应与外部 kb 叠加，
        # 且完全不依赖 kb_path（即使不传 kb_path 也能命中）。
        from core import learned_registers
        tmp = tempfile.TemporaryDirectory()
        try:
            ok = learned_registers.add_register_note(tmp.name, "0x2020=MY_REG 自定义寄存器")
            self.assertTrue(ok)
            facts = collect_log_facts(
                log_text="dump: reg=0x2020 val=0x1", source_root=tmp.name
            )
            names = [h.register_name for h in facts.register_hits]
            self.assertIn("MY_REG", names)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
