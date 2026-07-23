# =============================================================================
# core/log_facts.py
# 用途：日志问题分析的「确定性事实」抽取层（log_analyze 的核心防幻觉机制）。
#
#   动机：一段真实的嵌入式/驱动日志往往几千到几十万行，塞给小上下文模型
#   既装不下，也会让模型在噪声里"自由发挥"编出看似合理但不存在的寄存器名、
#   地址、文件行号。因此严格遵守黄金法则的思路：先用确定性手段（正则/字符串
#   匹配/文件系统查找）把"事实"摘出来，模型只被允许在事实范围内做推理。
#
#   四类事实：
#     1) excerpt：围绕 ERROR/assert/panic 等关键词的上下文行摘录（去重合并窗口，
#        按严重程度分两级优先：高危词优先纳入，预算不够时才用中危词兜底）
#     2) hex_tokens：日志里出现的十六进制值（地址/寄存器值/错误码）
#     3) register_hits：若提供 chip-manual-kit 的 knowledge.json，
#        按地址/名字反查出【真实存在】的寄存器（不是模型编的）
#     4) source_locations：从日志里解析出的 file:line（含 glibc assert 格式），
#        若提供源码根目录，进一步定位到本地文件并摘取真实源码上下文
#
#   踩过的坑（来自真实电信/嵌入式日志的教训，务必留着）：
#     - 很多嵌入式日志用 3 字母缩写严重级别（如 "43/ERR:"、"F6/ERR"），
#       不是拼全 "ERROR"，纯 \bERROR\b 会完全漏掉真实错误行；
#     - "recvReq failures:0" 这类计数器即使数值是 0（代表【没有】失败）也会命中
#       朴素的 "fail" 关键词，必须排除「关键词后紧跟 :0 / =0」的零值噪声。
#
# 无状态：只处理传入的日志文本/文件路径，不缓存跨调用状态。
# =============================================================================
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from core import learned_registers

# 日志文件超大时只扫描"尾部"这么多字节（most-recent-first 的调试直觉：
# 现场捕获的日志，问题通常发生在末尾附近）。可通过参数覆盖。
_DEFAULT_MAX_SCAN_BYTES = 20 * 1024 * 1024  # 20MB

# 高危：基本等同"确认出问题"。既匹配完整单词（ERROR/FATAL/...），
# 也匹配嵌入式日志常见的 3 字母严重级别缩写（.../ERR、.../FAT）。
_HIGH_RE = re.compile(
    r"\b(ERROR|FATAL|CRITICAL|PANIC|ASSERT(?:ION)?|EXCEPTION|SEGFAULT|"
    r"SEGMENTATION\s+FAULT|CORE\s+DUMPED|ABORT(?:ED)?)\b|/(?:ERR|FAT)\b",
    re.IGNORECASE,
)
# 中危：可能相关，但噪声也更多（如常年打印的 "xxx failures:0" 计数器），
# 只在没有任何高危命中时才作为兜底摘录来源。
_MED_RE = re.compile(
    r"\b(FAIL(?:ED|URE)?S?|TRACEBACK|WARN(?:ING)?)\b|/WRN\b",
    re.IGNORECASE,
)
# 零值噪声：关键词紧跟 ":0" / "=0" / "=(0 0 0 0)" 这类【全零】的计数器/元组
# （如 "failures:0"、电信日志里常见的 "Error=(0 0 0 0 0 0 0 0)"），代表【没有】
# 发生，不该被当成"有问题"的证据——否则健康的周期性遥测行会把摘录预算挤爆，
# 真正的报错反而被挤没了（这是本工具在真实 l1sw 日志上踩过的坑）。
# 只要元组里有任意一个非零值，就说明【确实】发生了，不算噪声。
_ZERO_TUPLE_RE = re.compile(r"^[:=]?\s*\(([^)]*)\)")
_ZERO_SCALAR_RE = re.compile(r"^[:=]\s*(-?\d+)\b")
_NUM_RE = re.compile(r"-?\d+")
_HEX_RE = re.compile(r"\b0[xX][0-9a-fA-F]{2,}\b")

# 常见「file:line」风格：编译器报错 / 一般栈帧 / "at file(line)"
_FILE_LINE_RE = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:c|cc|cpp|cxx|h|hpp|hh|hxx)):(\d+)"
)
# glibc 风格断言：Assertion `cond' failed: file "f.c", line 42, function "foo"
# 或更简写: Assertion failed: cond, file f.c, line 42, function foo
_GLIBC_ASSERT_RE = re.compile(
    r"file\s+\"?([\w./\\-]+)\"?,\s*line\s+(\d+)(?:,\s*function\s+\"?([\w:]+)\"?)?",
    re.IGNORECASE,
)

