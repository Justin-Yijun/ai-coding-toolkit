# =============================================================================
# tools/product_gen.py
# 用途：产品代码生成 Skill（应对「大型项目 + 小上下文窗口」的核心方案）。
#   输入：项目根目录 + 需求描述。
#   关键逻辑（缺省即「分而治之」）：
#       1) 骨架抽取：遍历项目源码（.py 与 C/C++ .c/.cc/.cpp/.h/.hpp 等），
#          「只抽取骨架」——导入/include、类名、方法/函数签名（丢弃实现体）。
#       2) 上下文构建：骨架放得下就直接用；放不下就「逐文件分析→记录→拼接」
#          （build_project_context 内部的 map-reduce），永不硬截断丢文件。
#       3) 项目级确定性事实（ProjectFacts）：已有头文件/模块名、已有符号名、
#          命名风格，注入 Prompt 压制臆造，同时喂给校验器做「抓幻觉」核对。
#       4) 让模型基于上下文生成符合项目风格的新代码（新类 / 工具函数）。
#       5) 语言感知校验：Python 走 ast.parse + import 可解析性；
#          C/C++ 走括号配平 + #include 存在性；再统一核对符号是否与项目重名；
#          失败塞回 Prompt 重试（最多 3 次）。
#
# 这样无论项目多大，喂给模型的永远是「极小的结构摘要」而非完整源码，
# 从根本上规避上下文溢出；即使没有可用的构建系统，也能靠确定性事实核对
# 拦住「编不存在的头文件/模块/重名符号」这类弱模型最常见的幻觉。
# 无状态：只依赖传入的 project_root/requirement，不缓存跨调用状态。
# =============================================================================
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_client import LLMClient, load_config
from core.text_utils import extract_code_block, approx_token_count
from core import validator
from core.project_facts import ProjectFacts, collect_project_facts
from tools.summarize_gen import build_project_context

_SYSTEM = (
    "你是资深软件架构师。基于给定的「项目结构骨架」生成新代码，"
    "保持与项目一致的语言、命名风格与导入/包含习惯。"
    "只输出新代码，用代码围栏包裹，不要解释。"
)

_SKELETON_TOKEN_BUDGET = 1500  # 上下文硬上限（token）
_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", "build"}
# 参与上下文构建的源码扩展名（不再局限于 .py）
_PY_EXT = {".py", ".pyi"}
_CPP_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"}
_SOURCE_EXT = _PY_EXT | _CPP_EXT


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)


def _detect_project_language(project_root: str) -> str:
    """按源码扩展名的多数票判定项目主语言：'python' / 'cpp'。"""
    counter: Counter = Counter()
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in _PY_EXT:
                counter["python"] += 1
            elif ext in _CPP_EXT:
                counter["cpp"] += 1
    if not counter:
        return "python"
    return counter.most_common(1)[0][0]


def _build_prompt(
    context: str,
    requirement: str,
    language: str,
    last_error: Optional[str],
    facts: Optional[ProjectFacts] = None,
) -> str:
    fence = "python" if language == "python" else "cpp"
    lang_label = "Python" if language == "python" else "C/C++"
    parts = [
        "下面是项目的结构上下文（仅含导入/类名/函数签名或其摘要，无实现体）：",
        f"```{fence}",
        context if context.strip() else "# （空项目，无可参考上下文）",
        "```",
        "",
        f"需求：{requirement}",
        f"请生成符合上述项目风格的新 {lang_label} 代码，用代码围栏包裹。",
    ]

    # 关键：注入「确定性事实」，压制头文件/模块名的臆造与重名符号
    if facts is not None:
        if facts.naming_style:
            parts += ["", f"【项目命名风格】{facts.naming_style}，新代码请保持一致。"]
        if language == "python" and facts.available_py_modules:
            sample = "、".join(sorted(facts.available_py_modules)[:30])
            parts += [
                "",
                f"【项目内可用模块】{sample}"
                + ("等" if len(facts.available_py_modules) > 30 else "")
                + "。import 项目内模块时只能使用这些名字，不要臆造不存在的模块。",
            ]
        if language == "cpp" and facts.available_headers:
            sample = "、".join(sorted(facts.available_headers)[:30])
            parts += [
                "",
                f"【项目内已有头文件】{sample}"
                + ("等" if len(facts.available_headers) > 30 else "")
                + "。#include 项目内头文件时只能使用这些文件名，不要臆造不存在的头文件。",
            ]
        if facts.common_includes:
            parts += [
                "",
                "项目里最常见的依赖（供参考，非强制）：",
                "\n".join(facts.common_includes[:10]),
            ]
        if facts.existing_symbols:
            sample = "、".join(sorted(facts.existing_symbols)[:40])
            parts += [
                "",
                f"【项目中已存在的函数/类名】{sample}"
                + ("等" if len(facts.existing_symbols) > 40 else "")
                + "。新代码请避免使用这些名字（会与已有定义冲突）。",
            ]

    if last_error:
        parts += [
            "",
            "上一版代码校验失败，错误如下，请修正后重新输出：",
            "```text",
            last_error,
            "```",
        ]
    return "\n".join(parts)


