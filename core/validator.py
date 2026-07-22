# =============================================================================
# core/validator.py
# 用途：通用「确定性校验」工具层 —— 黄金法则中的「校验」环节。
#       不依赖大模型，纯靠 AST / 正则 / pytest / mypy 给出客观对错信号，
#       校验失败时返回的错误文本会被上层塞回 Prompt 驱动模型自我修复。
#
# 设计原则（无状态）：每个函数只处理传入的代码/数据片段，
#   自行管理临时文件与子进程，不残留全局状态。
# =============================================================================
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple


@dataclass
class CheckResult:
    """统一校验结果结构。ok=True 表示通过；error 为可读错误信息。"""
    ok: bool
    error: str = ""


# -----------------------------------------------------------------------------
# 1) 基于 ast.parse 的 Python 语法检查
# -----------------------------------------------------------------------------
def check_python_syntax(code: str) -> CheckResult:
    """用 ast.parse 校验 Python 语法是否合法。"""
    try:
        ast.parse(code)
        return CheckResult(ok=True)
    except SyntaxError as exc:
        msg = f"SyntaxError: {exc.msg} (line {exc.lineno}, offset {exc.offset})"
        return CheckResult(ok=False, error=msg)


# -----------------------------------------------------------------------------
# 2) 基于 re.compile 的正则校验
# -----------------------------------------------------------------------------
def check_regex_compiles(pattern: str) -> CheckResult:
    """校验正则能否被 re.compile 成功编译。"""
    try:
        re.compile(pattern)
        return CheckResult(ok=True)
    except re.error as exc:
        return CheckResult(ok=False, error=f"无效正则: {exc}")


def check_regex_matches(
    pattern: str,
    positives: List[str],
    negatives: List[str],
) -> CheckResult:
    """断言正则：必须命中全部正例，且不命中任何反例。"""
    compiled = check_regex_compiles(pattern)
    if not compiled.ok:
        return compiled

    rx = re.compile(pattern)
    errors: List[str] = []
    for s in positives:
        if not rx.search(s):
            errors.append(f"应匹配但未匹配的正例: {s!r}")
    for s in negatives:
        if rx.search(s):
            errors.append(f"不应匹配却匹配的反例: {s!r}")

    if errors:
        return CheckResult(ok=False, error="\n".join(errors))
    return CheckResult(ok=True)


# -----------------------------------------------------------------------------
# 3) 基于 subprocess 调用 pytest（含超时控制）
# -----------------------------------------------------------------------------
def run_pytest(
    test_code: str,
    extra_files: Optional[List[Tuple[str, str]]] = None,
    timeout: int = 60,
) -> CheckResult:
    """在隔离临时目录运行 pytest。

    参数:
        test_code:   测试文件内容（将写为 test_generated.py）。
        extra_files: 额外文件列表 [(filename, content)]，如被测源码模块。
        timeout:     子进程超时秒数。
    """
    if not _module_available("pytest"):
        return CheckResult(
            ok=False,
            error="未安装 pytest。请先运行: pip install pytest",
        )

    with tempfile.TemporaryDirectory() as tmp:
        for name, content in (extra_files or []):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(content)
        test_path = os.path.join(tmp, "test_generated.py")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", test_path],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(ok=False, error=f"pytest 执行超时（>{timeout}s）")

        if proc.returncode == 0:
            return CheckResult(ok=True)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        # 只回传尾部关键报错，节省珍贵的上下文 token
        return CheckResult(ok=False, error=_tail(output, 2000))


# -----------------------------------------------------------------------------
# 4) 基于 subprocess 调用 mypy 严格模式（含超时控制）
# -----------------------------------------------------------------------------
def run_mypy_strict(code: str, timeout: int = 60) -> CheckResult:
    """对代码运行 mypy --strict 类型检查。"""
    if not _module_available("mypy"):
        return CheckResult(
            ok=False,
            error="未安装 mypy。请先运行: pip install mypy",
        )

    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, "mod_generated.py")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "mypy", "--strict",
                 "--no-error-summary", "--no-color-output", src_path],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(ok=False, error=f"mypy 执行超时（>{timeout}s）")

        if proc.returncode == 0:
            return CheckResult(ok=True)
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return CheckResult(ok=False, error=_tail(output, 2000))


