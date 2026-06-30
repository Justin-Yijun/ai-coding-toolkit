# =============================================================================
# tools/type_annotate.py
# 用途：类型注解生成 Skill。
#   输入：单个函数（或小段）源码。
#   流程（黄金法则，最多迭代 3 次）：
#       1) 让模型为函数补全类型注解（参数 + 返回值）。
#       2) 先用 ast.parse 做语法检查，再用 mypy --strict 做类型校验。
#       3) 任一失败，把错误塞回 Prompt 重生成。
#
# 无状态：只处理传入的 source 字符串片段。
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_client import LLMClient, load_config
from core.text_utils import extract_code_block
from core import validator

_SYSTEM = (
    "你是 Python 类型标注专家。为给定函数补全完整类型注解，"
    "保持逻辑不变，必要时补充 from typing import ...。"
    "只输出标注后的完整代码，用 ```python 围栏包裹，不要解释。"
)


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)


def _build_prompt(source: str, last_error: Optional[str]) -> str:
    parts = [
        "请为下面的函数补全严格的类型注解（需通过 mypy --strict）：",
        "```python",
        source,
        "```",
    ]
    if last_error:
        parts += [
            "上一版未通过校验，错误如下，请修正后重新输出完整代码：",
            "```text",
            last_error,
            "```",
        ]
    return "\n".join(parts)


def annotate_types(
    source: str,
    client: Optional[LLMClient] = None,
) -> ToolResult:
    """主入口：迭代为函数添加类型注解并通过 ast + mypy 校验。"""
    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))
    mypy_timeout = int(cfg.get("validator", {}).get("mypy_timeout", 60))

    result = ToolResult(ok=False)
    last_error: Optional[str] = None

    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(source, last_error)
        try:
            raw = client.generate(prompt, system=_SYSTEM)
        except RuntimeError as exc:   # 模型超时/网络错：当作本轮失败，重试而非崩溃
            last_error = str(exc)
            result.history.append(f"[第{i}轮] 模型调用失败，重试: {exc}")
            continue
        code = extract_code_block(raw)

        # 第一关：ast 语法检查
        syntax = validator.check_python_syntax(code)
        if not syntax.ok:
            last_error = syntax.error
            result.history.append(f"[第{i}轮] AST 语法错误: {syntax.error}")
            continue

        # 第二关：mypy 严格模式
        mypy_res = validator.run_mypy_strict(code, timeout=mypy_timeout)
        if mypy_res.ok:
            result.ok = True
            result.output = code
            result.history.append(f"[第{i}轮] ast + mypy --strict 通过 ✅")
            return result

        last_error = mypy_res.error
        result.history.append(f"[第{i}轮] mypy 失败，回填错误重试")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