# GDB/glibc 风格调用栈帧：#0  0xADDR in func (args) at file:line
_GDB_FRAME_RE = re.compile(
    r"^#(\d+)\s+(?:0x[0-9a-fA-F]+\s+in\s+)?(\S+)\s*\([^)]*\)\s*(?:at\s+([\w./\\-]+):(\d+))?"
)
# Python traceback 风格：File "x.py", line 42, in some_func
_PY_TRACEBACK_RE = re.compile(r'File\s+"([^"]+)",\s*line\s+(\d+),\s*in\s+(\S+)')

# 归一化模板用：把可变的数值/时间戳换成占位符，识别「同一条错误反复刷屏」
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b")
_NORM_HEX_RE = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_NORM_NUM_RE = re.compile(r"\b\d+\b")

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", "build"}
_MAX_FILE_SEARCH = 20000  # 源码根目录下搜索文件名的规模保护
_DEFAULT_MIN_REPEAT_TO_FOLD = 3  # 同一归一化模板的高危报错达到这个次数才折叠（config.yaml 可覆盖）


@dataclass
class RegisterHit:
    """从 chip-manual-kit knowledge.json 反查到的【真实存在】寄存器。"""
    token: str
    register_name: str
    module: str = ""
    address: str = ""
    description: str = ""


@dataclass
class SourceLocation:
    """日志里解析出的源码位置，及（若能定位到本地文件）真实源码上下文。"""
    file: str
    line: int
    function: str = ""
    resolved_path: str = ""
    context: str = ""
    ambiguous: bool = False


@dataclass
class ErrorGroup:
    """同一归一化模板的高危报错反复刷屏时，折叠后的分组记录（短期记忆去重）。"""
    normalized: str
    sample_line: str
    count: int
    first_line: int
    last_line: int


@dataclass
class StackFrame:
    """调用栈里的单帧：index=0 是最内层/崩溃点，往后是外层调用者。"""
    index: int
    function: str
    file: str = ""
    line: int = 0


@dataclass
class StackTrace:
    """一条完整的、有序的调用栈（GDB/glibc backtrace、Python traceback 或单帧 assert）。"""
    frames: List[StackFrame] = field(default_factory=list)
    anchor_line: int = 0  # 该栈在日志中第一行出现的真实行号（定位用）


@dataclass
class LogFacts:
    """日志问题分析的确定性事实集合。"""
    excerpt: str = ""
    hex_tokens: List[str] = field(default_factory=list)
    register_hits: List[RegisterHit] = field(default_factory=list)
    source_locations: List[SourceLocation] = field(default_factory=list)
    error_groups: List[ErrorGroup] = field(default_factory=list)
    stack_traces: List[StackTrace] = field(default_factory=list)
    total_lines_scanned: int = 0
    truncated: bool = False
    # 文件被截断（只扫描尾部）时，跳过了原文件开头这么多行；excerpt 里标注的
    # 行号已经加上这个偏移，等于原始文件里的真实行号，可以直接拿去定位/核对。
    line_offset: int = 0


# -----------------------------------------------------------------------------
# 1) 关键行摘录：围绕关键词的上下文窗口，合并重叠区间
# -----------------------------------------------------------------------------
def _is_all_zero_tail(line: str, end: int) -> bool:
    """判断关键词后紧跟的值是不是"全零"（标量或括号元组），代表【没有】发生。"""
    tail = line[end:end + 100]
    m = _ZERO_TUPLE_RE.match(tail)
    if m:
        nums = _NUM_RE.findall(m.group(1))
        return bool(nums) and all(int(n) == 0 for n in nums)
    m = _ZERO_SCALAR_RE.match(tail)
    if m:
        return int(m.group(1)) == 0
    return False


def _line_has_real_signal(line: str, regex: re.Pattern) -> bool:
    """命中关键词，且不是"计数器全为 0"这类零值噪声（如 failures:0、Error=(0 0 0)）。"""
    for m in regex.finditer(line):
        if not _is_all_zero_tail(line, m.end()):
            return True
    return False


def _find_hit_indices(lines: List[str], regex: re.Pattern) -> List[int]:
    return [i for i, ln in enumerate(lines) if _line_has_real_signal(ln, regex)]


