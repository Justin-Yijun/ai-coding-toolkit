# =============================================================================
# tools/log_analyze.py
# 用途：日志问题分析 Skill —— 小上下文/弱模型场景下的"防幻觉"日志诊断。
#   输入：日志文本或日志文件 + 可选源码根目录 + 可选 chip-manual-kit 知识库。
#
#   黄金法则的日志版本：
#       1) 确定性抽取（core/log_facts.py）：关键行摘录、十六进制 token、
#          （可选）按地址/名字反查芯片手册里真实存在的寄存器、
#          （可选）解析出的 file:line 定位到本地源码并摘取真实上下文。
#          —— 模型完全不看原始日志全文，只看这些"已核实的事实"。
#       2) 让模型基于事实推理可能原因/排查建议，system 提示词明确禁止引用
#          事实之外的具体符号。
#       3) grounded 校验（core/validator.check_grounded_references）：
#          扫描回答里的十六进制值与 file:line，任一不在事实集合内即判定
#          臆造，报错回填重试（最多 3 次）。
#
# 无状态：只依赖传入的日志/路径参数，不缓存跨调用状态。
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_client import LLMClient, load_config
from core.text_utils import approx_token_count
from core import validator
from core.log_facts import LogFacts, collect_log_facts

_SYSTEM = (
    "你是资深嵌入式系统/芯片驱动日志分析专家。你只能基于【已提供的事实】进行推理，"
    "严禁提及事实之外的十六进制地址/数值、文件名与行号——事实里没出现的，"
    "宁可说“信息不足，需要进一步排查 XXX”，也不要编造看起来合理的细节。"
    "输出结构：① 最可能的原因（按可能性排序） ② 支撑该判断的具体事实引用 "
    "③ 建议的下一步排查动作。"
)

_DEFAULT_QUESTION = "分析这段日志，给出最可能的问题原因与排查建议。"


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)
    facts: Optional[LogFacts] = None


def _format_facts(facts: LogFacts) -> str:
    parts = [
        "【日志关键片段摘录】（行号为日志内原始行号）",
        facts.excerpt if facts.excerpt.strip() else "（未摘录到内容）",
    ]

    if facts.hex_tokens:
        parts += ["", "【日志中出现的十六进制值】", "、".join(facts.hex_tokens[:30])]

    if facts.register_hits:
        parts += ["", "【反查到手册中真实存在的寄存器】（来自 chip-manual-kit 知识库）"]
        for h in facts.register_hits[:15]:
            desc = f" — {h.description}" if h.description else ""
            parts.append(f"  {h.register_name} [{h.module}] addr={h.address} 命中token={h.token}{desc}")

    if facts.source_locations:
        parts += ["", "【解析出的源码位置】"]
        for loc in facts.source_locations[:10]:
            if loc.resolved_path and loc.context:
                parts.append(f"  {loc.file}:{loc.line} → 已定位到 {loc.resolved_path}")
                parts.append("  真实源码上下文：")
                parts.append(loc.context)
            elif loc.ambiguous:
                parts.append(f"  {loc.file}:{loc.line} → 本地存在多个同名文件，无法确定具体是哪个，未展开上下文")
            else:
                parts.append(f"  {loc.file}:{loc.line} → 未在给定源码根目录下找到该文件")

    if facts.truncated:
        parts += [
            "",
            "（注意：日志文件过大，以上只基于文件尾部片段分析，更早的信息未纳入；"
            "但摘录里标注的行号已经换算成原始文件里的真实行号，可以直接据此定位）",
        ]

    return "\n".join(parts)


def _build_prompt(facts: LogFacts, question: str, last_error: Optional[str]) -> str:
    parts = [
        _format_facts(facts),
        "",
        f"问题：{question}",
        "请只依据以上事实作答；提到十六进制值/文件名/行号时必须与上面出现的完全一致。",
    ]
    if last_error:
        parts += [
            "",
            "上一版回答校验失败（引用了事实之外的内容），请修正后重新输出：",
            "```text",
            last_error,
            "```",
        ]
    return "\n".join(parts)


def _grounded_check(answer: str, facts: LogFacts) -> validator.CheckResult:
    allowed_hex = set(facts.hex_tokens) | {h.token for h in facts.register_hits}
    allowed_files = {
        loc.file.replace("\\", "/").rsplit("/", 1)[-1] for loc in facts.source_locations
    }
    return validator.check_grounded_references(
        answer,
        allowed_hex=allowed_hex if facts.hex_tokens else None,
        allowed_files=allowed_files if facts.source_locations else None,
    )


def analyze_log(
    log_text: str = "",
    log_file: Optional[str] = None,
    question: str = _DEFAULT_QUESTION,
    kb_path: Optional[str] = None,
    source_root: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> ToolResult:
    """主入口：抽取日志事实 → 模型推理 → grounded 校验迭代。"""
    if not log_text and not log_file:
        return ToolResult(ok=False, error="必须提供 log_text 或 log_file 其中之一")

    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))

    facts = collect_log_facts(
        log_text=log_text, log_file=log_file, kb_path=kb_path, source_root=source_root
    )
    result = ToolResult(ok=False, facts=facts)
    result.history.append(
        f"事实已抽取：扫描 {facts.total_lines_scanned} 行"
        + (f"（已截断，仅尾部；跳过原文件前 {facts.line_offset} 行，行号已按原文件换算）" if facts.truncated else "")
        + f"，十六进制token={len(facts.hex_tokens)}，"
        f"寄存器命中={len(facts.register_hits)}，源码位置={len(facts.source_locations)}"
    )
    result.history.append(f"事实摘录约 {approx_token_count(facts.excerpt)} tokens（喂给模型的唯一素材）")

    last_error: Optional[str] = None
    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(facts, question, last_error)
        try:
            raw = client.generate(prompt, system=_SYSTEM)
        except RuntimeError as exc:  # 模型超时/网络错：当作本轮失败，重试而非崩溃
            last_error = str(exc)
            result.history.append(f"[第{i}轮] 模型调用失败，重试: {exc}")
            continue

        answer = raw.strip()
        check = _grounded_check(answer, facts)
        if check.ok:
            result.ok = True
            result.output = answer
            result.history.append(f"[第{i}轮] grounded 校验通过 ✅（未发现事实外引用）")
            return result

        last_error = check.error
        result.history.append(f"[第{i}轮] grounded 校验失败，回填重试: {check.error}")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
