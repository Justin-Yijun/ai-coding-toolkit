# =============================================================================
# tools/summarize_gen.py
# 用途：大型项目「分而治之」分析 Skill —— 直面小上下文窗口的核心工作流。
#
#   工作流（与「人读大项目」一致）：
#       1) 骨架抽取：每个文件先抽成「导入/类名/函数签名」骨架（无实现体）。
#       2) 逐文件分析（MAP）：每个文件单独喂模型，产出 2-4 句摘要；
#          **每分析完一个就立刻落盘记录**（manifest.jsonl），天然断点续跑。
#       3) 拼接归并（REDUCE）：把各文件摘要拼起来；若超出上下文预算，
#          就分组压缩、分层归并，直到能放进窗口，最终产出整体架构综述。
#
#   两个对外能力：
#       · summarize_project   —— 产出人类可读的项目总结报告（map+reduce）。
#       · build_project_context —— 给 product_gen 用：骨架放得下就直接用；
#                                  放不下就「逐文件分析→记录→拼接」成精简摘要。
#
# 无状态：每次模型调用只喂「单个文件骨架」或「一组摘要」，永不超窗。
# =============================================================================
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from core.llm_client import LLMClient, load_config
from core.lang_utils import extract_skeleton, extract_cuda_excerpts, detect_language
from core.text_utils import approx_token_count, truncate_to_tokens

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                ".mypy_cache", "build", ".summaries"}
_SOURCE_EXT = {".py", ".pyi", ".c", ".cc", ".cpp", ".cxx", ".c++",
               ".h", ".hpp", ".hh", ".hxx", ".inl"}

_SYSTEM_MAP = (
    "你是资深软件架构师。用中文，2-4 句话概括该文件的职责、关键类/函数与对外接口。"
    "只输出摘要，不要解释、不要复述代码。"
)
_SYSTEM_REDUCE = (
    "你是资深软件架构师。基于给定的各部分摘要，输出中文的整体架构与模块职责综述，"
    "突出模块划分、协作关系与命名/风格约定。只输出综述。"
)

# 单文件分析【彻底失败】（连骨架回退也失败，通常是 Ollama 不可用）的占位前缀。
# 这类结果不写入 manifest，以便下次运行或调大 timeout 后自动重试。
_FAIL_PREFIX = "⚠️ 自动跳过（分析失败）"


def _apply_focus(base_system: str, focus: str) -> str:
    """把用户关注点注入 system 提示词；留空则用原始提示词。"""
    focus = (focus or "").strip()
    if not focus:
        return base_system
    return (
        f"{base_system}\n请特别聚焦于：{focus}\n"
        "围绕该主题展开，给出具体、可操作的要点；与该主题无关的内容可略去。"
    )


def _resolve_prompts(
    s_cfg: dict, focus_override: Optional[str]
) -> Tuple[str, str, str]:
    """解析生效的 (map_system, reduce_system, focus)。
    优先级：CLI/参数 focus_override > config.focus；system 可被 config 完全覆盖。"""
    focus = focus_override if focus_override is not None else str(s_cfg.get("focus", ""))
    map_base = str(s_cfg.get("map_system") or "").strip() or _SYSTEM_MAP
    reduce_base = str(s_cfg.get("reduce_system") or "").strip() or _SYSTEM_REDUCE
    return _apply_focus(map_base, focus), _apply_focus(reduce_base, focus), (focus or "").strip()