def _normalize_line(line: str) -> str:
    """把行内可变的时间戳/十六进制/十进制数字换成占位符，得到用于分组去重的模板。

    识别"同一条报错反复刷屏"（如循环里每次都打同一条 ERROR，只有计数器/地址不同），
    是 README 里"全零计数器挤爆预算"那个坑的姊妹问题——这次是【非零但海量重复】的报错。
    """
    norm = _TS_RE.sub("<TS>", line)
    norm = _NORM_HEX_RE.sub("<HEX>", norm)
    norm = _NORM_NUM_RE.sub("<NUM>", norm)
    return norm.strip()


def _group_repeated_errors(
    lines: List[str], hit_indices: List[int], line_offset: int, min_repeat: int
) -> tuple[List[int], List["ErrorGroup"]]:
    """按归一化模板对命中行分组；同一模板达到 min_repeat 次时，只保留首、末两条命中
    参与后续摘录窗口，其余折叠为一条 ErrorGroup 记录（节省摘录预算给真正不同的信号）。
    """
    template_to_indices: dict[str, List[int]] = {}
    order: List[str] = []
    for idx in hit_indices:
        tpl = _normalize_line(lines[idx])
        if tpl not in template_to_indices:
            template_to_indices[tpl] = []
            order.append(tpl)
        template_to_indices[tpl].append(idx)

    kept_indices: List[int] = []
    groups: List[ErrorGroup] = []
    for tpl in order:
        idxs = template_to_indices[tpl]
        if len(idxs) >= min_repeat:
            kept_indices.append(idxs[0])
            kept_indices.append(idxs[-1])
            groups.append(ErrorGroup(
                normalized=tpl,
                sample_line=lines[idxs[0]].strip(),
                count=len(idxs),
                first_line=line_offset + idxs[0] + 1,
                last_line=line_offset + idxs[-1] + 1,
            ))
        else:
            kept_indices.extend(idxs)
    kept_indices = sorted(set(kept_indices))
    return kept_indices, groups


def _extract_excerpt(
    lines: List[str],
    context_lines: int,
    token_budget: int,
    line_offset: int = 0,
    min_repeat_to_fold: int = _DEFAULT_MIN_REPEAT_TO_FOLD,
) -> tuple[str, List["ErrorGroup"]]:
    # 高危词优先：ERROR/FATAL/assert/panic/.../ERR 缩写等，基本等同"确认出问题"。
    # 只有一个高危命中都没有时，才退化用中危词（FAIL/WARN 等，噪声更多）兜底。
    hit_indices = _find_hit_indices(lines, _HIGH_RE)
    if not hit_indices:
        hit_indices = _find_hit_indices(lines, _MED_RE)

    if not hit_indices:
        # 没有明显关键词：退化为摘取尾部若干行（现场日志问题多在末尾附近）
        tail_n = min(len(lines), max(2 * context_lines + 1, 40))
        chunk = lines[-tail_n:]
        text = "\n".join(
            f"{line_offset + len(lines) - tail_n + i + 1}: {ln}" for i, ln in enumerate(chunk)
        )
        return _truncate_excerpt(text, token_budget), []

    hit_indices, error_groups = _group_repeated_errors(lines, hit_indices, line_offset, min_repeat_to_fold)

    # 合并重叠/相邻窗口
    windows: List[List[int]] = []
    for idx in hit_indices:
        start = max(0, idx - context_lines)
        end = min(len(lines) - 1, idx + context_lines)
        if windows and start <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    parts: List[str] = []
    for start, end in windows:
        block = "\n".join(f"{line_offset + i + 1}: {lines[i]}" for i in range(start, end + 1))
        parts.append(block)

    max_chars = token_budget * 4
    total = sum(len(p) for p in parts) + 5 * (len(parts) - 1)
    if total <= max_chars:
        return "\n...\n".join(parts), error_groups

    # 预算不够装下所有命中窗口：优先保留【更靠后】的窗口（现场问题多在末尾附近，
    # 且这正是本次真实日志踩过的坑——从头截断会把末尾真正的报错切掉）。
    selected: List[str] = []
    used = 0
    for part in reversed(parts):
        cost = len(part) + 5
        if selected and used + cost > max_chars:
            break
        selected.append(part)
        used += cost
    selected.reverse()
    text = "\n...\n".join(selected)
    # 注：selected 已经按 max_chars 预算挑选过，不再对其整体二次截断
    # （否则会把刚保留下来的、更靠后的真正报错行从中间截断出乱码）。
    if len(selected) < len(parts):
        omitted = len(parts) - len(selected)
        text = f"（更早的 {omitted} 处关键片段因预算限制已省略，只保留时间上更靠后的）...\n" + text
    # 安全兜底：极端情况下单个窗口本身就远超预算（如 context_lines 设得很大），
    # 用更宽松的硬上限兜底，避免真的无限增长；正常情况下不会触发二次截断。
    return _truncate_excerpt(text, token_budget * 3), error_groups