# -----------------------------------------------------------------------------
# 5) C/C++ 单测结构校验（离线无构建系统时的确定性校验闸门）
# -----------------------------------------------------------------------------
# 说明：内网纯 CPU、无统一构建/链接环境时，无法稳定编译运行 gtest/cpputest。
#       因此对 C++ 单测采用「结构性确定性校验」：括号配对 + 框架必备宏存在性。
#       若项目自带可用的 g++/构建脚本，可在此基础上扩展为真实编译校验。
_CPP_REQUIRED = {
    "googletest": {
        "macros": [r"\bTEST\s*\(", r"\bTEST_F\s*\(", r"\bTEST_P\s*\("],
        "asserts": [r"\bEXPECT_[A-Z]+\s*\(", r"\bASSERT_[A-Z]+\s*\("],
    },
    "cpputest": {
        "macros": [r"\bTEST\s*\(", r"\bTEST_GROUP\s*\("],
        "asserts": [r"\bCHECK[A-Z_]*\s*\(", r"\bLONGS_EQUAL\s*\(",
                    r"\bSTRCMP_EQUAL\s*\(", r"\bDOUBLES_EQUAL\s*\("],
    },
}


def check_balanced(code: str) -> CheckResult:
    """校验 ()[]{} 三类括号是否配对平衡（忽略注释/字符串内部）。"""
    from core.lang_utils import blank_comments_and_strings
    blanked = blank_comments_and_strings(code)
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set(pairs.values())
    stack: List[str] = []
    for ch in blanked:
        if ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return CheckResult(ok=False, error=f"括号不配对：多余的 '{ch}'")
            stack.pop()
    if stack:
        return CheckResult(ok=False, error=f"括号未闭合：缺少 '{stack[-1]}' 的配对")
    return CheckResult(ok=True)


def check_cpp_test_structure(code: str, framework: str) -> CheckResult:
    """对 googletest/cpputest 生成的测试做结构性校验。"""
    balanced = check_balanced(code)
    if not balanced.ok:
        return balanced

    req = _CPP_REQUIRED.get(framework.lower())
    if not req:
        return CheckResult(ok=True)  # 未知框架仅做括号校验

    if not any(re.search(p, code) for p in req["macros"]):
        return CheckResult(
            ok=False,
            error=f"未发现 {framework} 测试宏（如 {req['macros'][0]}）",
        )
    if not any(re.search(p, code) for p in req["asserts"]):
        return CheckResult(
            ok=False,
            error=f"未发现 {framework} 断言宏（测试缺少实际断言）",
        )
    return CheckResult(ok=True)


