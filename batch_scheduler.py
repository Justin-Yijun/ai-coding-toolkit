# =============================================================================
# batch_scheduler.py —— 批处理调度引擎（夜间无人值守的核心）
# 用途：从 jobs.json 读取任务队列，按优先级排序后逐个执行，具备：
#   - 优先级排序（priority 越小越先执行）。
#   - 断点续跑（checkpoint.json 记录已完成任务 ID，重启自动跳过）。
#   - 失败重试（单任务最多 3 次，未达上限则放回队尾延后再试）。
#   - 任务间隔延迟（delay_between_jobs，防止 CPU 持续满载过热）。
#   - 生成 report.md 摘要（成功/失败统计 + 失败清单）。
#
# jobs.json 单条任务结构示例：
#   {
#     "id": "job-001",
#     "priority": 1,
#     "task": "ut",                         # ut | regex | type | product
#     "params": {"file": "demo.py", "func": "add"}
#   }
# =============================================================================
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List

from core.llm_client import load_config
from tools.ut_gen import generate_unit_test
from tools.regex_gen import generate_regex
from tools.type_annotate import annotate_types
from tools.product_gen import generate_product
from tools.summarize_gen import summarize_project


# 任务类型 -> 执行函数的分发表（无状态映射）
def _run_ut(p: Dict[str, Any]):
    return generate_unit_test(
        p["file"], p["func"],
        framework=p.get("framework"),
        project_root=p.get("root"),
    )


def _run_regex(p: Dict[str, Any]):
    return generate_regex(p["desc"], p.get("pos", []), p.get("neg", []))


def _run_type(p: Dict[str, Any]):
    with open(p["file"], "r", encoding="utf-8") as f:
        return annotate_types(f.read())


def _run_product(p: Dict[str, Any]):
    return generate_product(p["root"], p["req"])


def _run_summarize(p: Dict[str, Any]):
    return summarize_project(p["root"], out_dir=p.get("out"))


_DISPATCH = {
    "ut": _run_ut,
    "regex": _run_regex,
    "type": _run_type,
    "product": _run_product,
    "summarize": _run_summarize,
}


@dataclass
class JobOutcome:
    job_id: str
    task: str
    status: str            # "success" | "failed"
    attempts: int
    message: str = ""


@dataclass
class Checkpoint:
    """断点续跑状态，落盘为 checkpoint.json。"""
    path: str
    done: Dict[str, str] = field(default_factory=dict)   # job_id -> status

    @classmethod
    def load(cls, path: str) -> "Checkpoint":
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls(path=path, done=data.get("done", {}))
            except (json.JSONDecodeError, OSError):
                pass
        return cls(path=path)

    def mark(self, job_id: str, status: str) -> None:
        self.done[job_id] = status
        self.save()

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"done": self.done, "updated": datetime.now().isoformat()},
                      f, ensure_ascii=False, indent=2)


def _load_jobs(jobs_path: str) -> List[Dict[str, Any]]:
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    if not isinstance(jobs, list):
        raise ValueError("jobs.json 顶层必须是任务数组")
    # 优先级排序：priority 越小越先（缺省 999）
    jobs.sort(key=lambda j: j.get("priority", 999))
    return jobs


def _write_report(report_path: str, outcomes: List[JobOutcome], skipped: List[str]) -> None:
    success = [o for o in outcomes if o.status == "success"]
    failed = [o for o in outcomes if o.status == "failed"]
    lines: List[str] = [
        "# 批处理执行报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 总执行：{len(outcomes)} | 成功：{len(success)} | 失败：{len(failed)}"
        f" | 断点跳过：{len(skipped)}",
        "",
        "## 成功任务",
        "",
    ]
    if success:
        for o in success:
            lines.append(f"- ✅ `{o.job_id}` ({o.task}) — 尝试 {o.attempts} 次")
    else:
        lines.append("（无）")

    lines += ["", "## 失败任务清单", ""]
    if failed:
        for o in failed:
            lines.append(f"- ❌ `{o.job_id}` ({o.task}) — 尝试 {o.attempts} 次")
            if o.message:
                snippet = o.message.replace("\n", " ")[:200]
                lines.append(f"    - 原因：{snippet}")
    else:
        lines.append("（无）")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_batch(jobs_path: str = "jobs.json") -> int:
    """批处理主循环。返回进程退出码（0=全部成功，1=存在失败）。"""
    cfg = load_config()
    sch = cfg.get("scheduler", {})
    max_retries = int(sch.get("max_retries", 3))
    delay = float(sch.get("delay_between_jobs", 5))
    checkpoint_path = sch.get("checkpoint_file", "checkpoint.json")
    report_path = sch.get("report_file", "report.md")

    if not os.path.exists(jobs_path):
        print(f"[错误] 找不到任务文件: {jobs_path}")
        return 1

    jobs = _load_jobs(jobs_path)
    checkpoint = Checkpoint.load(checkpoint_path)

    # 构建待执行队列；已在 checkpoint 中成功的任务直接跳过（断点续跑）
    queue: Deque[Dict[str, Any]] = deque()
    skipped: List[str] = []
    for job in jobs:
        jid = job.get("id")
        if jid and checkpoint.done.get(jid) == "success":
            skipped.append(jid)
            print(f"[跳过] 任务 {jid} 已完成（断点续跑）")
            continue
        job["_attempts"] = 0
        queue.append(job)

    outcomes: List[JobOutcome] = []
    print(f"[开始] 待执行 {len(queue)} 个任务，跳过 {len(skipped)} 个。")

    while queue:
        job = queue.popleft()
        jid = job.get("id", "<no-id>")
        task = job.get("task", "")
        job["_attempts"] += 1
        attempt = job["_attempts"]

        runner = _DISPATCH.get(task)
        if runner is None:
            msg = f"未知任务类型: {task}"
            print(f"[错误] {jid}: {msg}")
            outcomes.append(JobOutcome(jid, task, "failed", attempt, msg))
            checkpoint.mark(jid, "failed")
            continue

        print(f"[执行] {jid} ({task}) 第 {attempt}/{max_retries} 次尝试...")
        try:
            result = runner(job.get("params", {}))
            ok = result.ok
            message = "" if ok else result.error
        except Exception as exc:  # noqa: BLE001 - 单任务隔离，不拖垮整批
            ok = False
            message = f"执行异常: {exc}"

        if ok:
            print(f"[成功] {jid}")
            outcomes.append(JobOutcome(jid, task, "success", attempt))
            checkpoint.mark(jid, "success")
        elif attempt < max_retries:
            # 未达重试上限：放回队尾延后再试
            print(f"[重试] {jid} 失败，放回队尾（已尝试 {attempt} 次）")
            queue.append(job)
        else:
            print(f"[放弃] {jid} 连续失败 {attempt} 次")
            outcomes.append(JobOutcome(jid, task, "failed", attempt, message))
            checkpoint.mark(jid, "failed")

        # 任务间隔延迟，给 CPU 降温喘息
        if queue and delay > 0:
            time.sleep(delay)

    _write_report(report_path, outcomes, skipped)
    failed_count = sum(1 for o in outcomes if o.status == "failed")
    print(f"\n[完成] 报告已写入 {report_path}。失败 {failed_count} 个。")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(run_batch())