def _truncate_excerpt(text: str, token_budget: int) -> str:
    max_chars = token_budget * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...（摘录已按预算截断，只有以下事实是确定的）..."


# -----------------------------------------------------------------------------
# 2) 十六进制 token
# -----------------------------------------------------------------------------
def _extract_hex_tokens(lines: List[str], limit: int = 200) -> List[str]:
    seen = set()
    out: List[str] = []
    for ln in lines:
        for m in _HEX_RE.finditer(ln):
            tok = m.group(0)
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                out.append(tok)
                if len(out) >= limit:
                    return out
    return out


# -----------------------------------------------------------------------------
# 3) 寄存器反查（可选依赖 chip-manual-kit 的 knowledge.json）
# -----------------------------------------------------------------------------
def _hex_to_int(text: str) -> Optional[int]:
    m = re.search(r"0[xX][0-9a-fA-F]+", text)
    if m:
        try:
            return int(m.group(0), 16)
        except ValueError:
            return None
    return None


def _load_kb_registers(kb_path: str) -> List[dict]:
    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("registers", []) if isinstance(data, dict) else []


def _lookup_registers(
    hex_tokens: List[str],
    raw_text: str,
    kb_path: Optional[str],
    source_root: Optional[str] = None,
    limit: int = 30,
) -> List[RegisterHit]:
    """反查真实存在的寄存器：外部 chip-manual-kit 的 kb（可选）+ 本工具自己积累的
    架构规则记忆 learned_registers.json（若 source_root 下存在），两者叠加、互不依赖。
    """
    registers: List[dict] = []
    if kb_path:
        registers.extend(_load_kb_registers(kb_path))
    registers.extend(learned_registers.load_registers(source_root))
    if not registers:
        return []

    hits: List[RegisterHit] = []
    seen_names = set()

    # 3a) 按十六进制地址值匹配（数值相等，兼容手册地址栏各种写法）
    token_ints = {tok: _hex_to_int(tok) for tok in hex_tokens}
    for reg in registers:
        addr_raw = str(reg.get("address", "") or "")
        addr_int = _hex_to_int(addr_raw)
        if addr_int is None:
            continue
        for tok, val in token_ints.items():
            if val is not None and val == addr_int:
                name = reg.get("register_name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    hits.append(RegisterHit(
                        token=tok, register_name=name, module=reg.get("module", ""),
                        address=addr_raw, description=reg.get("description", ""),
                    ))

    # 3b) 按寄存器名字面出现在日志原文里匹配（很多驱动日志直接打印寄存器名）
    text_lc = raw_text.lower()
    for reg in registers:
        name = reg.get("register_name", "")
        if not name or name in seen_names:
            continue
        if len(name) >= 4 and name.lower() in text_lc:
            seen_names.add(name)
            hits.append(RegisterHit(
                token=name, register_name=name, module=reg.get("module", ""),
                address=str(reg.get("address", "") or ""), description=reg.get("description", ""),
            ))
        if len(hits) >= limit:
            break

    return hits[:limit]


# -----------------------------------------------------------------------------
# 4) 源码位置解析与本地定位
# -----------------------------------------------------------------------------
def _extract_source_locations(raw_text: str, limit: int = 20) -> List[SourceLocation]:
    seen = set()
    out: List[SourceLocation] = []

    for m in _GLIBC_ASSERT_RE.finditer(raw_text):
        file_, line_, func_ = m.group(1), int(m.group(2)), m.group(3) or ""
        key = (file_, line_)
        if key not in seen:
            seen.add(key)
            out.append(SourceLocation(file=file_, line=line_, function=func_))

    for m in _FILE_LINE_RE.finditer(raw_text):
        file_, line_ = m.group(1), int(m.group(2))
        key = (file_, line_)
        if key not in seen:
            seen.add(key)
            out.append(SourceLocation(file=file_, line=line_))
        if len(out) >= limit:
            break

    return out[:limit]