# -----------------------------------------------------------------------------
# 6) 防臆造校验：拿项目级确定性事实（core/project_facts.py）核对生成代码
#    —— 没有真实构建系统时，这是低成本但高价值的「抓幻觉」闸门：
#       弱模型最常见的错不是语法错，而是「编一个不存在的头文件/模块名」，
#       或者「悄悄和项目里已有的函数/类同名」。这两类错误确定性、零漏报地可查。
# -----------------------------------------------------------------------------
def check_python_imports_resolve(code: str, known_modules: Optional[Set[str]] = None) -> CheckResult:
    """校验 import 的顶层模块名是否可解析：标准库 / 已安装第三方 / 项目自身模块。

    捕获弱模型编造不存在模块名（如 import numpy_utils 但项目里根本没这个模块）。
    相对导入（from . import x）跳过，片段级无法判断项目内部相对路径是否有效。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return CheckResult(ok=False, error=f"SyntaxError: {exc.msg} (line {exc.lineno})")

    known = known_modules or set()
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    bad: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if not _py_module_resolvable(top, known, stdlib):
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对导入：项目内部路径，片段级无法核实
            top = (node.module or "").split(".")[0]
            if top and not _py_module_resolvable(top, known, stdlib):
                bad.append(node.module or "")

    if bad:
        names = "、".join(sorted(set(bad)))
        return CheckResult(ok=False, error=f"以下导入无法解析，疑似臆造的模块名: {names}")
    return CheckResult(ok=True)


def _py_module_resolvable(top: str, known: Set[str], stdlib) -> bool:
    if not top or top in known or top in stdlib:
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check_cpp_includes_exist(code: str, available_headers: Optional[Set[str]] = None) -> CheckResult:
    """校验 `#include "..."` 引用的项目内头文件是否真实存在。

    只查双引号（项目内）include；`<...>` 系统/第三方头不查
    （本机未必装有对应 SDK，误报代价远大于漏报）。
    available_headers 为空时（如项目本身没有任何 .h/.hpp）不做强校验，直接通过。
    """
    if not available_headers:
        return CheckResult(ok=True)
    bad: List[str] = []
    for m in re.finditer(r'#\s*include\s*"([^"]+)"', code):
        name = os.path.basename(m.group(1))
        if name not in available_headers:
            bad.append(m.group(1))
    if bad:
        names = "、".join(sorted(set(bad)))
        return CheckResult(ok=False, error=f'以下 #include 的头文件在项目中不存在，疑似臆造: {names}')
    return CheckResult(ok=True)


def check_no_symbol_redefinition(
    code: str,
    language: str,
    existing_symbols: Optional[Set[str]] = None,
) -> CheckResult:
    """校验新代码定义的函数/类名是否与项目中已存在的符号重名。

    弱模型常常不知道项目里已经有同名函数，生成的"新代码"其实是悄悄的重复定义，
    这类错误编译器/链接器最终会报，但离线小模型场景下越早拦截越省迭代次数。
    """
    existing = existing_symbols or set()
    if not existing:
        return CheckResult(ok=True)
    defined = _extract_defined_symbols(code, language)
    clashes = sorted(defined & existing)
    if clashes:
        names = "、".join(clashes)
        return CheckResult(ok=False, error=f"以下符号与项目中已存在的定义重名，请改名: {names}")
    return CheckResult(ok=True)


def _extract_defined_symbols(code: str, language: str) -> Set[str]:
    if language == "python":
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return set()
        return {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
    from core.lang_utils import extract_c_skeleton
    names: Set[str] = set()
    try:
        skeleton = extract_c_skeleton(code)
    except Exception:  # noqa: BLE001 - 抽取失败不阻塞，交由其它校验兜底
        skeleton = []
    for line in skeleton:
        m = re.search(r"([A-Za-z_]\w*)\s*\([^)]*\)\s*;?\s*$", line)
        if m:
            names.add(m.group(1))
    return names


# -----------------------------------------------------------------------------
# 7) grounded 校验：模型输出中的「具体引用」是否都出自给定事实集合
#    —— log_analyze 的核心闸门：小模型在日志分析里最容易犯的错不是语法错，
#       是编一个事实之外的十六进制值/文件:行号，看起来煞有介事但完全不存在。
# -----------------------------------------------------------------------------
def check_grounded_references(
    answer: str,
    allowed_hex: Optional[Set[str]] = None,
    allowed_files: Optional[Set[str]] = None,
) -> CheckResult:
    """校验回答里出现的十六进制值 / 文件:行号，是否都在允许集合内（大小写不敏感）。

    allowed_hex/allowed_files 为 None 表示不对该类做强校验（事实源本身为空时，
    强行要求"零引用"只会逼模型说废话，不如干脆跳过这类核对）。
    """
    errors: List[str] = []

    if allowed_hex is not None:
        allowed_lc = {h.lower() for h in allowed_hex}
        bad_hex = sorted({
            tok for tok in re.findall(r"\b0[xX][0-9a-fA-F]{2,}\b", answer)
            if tok.lower() not in allowed_lc
        })
        if bad_hex:
            errors.append("引用了事实之外的十六进制值（疑似臆造）: " + "、".join(bad_hex))

    if allowed_files is not None:
        allowed_lc = {f.lower() for f in allowed_files}
        bad_files = sorted({
            m.group(0)
            for m in re.finditer(r"\b([\w.\-]+\.(?:c|cc|cpp|cxx|h|hpp|hh|hxx)):(\d+)\b", answer)
            if m.group(1).lower() not in allowed_lc
        })
        if bad_files:
            errors.append("引用了事实之外的文件:行号（疑似臆造）: " + "、".join(bad_files))

    if errors:
        return CheckResult(ok=False, error="；".join(errors))
    return CheckResult(ok=True)


# -----------------------------------------------------------------------------
# 内部辅助
# -----------------------------------------------------------------------------
def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _tail(text: str, max_chars: int) -> str:
    """保留文本尾部（报错通常在末尾），控制回传体积。"""
    if len(text) <= max_chars:
        return text
    return "...（前文省略）...\n" + text[-max_chars:]
