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
from core import log_memory
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
    case_id: Optional[str] = None
    reused_case_id: Optional[str] = None


def _format_facts(facts: LogFacts) -> str:
    parts = [
        "【日志关键片段摘录】（行号为日志内原始行号）",
        facts.excerpt if facts.excerpt.strip() else "（未摘录到内容）",
    ]

    if facts.error_groups:
        parts += ["", "【重复报错折叠】（同类报错已去重，只展示首末位置与次数）"]
        for g in facts.error_groups:
            parts.append(f"  第 {g.first_line}~{g.last_line} 行共出现 {g.count} 次: {g.sample_line}")

    if facts.stack_traces:
        parts += ["", "【解析出的调用栈】（#0 为最内层/崩溃点，按调用顺序展开）"]
        for st in facts.stack_traces:
            parts.append(f"  锚点行 {st.anchor_line}：")
            for fr in st.frames:
                loc = f" at {fr.file}:{fr.line}" if fr.file else ""
                parts.append(f"    #{fr.index} {fr.function}{loc}")

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


def _top_evidence_reminder(facts: LogFacts) -> str:
    """从第 2 轮起重新点名最高优先级证据，防止模型"只改措辞、不重新核对事实"
    （常见于小模型收到报错后倾向于表面改写而非重新审视证据）。"""
    lines: List[str] = []
    for g in facts.error_groups[:3]:
        lines.append(f"  第 {g.first_line}~{g.last_line} 行（共 {g.count} 次）: {g.sample_line}")
    for st in facts.stack_traces[:3]:
        inner = st.frames[0] if st.frames else None
        if inner:
            loc = f" at {inner.file}:{inner.line}" if inner.file else ""
            lines.append(f"  锚点行 {st.anchor_line} 的最内层帧: #{inner.index} {inner.function}{loc}")
    if not lines:
        return ""
    return "请重新核对以下最高优先级证据后再作答（不要只改措辞）：\n" + "\n".join(lines)


def _build_prompt(
    facts: LogFacts,
    question: str,
    last_error: Optional[str],
    memory_note: str = "",
    iteration: int = 1,
) -> str:
    parts = [
        _format_facts(facts),
        "",
        f"问题：{question}",
        "请只依据以上事实作答；提到十六进制值/文件名/行号时必须与上面出现的完全一致。",
    ]
    if memory_note:
        parts += ["", memory_note]
    if iteration >= 2:
        reminder = _top_evidence_reminder(facts)
        if reminder:
            parts += ["", reminder]
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
    check = validator.check_grounded_references(
        answer,
        allowed_hex=allowed_hex if facts.hex_tokens else None,
        allowed_files=allowed_files if facts.source_locations else None,
    )
    if not check.ok:
        return check
    # 更进一步的一致性校验：即使十六进制/file:line 都真实存在，也要防止
    # 模型点名一个"存在但无关"的行号来凑数（半臆造）。
    return validator.check_answer_cites_primary_evidence(answer, facts)