# -----------------------------------------------------------------------------
# 4b) 结构化调用栈解析（短期记忆增强：给模型一条有序调用链，而不是散落的 file:line）
# -----------------------------------------------------------------------------
def _line_number_at(raw_text: str, pos: int, line_offset: int) -> int:
    return line_offset + raw_text.count("\n", 0, pos) + 1


def _extract_stack_traces(
    lines: List[str], raw_text: str, line_offset: int = 0, limit: int = 5
) -> List["StackTrace"]:
    """识别常见的多帧调用栈（GDB/glibc backtrace、Python traceback），聚合成有序
    StackTrace（frame 0 = 最内层/崩溃点）；另把单帧 glibc assert 也包成 size=1 的
    StackTrace，方便和多帧调用栈统一展示/统一作为 grounded 校验的锚点。
    """
    traces: List[StackTrace] = []
    covered_lines: set = set()
    i = 0
    n = len(lines)
    while i < n and len(traces) < limit:
        stripped = lines[i].strip()
        m = _GDB_FRAME_RE.match(stripped)
        if m and int(m.group(1)) == 0:
            anchor = line_offset + i + 1
            frames: List[StackFrame] = []
            j = i
            while j < n:
                fm = _GDB_FRAME_RE.match(lines[j].strip())
                if not fm:
                    break
                frames.append(StackFrame(
                    index=int(fm.group(1)), function=fm.group(2),
                    file=fm.group(3) or "", line=int(fm.group(4)) if fm.group(4) else 0,
                ))
                covered_lines.add(line_offset + j + 1)
                j += 1
            if frames:
                traces.append(StackTrace(frames=frames, anchor_line=anchor))
            i = j
            continue

        pm = _PY_TRACEBACK_RE.search(lines[i])
        if pm:
            anchor = line_offset + i + 1
            frames = []
            j = i
            while j < n:
                fm2 = _PY_TRACEBACK_RE.search(lines[j])
                if not fm2:
                    break
                frames.append(StackFrame(
                    index=len(frames), function=fm2.group(3), file=fm2.group(1), line=int(fm2.group(2)),
                ))
                covered_lines.add(line_offset + j + 1)
                j += 1
            if frames:
                traces.append(StackTrace(frames=frames, anchor_line=anchor))
            i = j
            continue

        i += 1

    # 单帧 glibc assert：复用为 size=1 的 StackTrace，和多帧调用栈统一展示/统一锚定。
    # 跳过已被 GDB/Python traceback 帧覆盖的行——_GLIBC_ASSERT_RE 的 "file X, line N"
    # 是宽松的通用模式，会误命中 Python traceback 的 `File "x.py", line N, in f`，
    # 靠 covered_lines 去重，避免同一行被算作两条不同的 StackTrace。
    for m in _GLIBC_ASSERT_RE.finditer(raw_text):
        if len(traces) >= limit:
            break
        anchor = _line_number_at(raw_text, m.start(), line_offset)
        if anchor in covered_lines:
            continue
        file_, line_, func_ = m.group(1), int(m.group(2)), m.group(3) or ""
        traces.append(StackTrace(
            frames=[StackFrame(index=0, function=func_, file=file_, line=line_)],
            anchor_line=anchor,
        ))

    return traces


def _find_local_file(source_root: str, logged_path: str) -> tuple[Optional[str], bool]:
    """在 source_root 里定位日志中的路径。

    日志路径常来自远程构建容器（如 /host/workdir/l1sw/...），与本机路径不同，
    因此按「路径后缀」匹配，从最长后缀开始尝试，找不到再退化到 basename；
    多个候选命中且无法收窄时标记 ambiguous（宁可不给上下文，也不给错的）。
    """
    norm = logged_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return None, False

    for depth in range(min(len(parts), 5), 0, -1):
        suffix = os.path.join(*parts[-depth:])
        candidates = _search_by_suffix(source_root, suffix)
        if len(candidates) == 1:
            return candidates[0], False
        if len(candidates) > 1 and depth > 1:
            continue  # 后缀太短導致歧义，尝试更长的后缀
    # 最后退化到仅按 basename 匹配
    candidates = _search_by_suffix(source_root, parts[-1])
    if len(candidates) == 1:
        return candidates[0], False
    if len(candidates) > 1:
        return None, True
    return None, False


def _search_by_suffix(source_root: str, suffix: str) -> List[str]:
    suffix_norm = suffix.replace("\\", "/")
    found: List[str] = []
    scanned = 0
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for fn in files:
            scanned += 1
            if scanned > _MAX_FILE_SEARCH:
                return found
            path = os.path.join(root, fn)
            norm = path.replace("\\", "/")
            if norm.endswith("/" + suffix_norm) or norm.endswith(suffix_norm):
                found.append(path)
    return found


