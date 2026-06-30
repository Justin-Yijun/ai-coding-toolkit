# =============================================================================
# tools/ut_gen.py
# 用途：单元测试生成 Skill（多框架）。
#   输入：源文件路径 + 函数名（可选：显式框架、项目根目录）。
#   关键能力：**自动探测项目现有 UT 框架并参照其风格**。
#       支持 pytest（Python）、googletest（C++）、cpputest（C/C++）。
#
#   流程（黄金法则，最多迭代 3 次）：
#       1) 按语言切出「单个目标函数」源码（Python 走 AST，C/C++ 走括号配对）。
#       2) 探测项目主用 UT 框架，并截取一段现有测试作为 few-shot 参照。
#       3) 让模型按该框架风格生成测试。
#       4) 确定性校验：
#            · pytest      → ast 语法 + 隔离环境实跑 pytest
#            · googletest  → 括号配对 + TEST/EXPECT 宏结构校验
#            · cpputest    → 括号配对 + TEST_GROUP/CHECK 宏结构校验
#          失败则把报错塞回 Prompt 重生成。
#
# 无状态：只依赖传入的 file_path/func_name/project_root，不读对话历史。
# =============================================================================
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_client import LLMClient, load_config
from core.text_utils import extract_code_block
from core.lang_utils import (
    detect_language,
    extract_function_source,
    extract_include_lines,
    extract_called_symbols,
    find_declaring_header,
)
from core.ut_framework import detect_ut_framework, UTFramework
from core import validator

# 各框架的 system 提示 + 代码围栏语言
_FRAMEWORK_META = {
    "pytest": {
        "fence": "python",
        "system": (
            "你是资深 Python 测试工程师。只输出一个完整的 pytest 测试文件，"
            "用 ```python 围栏包裹，不要解释。被测模块通过 "
            "`from target import <函数名>` 导入。"
        ),
    },
    "googletest": {
        "fence": "cpp",
        "system": (
            "你是资深 C++ 测试工程师。只输出一个完整的 GoogleTest 测试文件，"
            "用 ```cpp 围栏包裹，不要解释。使用 TEST/TEST_F 与 EXPECT_*/ASSERT_* 断言，"
            "并 #include 被测头文件。"
        ),
    },
    "cpputest": {
        "fence": "cpp",
        "system": (
            "你是资深嵌入式 C/C++ 测试工程师。只输出一个完整的 CppUTest 测试文件，"
            "用 ```cpp 围栏包裹，不要解释。使用 TEST_GROUP/TEST 与 CHECK*/LONGS_EQUAL "
            "等断言，并 #include 被测头文件与 CppUTest 头文件。"
        ),
    },
}


@dataclass
class ToolResult:
    """工具统一返回结构。"""
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)


@dataclass
class CodeFacts:
    """从被测代码里【确定性】提取的事实，用于压制弱模型臆造。

    - header_include：声明被测函数的真实头文件名（用于 #include）。
    - source_includes：被测源文件自身的 #include 行（测试多半需要同样依赖）。
    - collaborators：函数体内真实调用到的符号（要 mock/断言就用这些真名）。
    """
    header_include: str = ""
    source_includes: List[str] = field(default_factory=list)
    collaborators: List[str] = field(default_factory=list)


def _collect_code_facts(
    file_path: str, func_name: str, func_src: str, root: str, language: str
) -> CodeFacts:
    """仅 C/C++ 需要：把能从代码确定的事实抽出来（无网弱模型场景的关键锚点）。"""
    facts = CodeFacts()
    if language != "cpp":
        return facts
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            facts.source_includes = extract_include_lines(f.read())
    except OSError:
        pass
    facts.header_include = find_declaring_header(root, func_name, file_path) or ""
    facts.collaborators = [s for s in extract_called_symbols(func_src) if s != func_name]
    return facts