def _validate_generated(code: str, language: str, facts: ProjectFacts) -> validator.CheckResult:
    """语言感知校验 + 防臆造核对，任一环节失败即短路返回。"""
    # 1) 语法/结构：Python→AST；C/C++→括号配平兜底
    syntax = (
        validator.check_python_syntax(code)
        if language == "python"
        else validator.check_balanced(code)
    )
    if not syntax.ok:
        return syntax

    # 2) 防臆造：import/include 是否真实存在于项目中
    if language == "python":
        resolve = validator.check_python_imports_resolve(code, facts.available_py_modules)
    else:
        resolve = validator.check_cpp_includes_exist(code, facts.available_headers)
    if not resolve.ok:
        return resolve

    # 3) 防臆造：新定义的符号是否与项目里已有的重名
    return validator.check_no_symbol_redefinition(code, language, facts.existing_symbols)


def generate_product(
    project_root: str,
    requirement: str,
    client: Optional[LLMClient] = None,
) -> ToolResult:
    """主入口：扫描项目骨架+确定性事实 → 生成新产品代码 → 语言感知+防臆造校验迭代。"""
    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))

    if not os.path.isdir(project_root):
        return ToolResult(ok=False, error=f"项目目录不存在: {project_root}")

    language = _detect_project_language(project_root)
    result = ToolResult(ok=False)

    # 缺省「分而治之」：骨架放得下直接用；放不下则逐文件分析+记录+拼接
    context, condensed = build_project_context(
        project_root, client, _SKELETON_TOKEN_BUDGET, log=result.history.append
    )
    mode = "逐文件分析归并" if condensed else "完整骨架"
    result.history.append(
        f"上下文已构建（{mode}），约 {approx_token_count(context)} tokens"
        f"（预算 {_SKELETON_TOKEN_BUDGET}，主语言 {language}）"
    )

    # 项目级确定性事实：已有头文件/模块、已有符号名、命名风格（防臆造锚点）
    facts = collect_project_facts(project_root, language)
    result.history.append(
        f"项目事实已抽取：headers={len(facts.available_headers)} "
        f"modules={len(facts.available_py_modules)} symbols={len(facts.existing_symbols)} "
        f"naming={facts.naming_style or '未知'}"
        + ("（已达扫描上限，事实可能不完整）" if facts.truncated else "")
    )
    last_error: Optional[str] = None

    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(context, requirement, language, last_error, facts)
        try:
            raw = client.generate(prompt, system=_SYSTEM)
        except RuntimeError as exc:   # 模型超时/网络错：当作本轮失败，重试而非崩溃
            last_error = str(exc)
            result.history.append(f"[第{i}轮] 模型调用失败，重试: {exc}")
            continue
        code = extract_code_block(raw)

        check = _validate_generated(code, language, facts)
        if check.ok:
            result.ok = True
            result.output = code
            result.history.append(f"[第{i}轮] 新代码校验通过 ✅（语法 + 防臆造核对）")
            return result

        last_error = check.error
        result.history.append(f"[第{i}轮] 校验失败，回填重试: {check.error}")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
