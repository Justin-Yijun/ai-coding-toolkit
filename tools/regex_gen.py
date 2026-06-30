# =============================================================================
# tools/regex_gen.py
# 用途：正则表达式生成 Skill。
#   输入：自然语言描述 + 正例列表 + 反例列表。
#   流程（黄金法则，最多迭代 3 次）：
#       1) 让模型生成一个正则。
#       2) 用 re.compile 验证可编译，再用 re.search 断言「全中正例、全避反例」。
#       3) 不满足则把失败样例塞回 Prompt 重生成。
#
# 无状态：只依赖本次传入的 description/positives/negatives。
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_client import LLMClient, load_config
from core.text_utils import extract_code_block
from core import validator

_SYSTEM = (
    "你是正则表达式专家。只输出一行 Python 正则表达式字符串本体（不含引号、不加解释），"
    "可用 ```text 围栏包裹。"
)


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)


def _clean_pattern(raw: str) -> str:
    """从模型输出中清洗出纯正则：去围栏、去引号、取第一非空行。"""
    text = extract_code_block(raw).strip()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉可能包裹的引号
        if len(line) >= 2 and line[0] in "\"'" and line[-1] == line[0]:
            line = line[1:-1]
        return line
    return text


def _build_prompt(
    description: str,
    positives: List[str],
    negatives: List[str],
    last_error: Optional[str],
) -> str:
    parts = [
        f"需求：{description}",
        f"必须匹配（正例）：{positives}",
        f"必须不匹配（反例）：{negatives}",
        "请给出满足上述全部约束的 Python 正则表达式。",
    ]
    if last_error:
        parts += [
            "上一版正则未通过断言，失败详情：",
            last_error,
            "请修正后重新输出正则。",
        ]
    return "\n".join(parts)


def generate_regex(
    description: str,
    positives: List[str],
    negatives: List[str],
    client: Optional[LLMClient] = None,
) -> ToolResult:
    """主入口：迭代生成并断言验证正则表达式。"""
    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))

    result = ToolResult(ok=False)
    last_error: Optional[str] = None

    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(description, positives, negatives, last_error)
        try:
            raw = client.generate(prompt, system=_SYSTEM)
        except RuntimeError as exc:   # 模型超时/网络错：当作本轮失败，重试而非崩溃
            last_error = str(exc)
            result.history.append(f"[第{i}轮] 模型调用失败，重试: {exc}")
            continue
        pattern = _clean_pattern(raw)

        check = validator.check_regex_matches(pattern, positives, negatives)
        if check.ok:
            result.ok = True
            result.output = pattern
            result.history.append(f"[第{i}轮] 正则通过全部断言 ✅: {pattern}")
            return result

        last_error = check.error
        result.history.append(f"[第{i}轮] 正则 {pattern!r} 断言失败，回填重试")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
