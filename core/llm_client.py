# =============================================================================
# core/llm_client.py
# 用途：封装本地 Ollama /api/generate 调用，是所有工具访问大模型的唯一入口。
#       从 config.yaml 读取模型名、temperature、num_predict 等参数。
#
# -----------------------------------------------------------------------------
# 【模型升级时的参数调整建议】（重要，务必阅读）
# -----------------------------------------------------------------------------
# 本工具集初始面向「CPU 上运行的 9B 量化弱模型」（如 Qwen2.5-Coder-7B），
# 其特点是：上下文窗口小、容易胡说、推理慢。因此默认参数偏保守：
#
#   旧 9B 弱模型（当前默认）：
#       temperature = 0.2     # 略大于 0，抑制幻觉但保留少量灵活性
#       num_predict = 1024    # 限制输出长度，防止 CPU 长时间空转 / 上下文溢出
#       timeout     = 90      # CPU 推理慢，给足超时
#
#   升级到强模型后（如 32B / 70B，或换 GPU 推理）建议：
#       temperature = 0.05 ~ 0.1   # 强模型更稳，降低温度换取更确定、可复现的代码
#       num_predict = 4096+        # 放开输出限制，允许生成更完整的大段代码
#       （若上下文窗口扩大到 32K+，product_gen 的骨架裁剪阈值也可同步放宽）
#
# 调整方式：只改 config.yaml，无需改动任何业务代码。
# =============================================================================
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests
import yaml

# config.yaml 默认与本工具集根目录同级
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.yaml",
)


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 YAML 配置。无状态：每次按需读取，不缓存全局状态。"""
    cfg_path = path or _DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"找不到配置文件: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class LLMClient:
    """Ollama 客户端。线程内无状态，仅持有连接配置。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_config()
        ollama_cfg = self.config.get("ollama", {})
        self.base_url = ollama_cfg.get("base_url", "http://localhost:11434").rstrip("/")
        self.model = ollama_cfg.get("model", "qwen2.5-coder:7b")
        self.timeout = int(ollama_cfg.get("timeout", 90))
        self.gen_cfg = self.config.get("generation", {})

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        """调用 /api/generate（非流式），返回模型输出文本。

        参数:
            prompt:    用户提示词（已在调用方裁剪到上下文预算内）。
            system:    可选 system 提示，用于约束角色/输出格式。
            overrides: 临时覆盖生成参数（如某任务想单独调高 num_predict）。
        """
        options: Dict[str, Any] = {
            "temperature": float(self.gen_cfg.get("temperature", 0.2)),
            "top_p": float(self.gen_cfg.get("top_p", 0.9)),
            "num_predict": int(self.gen_cfg.get("num_predict", 1024)),
        }
        stop = self.gen_cfg.get("stop") or []
        if stop:
            options["stop"] = stop
        if overrides:
            options.update(overrides)

        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,  # 非流式，便于确定性解析
            "options": options,
        }
        if system:
            payload["system"] = system

        url = f"{self.base_url}/api/generate"
        try:
            # 所有 HTTP 请求强制设置超时（timeout=90，来自 config）
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(
                f"Ollama 请求超时（>{self.timeout}s）。CPU 推理慢，可调大 config 的 timeout。"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Ollama 请求失败: {exc}") from exc

        data = resp.json()
        return data.get("response", "")