def _build_prompt(
    func_src: str,
    func_name: str,
    fw: UTFramework,
    fence: str,
    last_error: Optional[str],
    facts: Optional[CodeFacts] = None,
) -> str:
    parts: List[str] = [
        f"请为下面这个函数编写 {fw.name} 单元测试，覆盖正常路径与边界/异常情况。",
        f"被测函数名: {func_name}。",
    ]
    if fw.name == "pytest":
        parts.append("被测函数已放在名为 target 的模块中，请 `from target import ...`。")

    # 关键：注入项目现有测试作为风格参照（大多数时候要照着既有框架写）
    if fw.reference_snippet.strip():
        ref_note = "（探测到的现有测试）" if fw.detected else "（默认模板）"
        parts += [
            "",
            f"请严格参照本项目现有的 {fw.name} 测试风格{ref_note}：",
            f"```{fence}",
            fw.reference_snippet,
            "```",
        ]

    # 关键：注入「确定性事实」，压制头文件名/协作者名的臆造
    if facts is not None:
        if facts.header_include:
            parts += [
                "",
                f"【已确定的事实】被测函数声明在头文件 `{facts.header_include}`，"
                f'测试请使用 `#include "{facts.header_include}"`，不要臆造其它头文件名。',
            ]
        if facts.source_includes:
            parts += [
                "",
                "被测源文件本身的 #include（你的测试很可能需要相同的依赖头）：",
                f"```{fence}",
                "\n".join(facts.source_includes),
                "```",
            ]
        if facts.collaborators:
            parts += [
                "",
                "函数体内【实际调用到的符号】（若需 mock 或断言，请使用这些真实名字，"
                "不要编造不存在的协作者）：",
                "、".join(facts.collaborators),
            ]

    parts += [
        "",
        "被测函数源码：",
        f"```{fence}",
        func_src,
        "```",
    ]
    if last_error:
        parts += [
            "",
            "上一版测试未通过校验，错误如下，请修正后重新输出完整测试文件：",
            "```text",
            last_error,
            "```",
        ]
    return "\n".join(parts)


def _validate(test_code: str, fw: UTFramework, func_src: str, cfg: dict) -> validator.CheckResult:
    """按框架分派确定性校验。"""
    if fw.validate == "pytest":
        syntax = validator.check_python_syntax(test_code)
        if not syntax.ok:
            return syntax
        pytest_timeout = int(cfg.get("validator", {}).get("pytest_timeout", 60))
        return validator.run_pytest(
            test_code,
            extra_files=[("target.py", func_src)],
            timeout=pytest_timeout,
        )
    # C/C++ 结构性校验（离线无构建系统时的确定性闸门）
    return validator.check_cpp_test_structure(test_code, fw.name)


def generate_unit_test(
    file_path: str,
    func_name: str,
    framework: Optional[str] = None,
    project_root: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> ToolResult:
    """主入口：探测框架 → 生成 → 校验 → 回填迭代。"""
    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))
    ut_cfg = cfg.get("ut", {})
    override = framework or (ut_cfg.get("framework") or None)
    ref_budget = int(ut_cfg.get("reference_token_budget", 800))

    if not os.path.exists(file_path):
        return ToolResult(ok=False, error=f"文件不存在: {file_path}")

    language = detect_language(file_path)
    root = project_root or os.path.dirname(os.path.abspath(file_path))

    try:
        func_src = extract_function_source(file_path, func_name)
    except Exception as exc:  # noqa: BLE001 - 边界处统一兜底
        return ToolResult(ok=False, error=str(exc))

    fw = detect_ut_framework(root, language, override=override, reference_token_budget=ref_budget)
    meta = _FRAMEWORK_META.get(fw.name, _FRAMEWORK_META["pytest"])
    fence = meta["fence"]

    facts = _collect_code_facts(file_path, func_name, func_src, root, language)

    result = ToolResult(ok=False)
    src_hint = fw.reference_path or "（未找到现有测试，使用默认模板）"
    result.history.append(
        f"框架={fw.name}（{'已探测' if fw.detected else '默认'}），参照: {src_hint}"
    )
    if facts.header_include:
        result.history.append(f"已定位被测头文件: {facts.header_include}")
    if facts.collaborators:
        result.history.append("协作者符号: " + "、".join(facts.collaborators))
    last_error: Optional[str] = None

    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(func_src, func_name, fw, fence, last_error, facts)
        try:
            raw = client.generate(prompt, system=meta["system"])
        except RuntimeError as exc:   # 模型超时/网络错：当作本轮失败，重试而非崩溃
            last_error = str(exc)
            result.history.append(f"[第{i}轮] 模型调用失败，重试: {exc}")
            continue
        test_code = extract_code_block(raw)

        check = _validate(test_code, fw, func_src, cfg)
        if check.ok:
            result.ok = True
            result.output = test_code
            result.history.append(f"[第{i}轮] {fw.name} 校验通过 ✅")
            return result

        last_error = check.error
        result.history.append(f"[第{i}轮] {fw.name} 校验失败，回填错误重试")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
