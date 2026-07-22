# =============================================================================
# core/project_facts.py
# 用途：项目级「确定性事实」抽取 —— product_gen 的防臆造锚点。
#   与 ut_gen.py 里单函数级的 CodeFacts 同一思路，但作用域是【整个项目】：
#   弱模型在缺少真实上下文时，最常见的臆造是「编一个不存在的头文件/模块名」
#   「和项目里已有的函数/类同名却不知道」「用了和项目风格不一致的命名」。
#   这些都能用 AST/正则确定性地从源码里抽出来，不需要真的跑编译器。
#
# 无状态：只处理传入的 project_root，不缓存、不读全局状态。
# =============================================================================
from __future__ import annotations

import ast
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Set

from core.lang_utils import (
    extract_include_lines,
    extract_python_import_lines,
    extract_c_skeleton,
)

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", "build"}
_HEADER_EXT = {".h", ".hpp", ".hh", ".hxx", ".inl"}
_CPP_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++"} | _HEADER_EXT
_PY_EXT = {".py", ".pyi"}

# 项目扫描规模保护：避免在超大仓库（如 l1sw 级别）上无限遍历拖垫小模型场景的响应时间
_MAX_FILES_SCANNED = 4000
_MAX_FILE_BYTES = 512 * 1024  # 单文件超过 512KB 跳过（生成代码/骨架用不到这么大的文件）


@dataclass
class ProjectFacts:
    """项目级确定性事实。用于喂入 Prompt（防臆造）与喂入校验器（抓臆造）。"""

    language: str = "python"
    available_headers: Set[str] = field(default_factory=set)   # cpp: 项目内已存在的头文件 basename
    available_py_modules: Set[str] = field(default_factory=set)  # py: 项目内可导入的模块/包名
    existing_symbols: Set[str] = field(default_factory=set)    # 已存在的函数/类名（用于查重名）
    common_includes: List[str] = field(default_factory=list)   # 最常见的 #include / import（供参照）
    naming_style: str = ""                                      # 'snake_case' / 'camelCase' / 'PascalCase' / 'mixed'
    files_scanned: int = 0
    truncated: bool = False


def _walk_source_files(project_root: str, exts: Set[str]):
    count = 0
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            path = os.path.join(root, fn)
            try:
                if os.path.getsize(path) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= _MAX_FILES_SCANNED:
                return


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _classify_naming(names: List[str]) -> str:
    """按标识符命名风格投票，返回项目的主流风格。"""
    votes: Counter = Counter()
    for name in names:
        if not name or name.startswith("__"):
            continue
        if "_" in name and name.lower() == name:
            votes["snake_case"] += 1
        elif name[:1].isupper() and "_" not in name:
            votes["PascalCase"] += 1
        elif name[:1].islower() and "_" not in name and any(c.isupper() for c in name):
            votes["camelCase"] += 1
        else:
            votes["mixed"] += 1
    if not votes:
        return ""
    return votes.most_common(1)[0][0]


def _cpp_symbol_names(skeleton_lines: List[str]) -> List[str]:
    names: List[str] = []
    for line in skeleton_lines:
        m = re.search(r"([A-Za-z_]\w*)\s*\([^)]*\)\s*;?\s*$", line)
        if m:
            names.append(m.group(1))
    return names


def _collect_cpp_facts(project_root: str, facts: ProjectFacts) -> None:
    include_counter: Counter = Counter()
    symbol_names: List[str] = []

    for path in _walk_source_files(project_root, _CPP_EXT):
        facts.files_scanned += 1
        ext = os.path.splitext(path)[1].lower()
        if ext in _HEADER_EXT:
            facts.available_headers.add(os.path.basename(path))
        source = _read(path)
        if not source:
            continue
        for inc in extract_include_lines(source):
            include_counter[inc] += 1
        try:
            skeleton = extract_c_skeleton(source)
        except Exception:  # noqa: BLE001 - 单文件解析失败不影响整体统计
            skeleton = []
        symbol_names.extend(_cpp_symbol_names(skeleton))

    facts.existing_symbols.update(symbol_names)
    facts.common_includes = [inc for inc, _ in include_counter.most_common(15)]
    facts.naming_style = _classify_naming(symbol_names)
    facts.truncated = facts.files_scanned >= _MAX_FILES_SCANNED


def _collect_python_facts(project_root: str, facts: ProjectFacts) -> None:
    import_counter: Counter = Counter()
    symbol_names: List[str] = []

    for path in _walk_source_files(project_root, _PY_EXT):
        facts.files_scanned += 1
        rel = os.path.relpath(path, project_root)
        top = rel.split(os.sep)[0]
        module_name = os.path.splitext(top)[0]
        if module_name and not module_name.startswith("."):
            facts.available_py_modules.add(module_name)

        source = _read(path)
        if not source:
            continue
        for imp in extract_python_import_lines(source):
            import_counter[imp] += 1
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbol_names.append(node.name)

    facts.existing_symbols.update(symbol_names)
    facts.common_includes = [imp for imp, _ in import_counter.most_common(15)]
    facts.naming_style = _classify_naming(symbol_names)
    facts.truncated = facts.files_scanned >= _MAX_FILES_SCANNED


def collect_project_facts(project_root: str, language: str) -> ProjectFacts:
    """主入口：扫描 project_root，按语言分派抽取确定性事实。"""
    facts = ProjectFacts(language=language)
    if not os.path.isdir(project_root):
        return facts
    if language == "python":
        _collect_python_facts(project_root, facts)
    else:
        _collect_cpp_facts(project_root, facts)
    return facts
