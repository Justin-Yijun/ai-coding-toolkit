# =============================================================================
# core/learned_registers.py
# 用途：log_analyze 的「架构规则记忆」（长期 · 语义层）—— 寄存器地址→含义规则。
#
#   与短期记忆（本次 LogFacts）、长期案例记忆（core/log_memory.py 的具体历史实例）
#   都不同：这里记录的是人工确认后归纳出的、与某一次具体日志无关的通用规则
#   （某地址对应哪个寄存器/是什么含义），几乎不随时间过期，属于"系统架构长期
#   成立的规则"。
#
#   与外部 chip-manual-kit 的 knowledge.json 完全解耦：
#     - 只能通过 `log-confirm --register-note` 人工写入，不读取也不修改外部 kb 文件；
#     - kb_path 依然是可选项，log_facts.collect_log_facts 会把两者的结果合并使用。
#
#   存储位置固定为 `<source_root>/.ai_toolkit/learned_registers.json`，
#   schema 与 chip-manual-kit 的 knowledge.json 保持一致（`{"registers": [...]}`），
#   便于复用 core/log_facts.py 里现成的反查逻辑。
#
# 无状态：不缓存跨调用状态，每次按需读写文件；读写失败只降级返回空/False，不抛异常
#   （这是"加分项"而非必需路径，不应影响主流程）。
# =============================================================================
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

_MEMORY_DIR_NAME = ".ai_toolkit"
_FILE_NAME = "learned_registers.json"

_REGISTER_NOTE_RE = re.compile(r"^\s*(0[xX][0-9a-fA-F]+)\s*=\s*(\S+)\s*(.*)$")


def resolve_path(source_root: Optional[str]) -> Optional[str]:
    """架构规则文件跟随 source_root 存放；未提供 source_root 时返回 None（不启用）。"""
    if not source_root or not os.path.isdir(source_root):
        return None
    mem_dir = os.path.join(source_root, _MEMORY_DIR_NAME)
    try:
        os.makedirs(mem_dir, exist_ok=True)
    except OSError:
        return None
    return os.path.join(mem_dir, _FILE_NAME)


def load_registers(source_root: Optional[str]) -> List[Dict]:
    """读取本工具自己积累的寄存器规则列表（schema 同 chip-manual-kit 的 knowledge.json）。"""
    path = resolve_path(source_root)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("registers", []) if isinstance(data, dict) else []


def add_register_note(source_root: str, note: str) -> bool:
    """把 `0xADDR=名称 描述` 形式的人工确认注解追加/更新进 learned_registers.json。

    只在 `log-confirm --register-note` 时被调用，保证这个文件里的每一条都经过人工确认，
    不会被 analyze_log 的自动流程静默写入。
    """
    m = _REGISTER_NOTE_RE.match(note)
    if not m:
        return False
    address, name, desc = m.group(1), m.group(2), m.group(3).strip()

    path = resolve_path(source_root)
    if not path:
        return False

    data: Dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            data = {}

    registers: List[Dict] = [
        r for r in data.get("registers", []) if str(r.get("register_name", "")) != name
    ]
    registers.append({
        "register_name": name,
        "module": "learned",
        "address": address,
        "description": desc,
    })
    data["registers"] = registers

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True
