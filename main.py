#!/usr/bin/env python3
# =============================================================================
# main.py —— ai_toolkit 统一 CLI 入口
# 用途：通过子命令调度 4 个工具 Skill，以及批处理调度器。
#
# 用法示例：
#   python main.py ut --file demo.py --func add
#   python main.py regex --desc "匹配邮箱" --pos a@b.com --neg "not_email"
#   python main.py type --file snippet.py            # 读取整文件作为函数源码
#   python main.py product --root ./myproject --req "新增一个重试装饰器"
#   python main.py batch --jobs jobs.json            # 夜间无人值守批处理
#
# 兼容需求中的 `--task` 风格：
#   python main.py --task ut --file demo.py --func add
# =============================================================================
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from tools.ut_gen import generate_unit_test
from tools.regex_gen import generate_regex
from tools.type_annotate import annotate_types
from tools.product_gen import generate_product
from tools.summarize_gen import summarize_project


def _print_result(title: str, result) -> int:
    """统一打印工具结果，返回进程退出码。"""
    print(f"\n===== {title} =====")
    for line in result.history:
        print("  " + line)
    if result.ok:
        print(f"\n[成功] 迭代 {result.iterations} 次。产物如下：\n")
        print(result.output)
        return 0
    print(f"\n[失败] {result.error}")
    return 1


# --- 各子命令处理函数 ---------------------------------------------------------
def _cmd_ut(args: argparse.Namespace) -> int:
    res = generate_unit_test(
        args.file, args.func,
        framework=getattr(args, "framework", None),
        project_root=getattr(args, "root", None),
    )
    return _print_result(f"单测生成 {args.file}::{args.func}", res)


def _cmd_regex(args: argparse.Namespace) -> int:
    res = generate_regex(args.desc, args.pos or [], args.neg or [])
    return _print_result(f"正则生成: {args.desc}", res)


def _cmd_type(args: argparse.Namespace) -> int:
    with open(args.file, "r", encoding="utf-8") as f:
        source = f.read()
    res = annotate_types(source)
    return _print_result(f"类型注解 {args.file}", res)


def _cmd_product(args: argparse.Namespace) -> int:
    res = generate_product(args.root, args.req)
    return _print_result(f"产品代码生成 @ {args.root}", res)


def _cmd_summarize(args: argparse.Namespace) -> int:
    res = summarize_project(
        args.root,
        out_dir=getattr(args, "out", None),
        focus=getattr(args, "focus", None),
        deep=True if getattr(args, "deep", False) else None,
    )
    return _print_result(f"项目总结 @ {args.root}", res)


def _cmd_batch(args: argparse.Namespace) -> int:
    # 延迟导入，避免无关命令也加载调度器
    from batch_scheduler import run_batch
    return run_batch(args.jobs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai_toolkit",
        description="本地受限环境 AI 辅助编程工具集（Ollama + 确定性校验）",
    )
    # 兼容 `--task xxx` 写法（可选；不传则用子命令）
    parser.add_argument("--task", choices=["ut", "regex", "type", "product", "summarize", "batch"],
                        help="任务类型（等价于子命令）")

    sub = parser.add_subparsers(dest="command")

    p_ut = sub.add_parser("ut", help="为指定函数生成单测（自动探测 pytest/googletest/cpputest）")
    p_ut.add_argument("--file", required=True, help="源文件路径（.py / .c / .cpp / .h ...）")
    p_ut.add_argument("--func", required=True, help="目标函数名")
    p_ut.add_argument("--framework", choices=["pytest", "googletest", "cpputest"],
                      default=None, help="显式指定框架；缺省则自动探测项目现有框架")
    p_ut.add_argument("--root", default=None, help="项目根目录（探测现有 UT 框架用，默认取源文件所在目录）")
    p_ut.set_defaults(handler=_cmd_ut)

    p_re = sub.add_parser("regex", help="根据正反例生成正则")
    p_re.add_argument("--desc", required=True, help="需求描述")
    p_re.add_argument("--pos", nargs="*", default=[], help="正例（应匹配）")
    p_re.add_argument("--neg", nargs="*", default=[], help="反例（不应匹配）")
    p_re.set_defaults(handler=_cmd_regex)

    p_ty = sub.add_parser("type", help="为文件中的函数添加类型注解")
    p_ty.add_argument("--file", required=True, help="包含函数源码的文件")
    p_ty.set_defaults(handler=_cmd_type)

    p_pr = sub.add_parser("product", help="基于项目骨架生成新产品代码")
    p_pr.add_argument("--root", required=True, help="项目根目录")
    p_pr.add_argument("--req", required=True, help="需求描述")
    p_pr.set_defaults(handler=_cmd_product)

    p_sm = sub.add_parser("summarize", help="分而治之总结整个目录（骨架→逐文件分析→记录拼接）")
    p_sm.add_argument("--root", required=True, help="要总结的项目根目录")
    p_sm.add_argument("--out", default=None, help="中间结果与报告输出目录（默认 .summaries）")
    p_sm.add_argument("--focus", default=None,
                      help="关注点：一句话说明想总结什么主题（如「用了哪些 CUDA 优化技巧」），覆盖 config.focus")
    p_sm.add_argument("--deep", action="store_true",
                      help="深度模式：对含 CUDA 标记的函数抽完整实现体分析（看到 __shared__/<<<>>> 等细节）")
    p_sm.set_defaults(handler=_cmd_summarize)

    p_ba = sub.add_parser("batch", help="从 jobs.json 批处理（夜间无人值守）")
    p_ba.add_argument("--jobs", default="jobs.json", help="任务队列文件")
    p_ba.set_defaults(handler=_cmd_batch)

    return parser


def _force_utf8_io() -> None:
    """让输出不会因无法编码的字符而崩溃。
    Windows 中文环境/重定向管道默认 gbk，遇到 emoji（如 ✅）会抛
    UnicodeEncodeError 直接崩溃。这里【保留控制台原编码】只把错误策略改成
    'replace'：中文（gbk 可编码）照常正确显示，emoji 退化为 '?'，永不崩溃，
    也不会像强制 UTF-8 那样在 gbk 控制台下把中文显示成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)

    # 若用户用 --task 指定但没给子命令，提示需配合子命令参数
    if not getattr(args, "handler", None):
        if args.task:
            print(f"请使用子命令形式提供参数，例如: python main.py {args.task} --help")
        else:
            parser.print_help()
        return 2

    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\n已被用户中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