def analyze_log(
    log_text: str = "",
    log_file: Optional[str] = None,
    question: str = _DEFAULT_QUESTION,
    kb_path: Optional[str] = None,
    source_root: Optional[str] = None,
    client: Optional[LLMClient] = None,
    use_memory: bool = True,
) -> ToolResult:
    """主入口：抽取日志事实 → （可选）案例记忆检索 → 模型推理 → grounded 校验迭代。

    记忆层（时间+记忆换小模型等效能力，见 core/log_memory.py 顶部注释）：
        - 强命中（已人工确认 + 相似度达标）：直接复用历史结论，跳过 LLM 调用；
        - 弱命中（未确认）：默认不注入 prompt（config.log_analyze.inject_weak_cases
          控制），只作为运行记录；
        - 本轮 grounded 校验通过后，自动把这次结论存为未确认案例，供后续复用/参考。
    仅当提供了 source_root 时才启用记忆（无 source_root 时行为与原无状态版本一致）。
    """
    if not log_text and not log_file:
        return ToolResult(ok=False, error="必须提供 log_text 或 log_file 其中之一")

    cfg = load_config()
    client = client or LLMClient(cfg)
    max_iter = int(cfg.get("iteration", {}).get("max_iterations", 3))
    log_cfg = cfg.get("log_analyze", {}) or {}
    min_repeat_to_fold = int(log_cfg.get("min_repeat_to_fold", 3))
    weak_threshold = float(log_cfg.get("weak_similarity_threshold", 0.4))
    strong_threshold = float(log_cfg.get("strong_similarity_threshold", 0.75))
    inject_weak_cases = bool(log_cfg.get("inject_weak_cases", False))
    memory_enabled = use_memory and bool(log_cfg.get("enable_memory", True))

    facts = collect_log_facts(
        log_text=log_text,
        log_file=log_file,
        kb_path=kb_path,
        source_root=source_root,
        min_repeat_to_fold=min_repeat_to_fold,
    )
    result = ToolResult(ok=False, facts=facts)
    result.history.append(
        f"事实已抽取：扫描 {facts.total_lines_scanned} 行"
        + (f"（已截断，仅尾部；跳过原文件前 {facts.line_offset} 行，行号已按原文件换算）" if facts.truncated else "")
        + f"，十六进制token={len(facts.hex_tokens)}，"
        f"寄存器命中={len(facts.register_hits)}，源码位置={len(facts.source_locations)}，"
        f"重复报错折叠={len(facts.error_groups)}，解析出的调用栈={len(facts.stack_traces)}"
    )
    result.history.append(f"事实摘录约 {approx_token_count(facts.excerpt)} tokens（喂给模型的唯一素材）")

    memory_path = log_memory.resolve_memory_path(source_root) if memory_enabled else None
    similar_cases: List = []
    if memory_path:
        cases = log_memory.load_cases(memory_path)
        similar_cases = log_memory.find_similar(cases, facts, weak_threshold)
        if similar_cases:
            result.history.append(
                f"案例记忆：命中 {len(similar_cases)} 条历史相似案例（阈值≥{weak_threshold}）"
            )
            strong_match = next(
                (c for c, score in similar_cases if c.confirmed and score >= strong_threshold),
                None,
            )
            if strong_match is not None:
                result.ok = True
                result.iterations = 0
                result.reused_case_id = strong_match.case_id
                result.output = (
                    strong_match.answer
                    + f"\n\n（注：本回答直接复用历史已确认案例 {strong_match.case_id}，未调用模型）"
                )
                result.history.append(f"强命中已确认案例 {strong_match.case_id}，直接复用，跳过 LLM 调用")
                return result

    memory_note = ""
    if memory_enabled and inject_weak_cases and similar_cases:
        lines = ["【历史相似案例（未确认，仅供参考，不可当作事实引用）】"]
        for case, score in similar_cases[:3]:
            lines.append(f"  相似度{score:.2f}｜{case.question} → {case.answer[:200]}")
        memory_note = "\n".join(lines)

    last_error: Optional[str] = None
    for i in range(1, max_iter + 1):
        result.iterations = i
        prompt = _build_prompt(facts, question, last_error, memory_note, iteration=i)
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
            if memory_path:
                case = log_memory.new_case(facts, question, answer, iterations=i, confirmed=False)
                case_id = log_memory.append_case(memory_path, case)
                if case_id:
                    result.case_id = case_id
                    result.history.append(
                        f"已自动存入案例记忆（未确认）：case_id={case_id}，"
                        f"如确认结论正确可执行 `log-confirm --case-id {case_id}` 升级为可信案例"
                    )
            return result

        last_error = check.error
        result.history.append(f"[第{i}轮] grounded 校验失败，回填重试: {check.error}")

    result.error = f"达到最大迭代 {max_iter} 次仍未通过。最后错误:\n{last_error}"
    return result
