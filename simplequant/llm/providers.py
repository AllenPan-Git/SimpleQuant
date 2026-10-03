"""
大模型接口适配：Claude（anthropic SDK）、OpenAI 及兼容接口（openai SDK）、LiteLLM
三者对外统一为 provider.chat(system, messages, schema) -> str：
    schema 为 None 时返回普通文本；否则返回 JSON 文本（由 extract_json 解析）
"""

import json
import os
import re

from .config import LLMConfig, PRESETS

# 支持服务端拒答自动换模型（fallbacks="default"）的 Claude 模型
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """从模型回复中取出 JSON 对象（兼容 ```json 代码块和前后多余文字）"""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"model reply contains no JSON / 模型回复中没有 JSON: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON from model / 模型返回的 JSON 无效: {e}") from e


def schema_hint(schema: dict) -> str:
    """不支持服务端 JSON Schema 时，把结构写进提示词"""
    return ("\n\nReturn ONLY a JSON object (no prose, no code fences) that matches this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False))


class Provider:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def chat(self, system: str, messages: list[dict], schema: dict | None = None) -> str:
        raise NotImplementedError


# ---------------- Claude ----------------
class AnthropicProvider(Provider):
    def build_request(self, system: str, messages: list[dict], schema: dict | None) -> dict:
        kw = {"model": self.cfg.model, "max_tokens": 16000, "system": system, "messages": messages}
        if schema is not None:
            if self.cfg.json_mode == "schema":
                kw["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
            else:
                kw["system"] = system + schema_hint(schema)
        if self.use_fallback:
            kw["betas"] = [_FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    @property
    def use_fallback(self) -> bool:
        # 仅官方地址 + 支持的模型；代理或其它平台不带这个参数
        return self.cfg.refusal_fallback and not self.cfg.base_url and self.cfg.model in _FALLBACK_MODELS

    def client(self, http_client=None):
        import anthropic
        return anthropic.Anthropic(api_key=self.cfg.api_key or None, base_url=self.cfg.base_url or None,
                                   timeout=self.cfg.timeout, http_client=http_client)

    def chat(self, system, messages, schema=None, http_client=None):
        import anthropic

        client = self.client(http_client)
        kw = self.build_request(system, messages, schema)
        try:
            if "betas" in kw:
                try:
                    resp = client.beta.messages.create(**kw)
                except anthropic.BadRequestError:
                    # 个别账户/模型不接受 fallbacks：去掉后重试一次
                    kw.pop("betas"), kw.pop("fallbacks")
                    resp = client.messages.create(**kw)
            else:
                resp = client.messages.create(**kw)
        except anthropic.AuthenticationError as e:
            raise LLMError(f"API key invalid / API key 无效: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited, try again later / 请求过于频繁，请稍后再试: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"cannot reach Claude API / 无法连接 Claude 接口: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request / 模型拒绝了这个请求")
        if resp.stop_reason == "max_tokens":
            raise LLMError("reply was cut off (max_tokens) / 回复被截断")
        return "".join(b.text for b in resp.content if b.type == "text")


# ---------------- OpenAI 及兼容接口 ----------------
def _openai_style_kwargs(cfg: LLMConfig, system: str, messages: list[dict], schema: dict | None) -> dict:
    """OpenAI 与 LiteLLM 共用的请求参数（两者都是 chat.completions 格式）"""
    sys_text = system
    kw = {}
    if schema is not None:
        if cfg.json_mode == "schema":
            kw["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": "strategy", "schema": schema, "strict": True}}
        else:
            sys_text = system + schema_hint(schema)
            if cfg.json_mode == "json_object":
                kw["response_format"] = {"type": "json_object"}
    kw["messages"] = [{"role": "system", "content": sys_text}] + messages
    kw["model"] = cfg.model
    return kw


class OpenAIProvider(Provider):
    def client(self, http_client=None):
        from openai import OpenAI
        key = self.cfg.api_key or None
        env = PRESETS.get(self.cfg.preset, {}).get("key_env")
        if key is None and env and env != "OPENAI_API_KEY":
            key = os.environ.get(env) or None     # 如 Gemini 的 GEMINI_API_KEY（OpenAI SDK 只会读 OPENAI_API_KEY）
        if key is None and self.cfg.base_url and "localhost" in self.cfg.base_url:
            key = "ollama"   # 本地服务不校验 key，但 SDK 要求非空
        return OpenAI(api_key=key, base_url=self.cfg.base_url or None, timeout=self.cfg.timeout,
                      http_client=http_client)

    def chat(self, system, messages, schema=None, http_client=None):
        import openai

        try:
            resp = self.client(http_client).chat.completions.create(
                **_openai_style_kwargs(self.cfg, system, messages, schema))
        except openai.AuthenticationError as e:
            raise LLMError(f"API key invalid / API key 无效: {e}") from e
        except openai.RateLimitError as e:
            raise LLMError(f"rate limited, try again later / 请求过于频繁，请稍后再试: {e}") from e
        except openai.BadRequestError as e:
            hint = (" (the service may not support JSON Schema; switch the output mode to 'JSON mode' / "
                    "服务可能不支持 JSON Schema，请在设置里改为「JSON 模式」)") if self.cfg.json_mode == "schema" else ""
            raise LLMError(f"request rejected / 请求被拒绝: {e}{hint}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"cannot reach {self.cfg.base_url or 'OpenAI'} / 无法连接: {e}") from e
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("reply was cut off / 回复被截断")
        return choice.message.content or ""


# ---------------- LiteLLM ----------------
class LiteLLMProvider(Provider):
    def chat(self, system, messages, schema=None, **extra):
        try:
            import litellm
        except ImportError as e:   # 打包版不含 LiteLLM（体积太大）；源码版是可选依赖，默认不安装
            raise LLMError("LiteLLM is not installed (source version: pip install litellm -c constraints.txt); "
                           "otherwise use an OpenAI-compatible preset / "
                           "未安装 LiteLLM（源码版可执行 pip install litellm -c constraints.txt），"
                           "或改用「OpenAI / 兼容 OpenAI 的接口」类服务商") from e

        kw = _openai_style_kwargs(self.cfg, system, messages, schema)
        if self.cfg.api_key:
            kw["api_key"] = self.cfg.api_key
        if self.cfg.base_url:
            kw["api_base"] = self.cfg.base_url
        try:
            resp = litellm.completion(timeout=self.cfg.timeout, **kw, **extra)
        except Exception as e:  # noqa: BLE001 - LiteLLM 会把各家异常映射成自己的类型，这里统一包装
            raise LLMError(f"LiteLLM error: {e}") from e
        return resp.choices[0].message.content or ""


def get_provider(cfg: LLMConfig) -> Provider:
    return {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "litellm": LiteLLMProvider}[cfg.provider](cfg)
