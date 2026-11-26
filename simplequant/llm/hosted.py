"""
SimpleQuant 云端托管服务客户端（AI 转发 + R2 云端离线数据包）

服务端在 SimpleQuant-site/worker（默认 https://api.simplequant.org，可用环境变量
SIMPLEQUANT_HOSTED_URL 覆盖，便于本地测试）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from ..i18n import L, pick
from ..paths import CACHE_DIR
from .config import CONFIG_DIR

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

HOSTED_API_ROOT = os.environ.get("SIMPLEQUANT_HOSTED_URL", "https://api.simplequant.org").rstrip("/")
HOSTED_AI_BASE = HOSTED_API_ROOT if HOSTED_API_ROOT.endswith("/v1/ai") else f"{HOSTED_API_ROOT}/v1/ai"


class HostedError(RuntimeError):
    """云端托管服务调用失败（鉴权失败、额度用尽、网络异常等）。"""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def api_root() -> str:
    env = os.environ.get("SIMPLEQUANT_HOSTED_URL", "").strip().rstrip("/")
    base = env or HOSTED_API_ROOT
    if base.endswith("/v1/ai"):
        base = base[:-6]
    return base


def ai_base_url() -> str:
    return f"{api_root()}/v1/ai"


def device_id(config_dir: Path | None = None, telemetry_path: Path | None = None) -> str:
    """返回本机稳定的匿名设备编号（uuid4），用于新机免费体验计数。"""
    cdir = config_dir or CONFIG_DIR
    id_file = cdir / "device_id"
    try:
        saved = id_file.read_text(encoding="utf-8").strip().lower()
        if _UUID4_RE.match(saved):
            return saved
    except OSError:
        pass

    # 若匿名统计已生成过 install_id，则优先复用，保持同机一致
    tpath = telemetry_path or (CACHE_DIR / "telemetry.json")
    found = ""
    try:
        data = json.loads(tpath.read_text(encoding="utf-8"))
        cand = str(data.get("install_id", "")).strip().lower()
        if _UUID4_RE.match(cand):
            found = cand
    except (OSError, ValueError, AttributeError):
        pass

    val = found or str(uuid.uuid4())
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        id_file.write_text(val, encoding="utf-8")
    except OSError:
        pass
    return val


def hosted_token(api_key: str = "") -> str:
    """解析托管接口凭证：已填额度码则用额度码，留空则使用本机免费体验凭证。"""
    code = (api_key or "").strip()
    if code:
        return code
    return f"trial:{device_id()}"


def _parse_err_body(raw: bytes) -> tuple[str, str]:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("code", "")), str(err.get("message", ""))
    except ValueError:
        pass
    return "", ""


def _friendly_error(status: int, code: str, server_msg: str, lang: str = "zh") -> str:
    if code == "trial_exhausted":
        return pick(L(
            "当前设备的免费体验次数已用完，请前往「设置」填入托管额度码，或切换为自有 API Key。",
            "Free trial quota for this device has been exhausted. Enter a hosted voucher code in Settings or switch to your own API key.",
        ), lang)
    if code == "quota_exhausted":
        return pick(L(
            "当前额度码的 AI 使用次数已用完，请前往「设置」更换额度码，或切换为自有 API Key。",
            "AI quota for this voucher code has been exhausted. Enter a new voucher code in Settings or switch to your own API key.",
        ), lang)
    if code == "ai_not_included":
        return pick(L(
            "当前额度码不包含 AI 托管额度，请使用含 AI 额度的额度码，或切换为自有 API Key。",
            "This voucher code does not include AI hosting quota. Use a voucher with AI quota or switch to your own API key.",
        ), lang)
    if code == "pack_not_included":
        return pick(L(
            "当前额度码不包含云端数据包下载权限。",
            "This voucher code does not include cloud data pack download permission.",
        ), lang)
    if code == "pack_exhausted":
        return pick(L(
            "当前额度码的云端数据包下载次数已用完。",
            "Cloud data pack download quota for this voucher code has been exhausted.",
        ), lang)
    if code == "invalid_voucher" or status == 401:
        return pick(L(
            "额度码无效，请核对后重试。",
            "Invalid voucher code. Please verify and try again.",
        ), lang)
    if code == "pack_not_found" or status == 404:
        return pick(L(
            "云端暂未提供该数据包，本次未扣除额度。",
            "This cloud data pack is not yet available; no quota was deducted.",
        ), lang)
    if status == 429:
        return pick(L("请求过于频繁，请稍后再试。", "Too many requests. Please try again later."), lang)
    if server_msg:
        return server_msg
    return pick(L(f"云端服务返回异常（HTTP {status}）。", f"Cloud service returned an error (HTTP {status})."), lang)


def query_quota(api_key: str = "", timeout: float = 10.0, lang: str = "zh") -> dict:
    """查询当前托管凭证（免费体验或额度码）的剩余次数。"""
    token = hosted_token(api_key)
    req = urllib.request.Request(
        f"{api_root()}/v1/ai/quota",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "SimpleQuant"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code, msg = _parse_err_body(e.read())
        raise HostedError(_friendly_error(e.code, code, msg, lang), status=e.code, code=code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HostedError(pick(L(f"无法连接云端托管服务：{e}", f"Unable to reach hosted service: {e}"), lang)) from e


def list_cloud_packs(timeout: float = 10.0, lang: str = "zh") -> dict:
    """查询云端离线数据包可用状态、体积与更新时间。"""
    req = urllib.request.Request(
        f"{api_root()}/v1/pack/list",
        headers={"User-Agent": "SimpleQuant"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("packs", {}) if isinstance(data, dict) else {}
    except urllib.error.HTTPError as e:
        code, msg = _parse_err_body(e.read())
        raise HostedError(_friendly_error(e.code, code, msg, lang), status=e.code, code=code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HostedError(pick(L(f"无法连接云端数据包服务：{e}", f"Unable to reach cloud data pack service: {e}"), lang)) from e


def _pack_state_path(cache_dir: Path | None = None) -> Path:
    return (cache_dir or CACHE_DIR) / "cloud_packs.json"


def get_imported_cloud_pack(pack: str, cache_dir: Path | None = None) -> dict | None:
    """读取本机已导入的云端数据包版本记录（用于避免同版本重复下载）。"""
    path = _pack_state_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = data.get(pack) if isinstance(data, dict) else None
        return dict(rec) if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def record_imported_cloud_pack(
    pack: str,
    version: str = "",
    uploaded: str = "",
    cache_dir: Path | None = None,
) -> None:
    """记录本机已导入的云端数据包版本信息。"""
    import datetime as dt
    path = _pack_state_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[pack] = {
        "version": (version or "").strip(),
        "uploaded": (uploaded or "").strip(),
        "imported_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def download_cloud_pack(
    code: str,
    pack: str,
    dest: Path,
    on_progress: Callable[[int, int], None] | None = None,
    timeout: float = 120.0,
    lang: str = "zh",
    meta_out: dict | None = None,
) -> tuple[Path, int | None]:
    """凭额度码从云端 R2 下载数据包并保存到 dest，返回 (dest, 剩余下载次数)。"""
    code = (code or "").strip()
    if not code:
        raise HostedError(pick(L("请先输入托管额度码。", "Please enter a hosted voucher code first."), lang), status=401)
    payload = json.dumps({"code": code, "pack": pack}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_root()}/v1/pack/download",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {code}",
            "User-Agent": "SimpleQuant",
        },
        method="POST",
    )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            left_hdr = resp.headers.get("X-SQ-Pack-Left")
            pack_left = int(left_hdr) if left_hdr and left_hdr.isdigit() else None
            if meta_out is not None:
                ded_hdr = resp.headers.get("X-SQ-Pack-Deducted")
                ver_hdr = (resp.headers.get("X-SQ-Pack-Version") or "").strip()
                meta_out["deducted"] = (ded_hdr != "0") if ded_hdr is not None else True
                meta_out["version"] = ver_hdr
            done = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        os.replace(tmp, dest)
        return dest, pack_left
    except urllib.error.HTTPError as e:
        err_code, msg = _parse_err_body(e.read())
        raise HostedError(_friendly_error(e.code, err_code, msg, lang), status=e.code, code=err_code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HostedError(pick(L(f"云端数据包下载失败：{e}", f"Cloud data pack download failed: {e}"), lang)) from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
