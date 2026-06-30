# =============================================================================
# core/lang_utils.py
# 用途：多语言（Python / C / C++）的「片段抽取」基础设施。
#   - detect_language：按扩展名判定语言。
#   - extract_function_source：精确切出单个目标函数源码
#       · Python 走 ast；C/C++ 走「括号配对」启发式抽取。
#   - extract_skeleton：抽取文件骨架（导入/类/函数签名，丢弃实现体），
#       供 product_gen 的「目录代码总结」使用——不再局限于 .py。
#   - blank_comments_and_strings：把注释/字符串置空（保留长度与换行），
#       让括号配对扫描不被字符串里的括号干扰。
#
# 无状态：所有函数纯函数，只处理传入的路径/源码片段。
# =============================================================================
from __future__ import annotations

import ast
import os
import re
from typing import List

# 扩展名 -> 语言
_PY_EXT = {".py", ".pyi"}
_C_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"}

# C 家族中需要排除的「控制关键字」（它们后面也跟 (...) {...} 但不是函数定义）
_CTRL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "do", "else",
    "sizeof", "decltype", "static_assert", "throw",
}
_PAIR = {"(": ")", "{": "}", "[": "]"}


def detect_language(file_path: str) -> str:
    """根据扩展名返回 'python' | 'cpp' | 'unknown'。C 与 C++ 统一按 'cpp' 处理。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _PY_EXT:
        return "python"
    if ext in _C_EXT:
        return "cpp"
    return "unknown"


# -----------------------------------------------------------------------------
# 注释/字符串置空：保证后续括号配对扫描的可靠性
# -----------------------------------------------------------------------------
def blank_comments_and_strings(source: str) -> str:
    """返回与 source 等长的字符串，其中注释与字符串内容被空格替换，换行保留。

    用途：让 `{ } ( )` 配对扫描不被字符串/注释里的括号误导。
    支持 // 行注释、/* */ 块注释、"..." 字符串、'...' 字符常量。
    """
    out: List[str] = []
    i, n = 0, len(source)
    state = "code"
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                out.append("  "); i += 2; state = "line_comment"; continue
            if c == "/" and nxt == "*":
                out.append("  "); i += 2; state = "block_comment"; continue
            if c == '"':
                out.append('"'); i += 1; state = "string"; continue
            if c == "'":
                out.append("'"); i += 1; state = "char"; continue
            out.append(c); i += 1; continue
        if state == "line_comment":
            out.append("\n" if c == "\n" else " ")
            state = "code" if c == "\n" else state
            i += 1; continue
        if state == "block_comment":
            if c == "*" and nxt == "/":
                out.append("  "); i += 2; state = "code"; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "string":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == '"':
                out.append('"'); i += 1; state = "code"; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if state == "char":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == "'":
                out.append("'"); i += 1; state = "code"; continue
            out.append(" "); i += 1; continue
    return "".join(out)


def find_matching(code: str, open_index: int) -> int:
    """在已 blank 处理的 code 中，返回 open_index 处括号的配对闭合下标；找不到返回 -1。"""
    open_c = code[open_index]
    close_c = _PAIR[open_c]
    depth = 0
    for i in range(open_index, len(code)):
        if code[i] == open_c:
            depth += 1
        elif code[i] == close_c:
            depth -= 1
            if depth == 0:
                return i
    return -1


# -----------------------------------------------------------------------------
# 目标函数抽取（Python / C++）
# -----------------------------------------------------------------------------
def extract_function_source(file_path: str, func_name: str) -> str:
    """读取文件并切出单个目标函数源码，按语言自动分派。"""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    lang = detect_language(file_path)
    if lang == "python":
        return _extract_python_function(source, func_name)
    if lang == "cpp":
        return _extract_c_function(source, func_name)
    # 未知语言：退化为按括号配对尝试（多数类 C 语言可用）
    return _extract_c_function(source, func_name)


def _extract_python_function(source: str, func_name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            seg = ast.get_source_segment(source, node)
            if seg:
                return seg
    raise ValueError(f"在源码中找不到 Python 函数 {func_name!r}")


def _extract_c_function(source: str, func_name: str) -> str:
    """启发式抽取 C/C++ 函数定义：定位 `name(` → 配对 ) → 找到函数体 {} 并配对。"""
    code = blank_comments_and_strings(source)
    pat = re.compile(r"\b" + re.escape(func_name) + r"\s*\(")
    for m in pat.finditer(code):
        paren_open = code.index("(", m.end() - 1)
        paren_close = find_matching(code, paren_open)
        if paren_close < 0:
            continue
        # 跳过限定符（const/noexcept/override/-> ret 等），直到遇到 { 或 ;
        j = paren_close + 1
        while j < len(code) and code[j] not in "{;":
            j += 1
        if j >= len(code) or code[j] == ";":
            continue  # 只是声明，不是定义
        brace_close = find_matching(code, j)
        if brace_close < 0:
            continue
        # 起点：回退到上一处语句边界，以带上返回类型与限定符
        start = max(
            code.rfind(";", 0, m.start()),
            code.rfind("}", 0, m.start()),
            code.rfind("{", 0, m.start()),
        ) + 1
        return source[start:brace_close + 1].strip()
    raise ValueError(f"在源码中找不到 C/C++ 函数 {func_name!r}")


# -----------------------------------------------------------------------------
# 骨架抽取（供 product_gen 的目录代码总结使用）
# -----------------------------------------------------------------------------
def extract_skeleton(file_path: str, source: str) -> List[str]:
    """按语言抽取文件骨架行。Python 用 ast；C/C++ 用括号扫描。"""
    lang = detect_language(file_path)
    if lang == "python":
        return _extract_python_skeleton(source)
    if lang == "cpp":
        return extract_c_skeleton(source)
    return []


def _py_signature(node: ast.AST) -> str:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    args = [a.arg for a in node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)}): ..."


def _extract_python_skeleton(source: str) -> List[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(source, node)
            if seg:
                lines.append(seg)
        elif isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}:")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append("    " + _py_signature(item))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_py_signature(node))
    return lines


def _classify_header(header: str) -> str:
    """判定 `{` 之前的头部文本属于：'agg'(类/结构/命名空间) | 'func' | 'other'。"""
    if not header:
        return "other"
    has_call = "(" in header and ")" in header
    first = re.match(r"[\w~]+", header)
    first_word = first.group(0) if first else ""
    if has_call and first_word not in _CTRL_KEYWORDS:
        # 形如 `Ret Class::method(args) const` → 函数
        return "func"
    if re.search(r"\b(class|struct|namespace|union|enum)\b", header):
        return "agg"
    return "other"


def extract_c_skeleton(source: str) -> List[str]:
    """C/C++ 骨架：保留 #include/#define、类/命名空间名、函数签名（去实现体）。"""
    code = blank_comments_and_strings(source)
    out: List[str] = []

    # 1) 预处理指令（原文读取，保留 include/define/guard）
    for line in source.splitlines():
        s = line.strip()
        if s.startswith("#include"):
            out.append(s)

    # 2) 括号扫描，抽取聚合体头与函数签名
    i, n = 0, len(code)
    boundary = 0
    ctx: List[str] = []  # 'agg' 上下文栈，用于缩进与闭合
    while i < n:
        c = code[i]
        if c not in ";{}":
            i += 1
            continue
        # 头部文本取自「已置空注释/字符串」的 code，避免版权注释等泄漏进骨架；
        # 再去掉预处理指令行与前导访问限定符，避免重复/噪声。
        raw_header = code[boundary:i]
        header_lines = [ln for ln in raw_header.splitlines() if not ln.lstrip().startswith("#")]
        header = re.sub(r"\s+", " ", " ".join(header_lines).strip())
        header = re.sub(r"^(public|private|protected)\s*:\s*", "", header)
        # 构造函数成员初始化列表（) : a(), b()）只保留签名，丢弃冗长的初始化串
        header = re.sub(r"\)\s*:\s.*$", ")", header)
        indent = "    " * len(ctx)
        if c == "{":
            kind = _classify_header(header)
            if kind == "agg":
                out.append(f"{indent}{header} {{")
                ctx.append("agg")
                boundary = i + 1
                i += 1
                continue
            close = find_matching(code, i)
            if close < 0:
                break
            if kind == "func":
                out.append(f"{indent}{header};")   # 仅签名，丢弃函数体
            boundary = close + 1
            i = close + 1
            continue
        if c == "}":
            if ctx and ctx[-1] == "agg":
                ctx.pop()
                out.append("    " * len(ctx) + "};")
            boundary = i + 1
            i += 1
            continue
        # c == ';'
        if "(" in header and ")" in header and _classify_header(header) == "func":
            out.append(f"{indent}{header};")       # 函数原型/声明
        boundary = i + 1
        i += 1
    return [ln for ln in out if ln.strip()]