def _read_context(path: str, line: int, context_lines: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return ""
    start = max(0, line - 1 - context_lines)
    end = min(len(all_lines), line + context_lines)
    return "".join(
        f"{start + i + 1}: {all_lines[start + i]}" for i in range(end - start)
    ).rstrip("\n")


def _resolve_source_locations(
    locations: List[SourceLocation], source_root: Optional[str], context_lines: int
) -> None:
    if not source_root or not os.path.isdir(source_root):
        return
    for loc in locations:
        resolved, ambiguous = _find_local_file(source_root, loc.file)
        loc.ambiguous = ambiguous
        if resolved:
            loc.resolved_path = resolved
            loc.context = _read_context(resolved, loc.line, context_lines)


# -----------------------------------------------------------------------------
# 主入口
# -----------------------------------------------------------------------------
def _count_newlines_before(path: str, byte_offset: int, chunk_size: int = 4 * 1024 * 1024) -> int:
    """统计文件里 [0, byte_offset) 字节内的换行符数量。

    只做字节计数（不解码/不建列表），即使跳过的前缀有几百 MB 也很快，
    用来把"只扫描尾部"截断后的行号换算回原始文件的真实行号。
    """
    count = 0
    remaining = byte_offset
    with open(path, "rb") as f:
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            count += chunk.count(b"\n")
            remaining -= len(chunk)
    return count


def _read_lines(
    log_text: str, log_file: Optional[str], max_scan_bytes: int
) -> tuple[List[str], bool, int]:
    if log_file:
        try:
            size = os.path.getsize(log_file)
        except OSError:
            return [], False, 0
        truncated = size > max_scan_bytes
        line_offset = 0
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            if truncated:
                skip_bytes = max(0, size - max_scan_bytes)
                line_offset = _count_newlines_before(log_file, skip_bytes)
                f.seek(skip_bytes)
            text = f.read()
        lines = text.splitlines()
        if truncated and lines:
            # seek 落在行中间，第一行大概率是被切断的半行，丢弃它，
            # 行号偏移量相应 +1，保证剩下的行号能对上原文件真实行号。
            lines = lines[1:]
            line_offset += 1
        return lines, truncated, line_offset
    return (log_text or "").splitlines(), False, 0


def collect_log_facts(
    log_text: str = "",
    log_file: Optional[str] = None,
    kb_path: Optional[str] = None,
    source_root: Optional[str] = None,
    context_lines: int = 3,
    excerpt_token_budget: int = 1200,
    max_scan_bytes: int = _DEFAULT_MAX_SCAN_BYTES,
    min_repeat_to_fold: int = _DEFAULT_MIN_REPEAT_TO_FOLD,
) -> LogFacts:
    """主入口：从日志文本/文件抽取确定性事实。

    参数:
        log_text/log_file: 二者至少提供一个；log_file 优先。
        kb_path:      chip-manual-kit 的 knowledge.json 路径（可选，用于寄存器反查）。
        source_root:  源码根目录（可选，用于定位源码上下文；同时也是 Phase 3
                      架构规则记忆 learned_registers.json 的加载位置）。
        context_lines: 关键行/源码行的上下文窗口大小。
        excerpt_token_budget: 摘录喂给模型的 token 预算。
        max_scan_bytes: 超大日志文件只扫描尾部这么多字节。
        min_repeat_to_fold: 同一归一化模板的高危报错达到这个次数才折叠为 ErrorGroup。
    """
    lines, file_truncated, line_offset = _read_lines(log_text, log_file, max_scan_bytes)
    raw_text = "\n".join(lines)

    facts = LogFacts(
        total_lines_scanned=len(lines), truncated=file_truncated, line_offset=line_offset
    )
    facts.excerpt, facts.error_groups = _extract_excerpt(
        lines, context_lines, excerpt_token_budget, line_offset, min_repeat_to_fold
    )
    facts.hex_tokens = _extract_hex_tokens(lines)
    facts.register_hits = _lookup_registers(facts.hex_tokens, raw_text, kb_path, source_root)
    facts.source_locations = _extract_source_locations(raw_text)
    facts.stack_traces = _extract_stack_traces(lines, raw_text, line_offset)
    _resolve_source_locations(facts.source_locations, source_root, context_lines)
    return facts
