# =============================================================================
# core/log_memory.py
# 用途：log_analyze 的「案例记忆」（长期 · 案例层 / Episodic Memory）。
#
#   记忆分层（详见 tools/log_analyze.py 顶部注释）：
#     - 短期记忆（当前 Step 的观察值）= 本次调用产出的 core.log_facts.LogFacts，
#       只在本次 analyze_log 调用内存在，不落盘、不跨调用共享。
#     - 长期记忆分两层：本模块负责【案例记忆】——记录"某次具体日志的问题与结论"，
#       本质是具体实例，存在过期风险（代码/固件变了可能不再适用）；
#       架构规则记忆（寄存器地址→含义等通用规则）见 core/learned_registers.py。
#
#   签名/相似度设计：完全基于 LogFacts 里已有的确定性字段（hex_tokens、
#   source_locations、stack_traces、excerpt 里出现的高危词类型），不引入新的
#   NLP/向量检索——保持与项目"黄金法则"一致的可解释性：能清楚说出"为什么判定
#   这两次日志相似"。
#
#   可信度机制：
#     - grounded 校验通过即自动弱入库（confirmed=False），仅作为未来分析的参考，
#       默认不注入 prompt（见 config.yaml 的 log_analyze.inject_weak_cases）。
#     - 人工确认后（`log-confirm`）才升级为 confirmed=True，之后命中高相似度可以
#       直接短路复用、跳过 LLM 调用。
#
# 记忆文件跟随 source_root 存放在 `<source_root>/.ai_toolkit/log_cases.jsonl`；
# 未提供 source_root 时该次调用不启用记忆（向后兼容，无副作用）。
# 读写失败只降级返回空/None，不抛异常——记忆是加分项，不能拖垮主流程。
# =============================================================================
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Set, Tuple

from core.log_facts import LogFacts

_MEMORY_DIR_NAME = ".ai_toolkit"
_CASES_FILE_NAME = "log_cases.jsonl"

# 用于从 excerpt 里粗略识别"这次事件的严重程度类型"，作为签名的一部分
# （不引入 NLP，只是复用 log_facts._HIGH_RE 涉及的关键词字面量）。
_HIGH_KEYWORDS = ("ERROR", "FATAL", "CRITICAL", "PANIC", "ASSERT", "EXCEPTION", "SEGFAULT", "ABORT")


@dataclass
class LogCase:
    """一条已分析过的具体日志案例（长期记忆 · 案例层）。"""
    case_id: str
    hex_tokens: List[str] = field(default_factory=list)
    file_locations: List[str] = field(default_factory=list)   # "basename:line"
    severity_kinds: List[str] = field(default_factory=list)
    question: str = ""
    answer: str = ""
    confirmed: bool = False
    created_at: float = 0.0
    iterations: int = 0

    def signature_set(self) -> Set[str]:
        return set(self.hex_tokens) | set(self.file_locations) | set(self.severity_kinds)


def _severity_kinds_from_excerpt(excerpt: str) -> List[str]:
    upper = excerpt.upper()
    return sorted({kw for kw in _HIGH_KEYWORDS if kw in upper})


def build_signature_from_facts(facts: LogFacts) -> Tuple[List[str], List[str], List[str]]:
    """从 LogFacts 抽取用于案例记忆的确定性签名字段（hex/file:line/严重程度类型）。

    file_locations 同时纳入 source_locations 与 stack_traces 里的帧（Phase 0 新增的
    结构化调用栈），因为调用栈的帧本质上也是"这次问题涉及的源码位置"。
    """
    hex_tokens = list(dict.fromkeys(facts.hex_tokens))

    file_locations: List[str] = []
    for loc in facts.source_locations:
        base = loc.file.replace("\\", "/").rsplit("/", 1)[-1]
        file_locations.append(f"{base}:{loc.line}")
    for st in facts.stack_traces:
        for fr in st.frames:
            if fr.file:
                base = fr.file.replace("\\", "/").rsplit("/", 1)[-1]
                file_locations.append(f"{base}:{fr.line}")
    file_locations = list(dict.fromkeys(file_locations))

    severity_kinds = _severity_kinds_from_excerpt(facts.excerpt)
    return hex_tokens, file_locations, severity_kinds


def resolve_memory_path(source_root: Optional[str]) -> Optional[str]:
    """记忆库文件路径跟随 source_root；未提供/不存在时返回 None（本次调用不启用记忆）。"""
    if not source_root or not os.path.isdir(source_root):
        return None
    mem_dir = os.path.join(source_root, _MEMORY_DIR_NAME)
    try:
        os.makedirs(mem_dir, exist_ok=True)
    except OSError:
        return None
    return os.path.join(mem_dir, _CASES_FILE_NAME)


def load_cases(path: Optional[str]) -> List[LogCase]:
    if not path or not os.path.isfile(path):
        return []
    cases: List[LogCase] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cases.append(LogCase(**data))
                except (json.JSONDecodeError, TypeError):
                    continue  # 单行损坏不影响其余案例
    except OSError:
        return []
    return cases


def append_case(path: Optional[str], case: LogCase) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")
    except OSError:
        return None
    return case.case_id


def new_case(
    facts: LogFacts, question: str, answer: str, iterations: int, confirmed: bool = False
) -> LogCase:
    hex_tokens, file_locations, severity_kinds = build_signature_from_facts(facts)
    return LogCase(
        case_id=uuid.uuid4().hex[:12],
        hex_tokens=hex_tokens,
        file_locations=file_locations,
        severity_kinds=severity_kinds,
        question=question,
        answer=answer,
        confirmed=confirmed,
        created_at=time.time(),
        iterations=iterations,
    )


def _jaccard(a: Set[str], b: Set[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def find_similar(
    cases: List[LogCase],
    facts: LogFacts,
    weak_threshold: float = 0.4,
) -> List[Tuple[LogCase, float]]:
    """按 Jaccard 相似度对历史案例排序，只返回 >= weak_threshold 的（强弱由调用方
    结合 case.confirmed 和 strong_threshold 再筛一次）。"""
    hex_tokens, file_locations, severity_kinds = build_signature_from_facts(facts)
    current = set(hex_tokens) | set(file_locations) | set(severity_kinds)
    if not current:
        return []
    scored: List[Tuple[LogCase, float]] = []
    for case in cases:
        score = _jaccard(current, case.signature_set())
        if score >= weak_threshold:
            scored.append((case, score))
    scored.sort(key=lambda cs: cs[1], reverse=True)
    return scored


def confirm_case(path: Optional[str], case_id: str) -> bool:
    """把指定 case 标为 confirmed=True（人工确认过，之后可被高相似度短路复用）。"""
    if not path or not os.path.isfile(path):
        return False
    cases = load_cases(path)
    found = False
    for case in cases:
        if case.case_id == case_id:
            case.confirmed = True
            found = True
    if not found:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            for case in cases:
                f.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True