# -----------------------------------------------------------------------------
# 深度模式：抽取「含 CUDA 标记的函数实现体」（保留 body，供细节分析）
# -----------------------------------------------------------------------------
# 默认 CUDA 标记：出现其一即认为该函数块含有值得分析的 CUDA 技巧。
_DEFAULT_CUDA_MARKERS = (
    "__global__", "__device__", "__host__", "__shared__", "__constant__",
    "__restrict__", "__launch_bounds__", "__forceinline__",
    "__syncthreads", "__syncwarp", "__threadfence",
    "__shfl", "__ballot", "__any", "__all", "__popc", "__ldg",
    "atomicAdd", "atomicCAS", "atomicExch", "atomicMax", "atomicMin", "atomicOr",
    "threadIdx", "blockIdx", "blockDim", "gridDim", "warpSize", "laneId",
    "cudaMemcpy", "cudaMemcpyAsync", "cudaMalloc", "cudaFree", "cudaStream",
    "cudaMemset", "cudaEvent", "cooperative_groups", "tex2D", "surf2D",
    "<<<", "cg::", "wmma", "mma_sync", "cub::", "thrust::",
)


def extract_cuda_excerpts(source: str, markers=None) -> List[str]:
    """抽取所有「含 CUDA 标记」的函数定义完整源码（含实现体）。

    返回原文片段列表；无匹配时返回空列表（调用方可回退到骨架）。
    标记检测在「置空注释/字符串」后的代码上进行，避免被注释/字符串误命中。
    """
    mk = tuple(markers) if markers else _DEFAULT_CUDA_MARKERS
    code = blank_comments_and_strings(source)
    n = len(code)

    def _has_marker(seg: str) -> bool:
        return any(m in seg for m in mk)

    excerpts: List[str] = []
    i, boundary = 0, 0
    depth_container = 0  # 进入 class/namespace/extern "C" 等容器的层数
    while i < n:
        c = code[i]
        if c not in ";{}":
            i += 1
            continue
        raw_header = code[boundary:i]
        header_lines = [ln for ln in raw_header.splitlines() if not ln.lstrip().startswith("#")]
        header = re.sub(r"\s+", " ", " ".join(header_lines).strip())
        if c == "{":
            kind = _classify_header(header)
            is_container = (
                kind == "agg" or header == "" or "extern" in header.split("(")[0]
            )
            if kind == "func":
                close = find_matching(code, i)
                if close < 0:
                    break
                # 头部真实起点：跳过 boundary 后的前导空白
                start = boundary + (len(raw_header) - len(raw_header.lstrip()))
                if _has_marker(code[start:close + 1]):
                    excerpts.append(source[start:close + 1].strip())
                boundary = i = close + 1
                continue
            if is_container:
                depth_container += 1
                boundary = i + 1
                i += 1
                continue
            # 其它 `{...}`（如初始化列表/裸作用域）：跳过整块，不递归
            close = find_matching(code, i)
            if close < 0:
                break
            boundary = i = close + 1
            continue
        # c in ';' or '}'
        if c == "}" and depth_container > 0:
            depth_container -= 1
        boundary = i + 1
        i += 1
    return excerpts