def _manifest_name(map_system: str, focus: str, deep: bool = False) -> str:
    """不同关注点/提示词/深度用不同缓存文件，避免旧摘要污染新主题。"""
    if not focus and not deep and map_system == _SYSTEM_MAP:
        return "manifest.jsonl"        # 默认场景保持原文件名（向后兼容）
    key = f"{map_system}\x00{focus}\x00deep={int(deep)}"
    tag = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"manifest-{tag}.jsonl"


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""
    iterations: int = 0
    history: List[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# 文件遍历 / 骨架
# -----------------------------------------------------------------------------
def _iter_source_files(root: str) -> List[str]:
    files: List[str] = []
    for cur, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in _SOURCE_EXT:
                files.append(os.path.join(cur, name))
    return files


def _file_skeleton(path: str, per_file_tokens: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return ""
    skel = extract_skeleton(path, source)
    return truncate_to_tokens("\n".join(skel), per_file_tokens) if skel else ""


def _file_analysis_input(
    path: str,
    per_file_tokens: int,
    deep: bool,
    deep_tokens: int,
    markers,
) -> Tuple[str, str]:
    """返回 (喂给模型的文本, 类型)。类型为 'deep'(含实现体) 或 'skeleton'。
    深度模式下：含 CUDA 标记的函数抽完整实现体；无标记则回退骨架。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return "", "skeleton"
    if deep and detect_language(path) == "cpp":
        excerpts = extract_cuda_excerpts(source, markers)
        if excerpts:
            joined = "\n\n// ---- 下一段 ----\n".join(excerpts)
            return truncate_to_tokens(joined, deep_tokens), "deep"
    skel = extract_skeleton(path, source)
    return (truncate_to_tokens("\n".join(skel), per_file_tokens) if skel else ""), "skeleton"


def _safe_name(path: str) -> str:
    base = os.path.basename(os.path.normpath(path)) or "root"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)


# -----------------------------------------------------------------------------
# 中间结果记录（断点续跑）：每文件摘要按行追加到 manifest.jsonl
# -----------------------------------------------------------------------------
def _load_manifest(manifest_path: str) -> Dict[str, str]:
    done: Dict[str, str] = {}
    if not os.path.exists(manifest_path):
        return done
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done[rec["file"]] = rec["summary"]
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _append_manifest(manifest_path: str, rel: str, summary: str) -> None:
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"file": rel, "summary": summary}, ensure_ascii=False) + "\n")


# -----------------------------------------------------------------------------
# MAP：逐文件分析（每个文件单独喂模型，分析完即落盘）
# -----------------------------------------------------------------------------
def _deep_body(rel: str, content: str, focus_line: str) -> str:
    return (
        f"文件: {rel}\n以下是该文件中含 CUDA 标记的函数【完整实现体】：\n"
        f"```cpp\n{content}\n```\n{focus_line}"
        "请指出代码中具体使用的 CUDA 技巧（如共享内存、warp 级原语、原子操作、"
        "内存合并/__ldg、同步、kernel 启动配置、流/异步等），并简述每项的作用与位置。"
    )


def _skel_body(rel: str, content: str, focus_line: str) -> str:
    return (
        f"文件: {rel}\n骨架（仅签名，无实现体）：\n```\n{content}\n```\n{focus_line}"
        "请概括该文件。"
    )


def _try_generate(
    client: LLMClient,
    body: str,
    system: str,
    out_budget: int,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Optional[str]]:
    """调用模型并吞掉异常，返回 (摘要, 错误信息)。
    成功：(摘要, None)；失败：("", 错误文本)。让调用方决定回退/跳过，绝不向上抛。"""
    try:
        out = client.generate(
            body, system=system, overrides={"num_predict": out_budget}
        ).strip()
        return re.sub(r"\s+", " ", out), None
    except RuntimeError as exc:   # llm_client 把超时/网络错统一包成 RuntimeError
        if log:
            log(f"[WARN] 单文件生成失败：{exc}")
        return "", str(exc)


def _map_summarize(
    project_root: str,
    client: LLMClient,
    cfg: dict,
    manifest_path: str,
    map_system: str,
    focus: str = "",
    deep: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> List[Tuple[str, str]]:
    s_cfg = cfg.get("summarize", {})
    per_file_tokens = int(s_cfg.get("per_file_tokens", 600))
    map_tokens = int(s_cfg.get("map_summary_tokens", 200))
    deep_tokens = int(s_cfg.get("deep_file_tokens", 1200))
    deep_out_tokens = int(s_cfg.get("deep_summary_tokens", 400))
    markers = s_cfg.get("deep_markers") or None

    focus_line = (
        f"请围绕「{focus}」来分析该文件；若该文件与此主题无关，简要说明即可。\n"
        if focus else ""
    )
    done = _load_manifest(manifest_path)            # 断点续跑：已分析的跳过
    results: List[Tuple[str, str]] = []
    files = _iter_source_files(project_root)
    for path in files:
        rel = os.path.relpath(path, project_root)
        if rel in done:
            results.append((rel, done[rel]))
            continue
        content, kind = _file_analysis_input(
            path, per_file_tokens, deep, deep_tokens, markers
        )
        if not content.strip():
            continue

        # 逐文件「隔离容错」：单个文件超时/出错绝不拖垮整轮任务。
        if kind == "deep":
            body = _deep_body(rel, content, focus_line)
            summary, err = _try_generate(client, body, map_system, deep_out_tokens, log)
            if err:
                # 深度失败 → 自动回退到「骨架+短输出」（更快），再试一次。
                skel = _file_skeleton(path, per_file_tokens)
                if skel.strip():
                    body = _skel_body(rel, skel, focus_line)
                    summary, err = _try_generate(
                        client, body, map_system, map_tokens, log
                    )
                    kind = "skeleton(回退)"
        else:
            body = _skel_body(rel, content, focus_line)
            summary, err = _try_generate(client, body, map_system, map_tokens, log)

        if err or not summary:
            # 彻底失败（通常是 Ollama 不可用）：不写 manifest，保留下次重试机会，
            # 但本轮继续处理后续文件，保证任务能跑完。
            results.append((rel, f"{_FAIL_PREFIX}：{err or '空输出'}"))
            if log:
                log(f"[MAP/failed] {rel} 跳过（未记录，下次可自动重试）")
            continue

        _append_manifest(manifest_path, rel, summary)   # 成功才记录，断点续跑
        results.append((rel, summary))
        if log:
            log(f"[MAP/{kind}] {rel} 已分析并记录")
    return results


# -----------------------------------------------------------------------------
# REDUCE：分组拼接，超预算则分层归并
# -----------------------------------------------------------------------------
def _group_by_budget(blocks: List[str], budget: int) -> List[List[str]]:
    groups: List[List[str]] = []
    cur: List[str] = []
    cur_tok = 0
    for b in blocks:
        t = approx_token_count(b)
        if cur and cur_tok + t > budget:
            groups.append(cur)
            cur, cur_tok = [], 0
        cur.append(b)
        cur_tok += t
    if cur:
        groups.append(cur)
    return groups


def _reduce(
    blocks: List[str],
    client: LLMClient,
    budget: int,
    system: str,
    final_instruction: str,
    compress_instruction: str,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """分层归并：每轮把超预算的摘要分组压缩，直到能一次性放进窗口。
    容错：任一模型调用失败（超时/Ollama 不可用）即降级为「直接拼接」，
    保证 summarize_project 永远能产出报告，不会因 REDUCE 崩溃而前功尽弃。"""
    while len(blocks) > 1:
        groups = _group_by_budget(blocks, budget)
        # 无法再按预算合并（单块已超预算，分组数不减）→ 跳出收尾，避免死循环
        if len(groups) >= len(blocks):
            break
        if log:
            log(f"[REDUCE] {len(blocks)} 段超预算，分 {len(groups)} 组压缩后再归并")
        new_blocks: List[str] = []
        for g in groups:
            joined = "\n".join(g)
            try:
                out = client.generate(
                    f"{compress_instruction}\n{joined}", system=system
                ).strip()
            except RuntimeError as exc:
                if log:
                    log(f"[WARN] REDUCE 分组压缩失败，退化为截断拼接：{exc}")
                out = truncate_to_tokens(joined, budget)
            new_blocks.append(out)
        blocks = new_blocks
    text = "\n".join(blocks)
    try:
        return client.generate(f"{final_instruction}\n{text}", system=system).strip()
    except RuntimeError as exc:
        if log:
            log(f"[WARN] REDUCE 总览生成失败，改用各部分摘要直接拼接：{exc}")
        return "（自动降级：模型不可用，以下为各文件摘要的直接拼接）\n" + text


# -----------------------------------------------------------------------------
# 对外能力 1：项目总结报告
# -----------------------------------------------------------------------------
def summarize_project(
    project_root: str,
    client: Optional[LLMClient] = None,
    out_dir: Optional[str] = None,
    focus: Optional[str] = None,
    deep: Optional[bool] = None,
) -> ToolResult:
    cfg = load_config()
    client = client or LLMClient(cfg)
    s_cfg = cfg.get("summarize", {})
    reduce_budget = int(s_cfg.get("reduce_group_tokens", 1500))
    base_out = out_dir or s_cfg.get("output_dir", ".summaries")
    map_system, reduce_system, eff_focus = _resolve_prompts(s_cfg, focus)
    eff_deep = bool(s_cfg.get("deep", False)) if deep is None else bool(deep)

    result = ToolResult(ok=False)
    if not os.path.isdir(project_root):
        result.error = f"项目目录不存在: {project_root}"
        return result

    work_dir = os.path.join(base_out, _safe_name(project_root))
    manifest_path = os.path.join(work_dir, _manifest_name(map_system, eff_focus, eff_deep))
    report_path = os.path.join(work_dir, "summary.md")
    if eff_focus:
        result.history.append(f"关注点 focus：{eff_focus}")
    if eff_deep:
        result.history.append("深度模式：含 CUDA 标记的函数将按完整实现体分析")

    pairs = _map_summarize(
        project_root, client, cfg, manifest_path, map_system,
        eff_focus, eff_deep, result.history.append,
    )
    if not pairs:
        result.error = "未找到可分析的源码文件"
        return result
    result.history.append(f"MAP 完成：{len(pairs)} 个文件已分析并记录到 {manifest_path}")
    failed = [rel for rel, summ in pairs if summ.startswith(_FAIL_PREFIX)]
    if failed:
        result.history.append(
            f"⚠️ {len(failed)} 个文件本轮失败（已跳过、未记录，重跑或调大 timeout 后自动重试）"
        )

    blocks = [f"{rel}: {summ}" for rel, summ in pairs]
    overview = _reduce(
        blocks, client, reduce_budget, reduce_system,
        final_instruction="以下是各文件摘要，请输出项目整体综述：",
        compress_instruction="请把以下若干文件摘要压缩成更短的分组综述：",
        log=result.history.append,
    )

    # 拼接最终报告并落盘
    lines = [
        f"# 项目总结：{project_root}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 文件数：{len(pairs)}",
    ]
    if failed:
        lines.append(f"- 分析失败（已跳过）：{len(failed)} 个")
    if eff_focus:
        lines.append(f"- 关注点：{eff_focus}")
    if eff_deep:
        lines.append("- 分析粒度：深度模式（含 CUDA 标记的函数按完整实现体分析）")
    lines += [
        "",
        "## 总体架构",
        "",
        overview,
        "",
        "## 各文件摘要",
        "",
    ]
    lines += [f"- `{rel}`：{summ}" for rel, summ in pairs]
    os.makedirs(work_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    result.ok = True
    result.output = overview
    result.history.append(f"REDUCE 完成，报告已写入 {report_path}")
    return result


# -----------------------------------------------------------------------------
# 对外能力 2：给 product_gen 构建「项目风格上下文」
#   骨架放得下 → 直接返回骨架；放不下 → 逐文件分析+记录+拼接成精简摘要。
# -----------------------------------------------------------------------------
def build_project_context(
    project_root: str,
    client: LLMClient,
    total_budget: int,
    out_dir: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, bool]:
    """返回 (上下文文本, 是否走了 map-reduce 压缩)。"""
    cfg = load_config()
    s_cfg = cfg.get("summarize", {})
    per_file_tokens = int(s_cfg.get("per_file_tokens", 600))

    # 先尝试纯骨架拼接（小项目零模型调用）
    files = _iter_source_files(project_root)
    blocks: List[str] = []
    for path in files:
        skel = _file_skeleton(path, per_file_tokens)
        if not skel.strip():
            continue
        rel = os.path.relpath(path, project_root)
        blocks.append(f"# --- {rel} ---\n{skel}")
    joined = "\n\n".join(blocks)
    if approx_token_count(joined) <= total_budget:
        return joined, False

    # 骨架超预算：逐文件分析 → 记录 → 拼接（必要时分层归并到预算内）
    if log:
        log("骨架超出上下文预算，转为「逐文件分析+记录+拼接」模式")
    map_system, reduce_system, eff_focus = _resolve_prompts(s_cfg, None)
    eff_deep = bool(s_cfg.get("deep", False))
    base_out = out_dir or s_cfg.get("output_dir", ".summaries")
    manifest_path = os.path.join(
        base_out, _safe_name(project_root), _manifest_name(map_system, eff_focus, eff_deep)
    )
    pairs = _map_summarize(
        project_root, client, cfg, manifest_path, map_system, eff_focus, eff_deep, log
    )

    digest_blocks = [f"- {rel}: {summ}" for rel, summ in pairs]
    digest = "\n".join(digest_blocks)
    if approx_token_count(digest) > total_budget:
        reduce_budget = int(s_cfg.get("reduce_group_tokens", 1500))
        digest = _reduce(
            digest_blocks, client, min(reduce_budget, total_budget), reduce_system,
            final_instruction="请把以下文件摘要汇总为一份不超过预算的项目结构综述：",
            compress_instruction="请把以下文件摘要压缩成更短的分组综述：",
            log=log,
        )
    header = "# 项目结构摘要（由逐文件分析归并而来，非完整源码）\n"
    return header + truncate_to_tokens(digest, total_budget), True
