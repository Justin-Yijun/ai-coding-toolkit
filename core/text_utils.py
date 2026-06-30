# =============================================================================
# core/text_utils.py
# 用途：无状态文本处理工具集。
#   - extract_code_block：从 LLM 输出中剥离 ```python ... ``` 围栏，拿到纯代码。
#   - approx_token_count：粗略估算 token 数（按字符 / 4），用于上下文裁剪。
#
# 设计原则（无状态 Skill）：所有函数纯函数，只处理传入字符串，
#   不读全局变量、不依赖对话历史，便于在受限上下文环境中安全复用。
# =============================================================================
from __future__ import annotations

import re

# 匹配 ```lang\n...\n``` 代码围栏
_FENCE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```",
    re.DOTALL,
)


def extract_code_block(text: str) -> str:
    """从模型输出中提取第一个代码块；若无围栏则返回原文（去首尾空白）。

    弱模型经常会在代码前后夹杂解释文字，这里做确定性清洗，
    保证后续 AST / pytest 校验拿到的是纯代码。
    """
    if not text:
        return ""
    matches = _FENCE_RE.findall(text)
    if matches:
        # 取最长的代码块（通常是主体实现，避免误取示例片段）
        return max(matches, key=len).strip()
    return text.strip()


def approx_token_count(text: str) -> int:
    """粗略估算 token 数。

    经验值：英文/代码约 4 字符 ≈ 1 token。用于 product_gen 等场景
    在喂入小上下文窗口前做硬性裁剪，避免溢出导致模型崩溃。
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断文本（保留头部，丢弃尾部）。"""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n# ...（上下文已按 token 预算截断）...\n"
