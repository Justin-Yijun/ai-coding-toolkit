# =============================================================================
# core/ut_framework.py
# 用途：探测项目「现有单测框架」，并抽取一段现有测试作为风格参照。
#   支持：pytest（Python）、googletest（C++）、cpputest（C/C++）。
#
# 设计动机：大多数情况下，新单测必须沿用项目里已有的 UT 框架与写法。
#   因此本模块扫描目录里的测试文件，按「头文件 include + 测试宏」打分，
#   选出占比最高的框架，并截取一份现有测试作为 few-shot 参照喂给模型。
#
# 无状态：只依赖传入的 project_root 与目标语言。
# =============================================================================
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.text_utils import truncate_to_tokens

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", "build"}
_C_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx"}

# 各框架的强/弱特征（强特征命中分高，用于消歧 googletest 与 cpputest）
_SIGNATURES = {
    "googletest": {
        "language": "cpp",
        "validate": "cpp_structure",
        "strong": [r'#\s*include\s*[<"][^">]*gtest/gtest\.h'],
        "weak": [r"\bTEST_F\s*\(", r"\bTEST_P\s*\(", r"\bEXPECT_[A-Z]+\s*\(",
                 r"\bASSERT_[A-Z]+\s*\(", r"::testing::"],
    },
    "cpputest": {
        "language": "cpp",
        "validate": "cpp_structure",
        "strong": [r'#\s*include\s*[<"][^">]*CppUTest'],
        "weak": [r"\bTEST_GROUP\s*\(", r"\bCHECK_EQUAL\s*\(", r"\bLONGS_EQUAL\s*\(",
                 r"\bSTRCMP_EQUAL\s*\(", r"\bmock\s*\(\s*\)"],
    },
    "pytest": {
        "language": "python",
        "validate": "pytest",
        "strong": [r"^\s*import\s+pytest", r"^\s*from\s+pytest\b"],
        "weak": [r"^\s*def\s+test_\w+\s*\(", r"@pytest\.", r"\bassert\s+"],
    },
}


@dataclass
class UTFramework:
    name: str                       # pytest | googletest | cpputest
    language: str                   # python | cpp
    validate: str                   # pytest | cpp_structure
    detected: bool                  # True=从项目探测到；False=按语言默认
    reference_path: Optional[str]   # 参照测试文件路径
    reference_snippet: str          # 截断后的参照片段


def _iter_source_files(root: str) -> List[str]:
    files: List[str] = []
    for cur, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext == ".py" or ext in _C_EXT:
                files.append(os.path.join(cur, name))
    return files


def _score_text(text: str, name: str) -> Dict[str, int]:
    """对单个文件内容给三种框架打分。"""
    scores: Dict[str, int] = {}
    flags = re.MULTILINE
    for fw, sig in _SIGNATURES.items():
        s = 0
        for pat in sig["strong"]:
            if re.search(pat, text, flags):
                s += 5
        for pat in sig["weak"]:
            s += len(re.findall(pat, text, flags))
        # 文件名是测试文件的额外加权（如 test_*.py / *_test.cpp / *Test.cpp）
        if fw == "pytest" and (name.startswith("test_") or name.endswith("_test.py")):
            s += 2
        if sig["language"] == "cpp" and re.search(r"(test|Test)", name):
            s += 1
        if s:
            scores[fw] = s
    return scores


def detect_ut_framework(
    project_root: str,
    target_language: str,
    override: Optional[str] = None,
    reference_token_budget: int = 800,
) -> UTFramework:
    """探测项目主用 UT 框架。override 非空则强制使用该框架。"""
    totals: Dict[str, int] = {}
    best_file: Dict[str, Tuple[int, str]] = {}  # fw -> (score, path)

    for path in _iter_source_files(project_root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for fw, s in _score_text(text, os.path.basename(path)).items():
            totals[fw] = totals.get(fw, 0) + s
            if s > best_file.get(fw, (0, ""))[0]:
                best_file[fw] = (s, path)

    chosen: Optional[str] = None
    if override:
        chosen = override.lower()
    elif totals:
        # 优先选与目标语言一致的最高分框架，避免给 C++ 文件配 pytest
        candidates = [(fw, sc) for fw, sc in totals.items()
                      if _SIGNATURES[fw]["language"] == target_language] or list(totals.items())
        chosen = max(candidates, key=lambda kv: kv[1])[0]

    detected = chosen in totals if chosen else False
    if not chosen:
        # 兜底默认：按目标语言选常见框架
        chosen = "pytest" if target_language == "python" else "googletest"

    sig = _SIGNATURES.get(chosen, _SIGNATURES["googletest"])
    ref_path, ref_snippet = "", ""
    if chosen in best_file:
        ref_path = best_file[chosen][1]
        try:
            with open(ref_path, "r", encoding="utf-8", errors="replace") as f:
                ref_snippet = truncate_to_tokens(f.read(), reference_token_budget)
        except OSError:
            ref_snippet = ""

    return UTFramework(
        name=chosen,
        language=sig["language"],
        validate=sig["validate"],
        detected=detected,
        reference_path=ref_path or None,
        reference_snippet=ref_snippet,
    )
