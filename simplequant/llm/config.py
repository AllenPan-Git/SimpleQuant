"""
大模型接入配置

支持三类接口：
- anthropic : Claude 官方 SDK
- openai    : OpenAI 官方 SDK；也用于所有"兼容 OpenAI 协议"的服务（Gemini、DeepSeek、通义千问、Kimi、智谱、本地 Ollama……）
- litellm   : LiteLLM，一个库对接上百家模型，模型名形如 "deepseek/deepseek-chat"

配置保存位置（都按当前 Windows 用户区分，每人一份）：
- 源码运行：~/.simplequant/llm.json，不在项目目录里，避免 key 被提交或随项目复制出去
- 打包成 exe：和其他数据放在一起，%LOCALAPPDATA%\\SimpleQuant\\llm.json；第一次运行时自动从 ~/.simplequant 搬过来
- 环境变量 SIMPLEQUANT_HOME 可指定目录（优先级最高）
api_key 留空时，各 SDK 会读取自己的环境变量（ANTHROPIC_API_KEY / OPENAI_API_KEY 等）。
"""

import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path

from ..i18n import L
from ..paths import DATA_ROOT, FROZEN

LEGACY_DIR = Path.home() / ".simplequant"         # 源码版的位置；exe 第一次运行时从这里搬


def _config_dir() -> Path:
    if env := os.environ.get("SIMPLEQUANT_HOME"):
        return Path(env)
    return DATA_ROOT if FROZEN else LEGACY_DIR


CONFIG_DIR = _config_dir()
CONFIG_PATH = CONFIG_DIR / "llm.json"

PROVIDERS = {
    "anthropic": L("Claude 官方接口", "Anthropic (Claude)"),
    "openai": L("OpenAI / 兼容 OpenAI 的接口", "OpenAI / OpenAI-compatible"),
    "litellm": L("LiteLLM（统一接入多家模型）", "LiteLLM (many providers)"),
}

# 结构化输出方式：
#   schema      服务端按 JSON Schema 约束输出（Claude、OpenAI、较新的 Ollama 支持）
#   json_object 只保证输出是 JSON，结构靠提示词（大多数国内兼容接口支持）
#   prompt      不用任何服务端约束，完全靠提示词并从回复中提取 JSON（兜底）
JSON_MODES = {
    "schema": L("JSON Schema（最可靠）", "JSON Schema (most reliable)"),
    "json_object": L("JSON 模式", "JSON mode"),
    "prompt": L("仅提示词（兼容性最好）", "Prompt only (most compatible)"),
}

# 预设：填好接口类型、地址和示例模型名。模型名会随服务商更新，以其文档为准。
PRESETS = {
    "claude": {"label": L("Claude（Anthropic）", "Claude (Anthropic)"), "provider": "anthropic",
               "model": "claude-opus-5-5", "base_url": "", "json_mode": "schema", "key_env": "ANTHROPIC_API_KEY"},
    "openai": {"label": L("OpenAI", "OpenAI"), "provider": "openai",
               "model": "", "base_url": "", "json_mode": "schema", "key_env": "OPENAI_API_KEY"},
    # Google 官方的 OpenAI 兼容接口；JSON Schema 只支持一部分关键字，默认用 JSON 模式
    "gemini": {"label": L("Gemini（Google）", "Gemini (Google)"), "provider": "openai",
               "model": "gemini-2.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "json_mode": "json_object", "key_env": "GEMINI_API_KEY"},
    "deepseek": {"label": L("DeepSeek", "DeepSeek"), "provider": "openai",
                 "model": "deepseek-chat", "base_url": "https://api.deepseek.com", "json_mode": "json_object"},
    "qwen": {"label": L("通义千问（阿里云百炼）", "Qwen (Alibaba DashScope)"), "provider": "openai",
             "model": "qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "json_mode": "json_object"},
    "kimi": {"label": L("Kimi（月之暗面）", "Kimi (Moonshot)"), "provider": "openai",
             "model": "", "base_url": "https://api.moonshot.cn/v1", "json_mode": "json_object"},
    "zhipu": {"label": L("智谱 GLM", "Zhipu GLM"), "provider": "openai",
              "model": "", "base_url": "https://open.bigmodel.cn/api/paas/v4/", "json_mode": "json_object"},
    "ollama": {"label": L("本地 Ollama（免费，无需 key）", "Local Ollama (free, no key)"), "provider": "openai",
               "model": "qwen2.5:7b", "base_url": "http://localhost:11434/v1", "json_mode": "json_object"},
    "compatible": {"label": L("其他兼容 OpenAI 的服务", "Other OpenAI-compatible service"), "provider": "openai",
                   "model": "", "base_url": "", "json_mode": "json_object"},
    "litellm": {"label": L("LiteLLM", "LiteLLM"), "provider": "litellm",
                "model": "", "base_url": "", "json_mode": "json_object"},
}


@dataclass
class LLMConfig:
    preset: str = "claude"
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    api_key: str = ""
    base_url: str = ""
    json_mode: str = "schema"
    timeout: float = 120.0
    refusal_fallback: bool = True      # 仅 Claude：模型拒答时由服务端自动换模型重试

    @property
    def ready(self) -> bool:
        return bool(self.provider and self.model)

    @classmethod
    def from_preset(cls, key: str, **overrides) -> "LLMConfig":
        p = PRESETS[key]
        base = {"preset": key, "provider": p["provider"], "model": p["model"], "base_url": p["base_url"],
                "json_mode": p["json_mode"]}
        return cls(**{**base, **overrides})


def migrate_legacy(path: Path = CONFIG_PATH, legacy: Path = LEGACY_DIR / "llm.json") -> bool:
    """新位置还没有设置、旧位置有时，复制过来（旧文件保留，源码版还能用）"""
    if path.exists() or path == legacy or not legacy.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(legacy.read_bytes())
    return True


def load_config(path: Path = CONFIG_PATH) -> LLMConfig | None:
    if path == CONFIG_PATH:
        try:
            migrate_legacy(path)
        except OSError:
            pass
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    known = {f.name for f in fields(LLMConfig)}
    return LLMConfig(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: LLMConfig, path: Path = CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
