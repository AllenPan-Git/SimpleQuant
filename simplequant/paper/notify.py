"""
模拟盘「待执行信号」与异常推送通知（纯标准库，无新增第三方依赖）

支持三类渠道：
- 邮件通知（通过 api.simplequant.org 经 Resend 发送研报风邮件；填 QQ 邮箱可配合微信「QQ 邮箱提醒」收弹窗）
- 系统原生桌面通知（Windows WinRT Toast / macOS osascript / Linux notify-send）
- 其他推送方式（PushPlus / Server酱 / 企业微信 / 飞书 / 钉钉 / Bark / 通用 JSON Webhook）

配置文件保存于 <PAPER_ROOT>/notify.json，去重状态保存于 <PAPER_ROOT>/notify_state.json。
默认仅在定时任务（SimpleQuant --paper）推进完成时触发，任何发送异常仅写日志，不阻塞主流程。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html as _html
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from ..system import SYSTEM
from . import account as _account

DEFAULT_API_BASE = "https://api.simplequant.org"
HTTP_TIMEOUT = 12
MAX_SIGNALS_PER_ACCOUNT = 12


def api_base() -> str:
    return (os.environ.get("SIMPLEQUANT_NOTIFY_URL") or DEFAULT_API_BASE).rstrip("/")


@dataclass
class NotifyConfig:
    desktop: bool = False
    email_enabled: bool = False
    email_token: str = ""
    email_address: str = ""
    webhook_enabled: bool = False
    webhook_kind: str = "auto"      # auto / pushplus / serverchan / wecom / feishu / dingtalk / bark / generic
    webhook_url: str = ""
    webhook_secret: str = ""
    mode: str = "signals"           # signals（仅在有信号或异常时）/ always（每日均推送）

    @property
    def any_enabled(self) -> bool:
        return bool(
            self.desktop
            or (self.email_enabled and self.email_token.strip())
            or (self.webhook_enabled and self.webhook_url.strip())
        )


def mask_email(addr: str) -> str:
    s = (addr or "").strip()
    if "@" not in s:
        return s
    name, domain = s.split("@", 1)
    if len(name) <= 2:
        masked = name[:1] + "***"
    else:
        masked = name[:2] + "***" + name[-1:]
    return f"{masked}@{domain}"


def config_path(root: Path | None = None) -> Path:
    return (root or _account.PAPER_ROOT) / "notify.json"


def state_path(root: Path | None = None) -> Path:
    return (root or _account.PAPER_ROOT) / "notify_state.json"


def load_config(root: Path | None = None) -> NotifyConfig:
    p = config_path(root)
    if not p.exists():
        return NotifyConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return NotifyConfig()
        fields = {f: raw[f] for f in NotifyConfig.__dataclass_fields__ if f in raw}
        return NotifyConfig(**fields)
    except (OSError, json.JSONDecodeError, TypeError):
        return NotifyConfig()


def save_config(cfg: NotifyConfig, root: Path | None = None) -> None:
    p = config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(root: Path | None = None) -> dict:
    p = state_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(day: str, digest: str, root: Path | None = None) -> None:
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": day, "digest": digest}, ensure_ascii=False), encoding="utf-8")


# ---------------- HTTP 辅助 ----------------

def _http_json(method: str, url: str, payload: dict | None = None,
               timeout: int = HTTP_TIMEOUT) -> tuple[int, dict, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "SimpleQuant"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            return resp.status, data if isinstance(data, dict) else {}, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        return e.code, data if isinstance(data, dict) else {}, raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {}, str(e)


def _err_msg(data: dict, fallback: str) -> str:
    err = data.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if data.get("msg"):
        return str(data["msg"])
    if data.get("errmsg"):
        return str(data["errmsg"])
    if data.get("message"):
        return str(data["message"])
    return fallback


# ---------------- 邮件推送（Resend via api.simplequant.org） ----------------

def mail_request_code(email: str) -> tuple[bool, str]:
    addr = (email or "").strip()
    if not addr:
        return False, "请输入有效的邮箱地址。"
    status, data, raw = _http_json("POST", f"{api_base()}/v1/mail/request_code", {"email": addr})
    if status == 200 and data.get("ok"):
        return True, ""
    return False, _err_msg(data, raw or f"HTTP {status}")


def mail_verify(email: str, code: str) -> tuple[bool, dict | str]:
    addr = (email or "").strip()
    c = (code or "").strip()
    status, data, raw = _http_json("POST", f"{api_base()}/v1/mail/verify", {"email": addr, "code": c})
    if status == 200 and data.get("token"):
        return True, data
    return False, _err_msg(data, raw or f"HTTP {status}")


def mail_unbind(token: str) -> tuple[bool, str]:
    tok = token.strip()
    if not tok:
        return True, ""
    status, data, raw = _http_json("POST", f"{api_base()}/v1/mail/unbind", {"token": tok})
    if status in (200, 204):
        return True, ""
    return False, _err_msg(data, raw or f"HTTP {status}")


def send_email(token: str, subject: str, text: str, html: str = "") -> tuple[bool, str]:
    tok = token.strip()
    if not tok:
        return False, "尚未绑定接收邮箱"
    status, data, raw = _http_json("POST", f"{api_base()}/v1/mail/send", {
        "token": tok,
        "subject": subject[:140],
        "text": text[:8000],
        "html": html[:24000],
    })
    if status == 200 and data.get("ok"):
        return True, ""
    return False, _err_msg(data, raw or f"HTTP {status}")


# ---------------- 系统原生桌面通知 ----------------

def send_desktop(title: str, body: str) -> tuple[bool, str]:
    """跨平台系统原生桌面通知（零第三方依赖）"""
    title_s = (title or "SimpleQuant").strip()[:120]
    body_s = (body or "").strip()[:300]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if SYSTEM == "windows":
            t_xml = _xml_escape(title_s)
            b_xml = _xml_escape(body_s)
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
                f"$xml = '<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{t_xml}</text><text id=\"2\">{b_xml}</text></binding></visual></toast>';"
                "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
                "$doc.LoadXml($xml);"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SimpleQuant').Show($toast)"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                               capture_output=True, text=True, timeout=15, creationflags=flags)
            return (True, "") if r.returncode == 0 else (False, (r.stderr or r.stdout or "Toast failed").strip())
        if SYSTEM == "macos":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            script = f'display notification "{esc(body_s)}" with title "{esc(title_s)}"'
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
            return (True, "") if r.returncode == 0 else (False, (r.stderr or "osascript failed").strip())
        if shutil.which("notify-send"):
            r = subprocess.run(["notify-send", title_s, body_s], capture_output=True, text=True, timeout=10)
            return (True, "") if r.returncode == 0 else (False, (r.stderr or "notify-send failed").strip())
        return False, "notify-send not found"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


# ---------------- 其他推送方式（PushPlus / Server酱 / Webhook） ----------------

def detect_webhook_kind(url: str) -> str:
    s = url.strip()
    if s.startswith("SCT") and "://" not in s:
        return "serverchan"
    host = urllib.parse.urlparse(s).netloc.lower()
    if "pushplus.plus" in host:
        return "pushplus"
    if "sctapi.ftqq.com" in host:
        return "serverchan"
    if "qyapi.weixin.qq.com" in host:
        return "wecom"
    if "open.feishu.cn" in host or "open.larksuite.com" in host:
        return "feishu"
    if "oapi.dingtalk.com" in host:
        return "dingtalk"
    if "api.day.app" in host:
        return "bark"
    return "generic"


def send_webhook(cfg: NotifyConfig, title: str, markdown: str) -> tuple[bool, str]:
    raw_url = cfg.webhook_url.strip()
    if not raw_url:
        return False, "未填写推送地址或 Token"
    kind = detect_webhook_kind(raw_url) if cfg.webhook_kind == "auto" else cfg.webhook_kind
    secret = cfg.webhook_secret.strip()
    url = raw_url

    if kind == "pushplus":
        token = raw_url
        if "://" in raw_url:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
            token = (qs.get("token") or [""])[0]
        url = "https://www.pushplus.plus/send"
        payload = {"token": token, "title": title[:100], "content": markdown[:8000], "template": "markdown"}
    elif kind == "serverchan":
        if "://" not in raw_url:
            url = f"https://sctapi.ftqq.com/{raw_url}.send"
        payload = {"title": title[:100], "desp": markdown[:8000]}
    elif kind == "wecom":
        payload = {"msgtype": "markdown", "markdown": {"content": markdown[:4000]}}
    elif kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{markdown}"[:4000]}}
        if secret:
            ts = str(int(time.time()))
            sign = base64.b64encode(
                hmac.new(f"{ts}\n{secret}".encode("utf-8"), digestmod=hashlib.sha256).digest()
            ).decode()
            payload["timestamp"] = ts
            payload["sign"] = sign
    elif kind == "dingtalk":
        if secret:
            ts = str(int(time.time() * 1000))
            sign = urllib.parse.quote_plus(base64.b64encode(
                hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"), hashlib.sha256).digest()
            ).decode())
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={sign}"
        payload = {"msgtype": "markdown", "markdown": {"title": title[:64], "text": markdown[:4000]}}
    elif kind == "bark":
        payload = {"title": title[:80], "body": markdown[:2000]}
    else:
        payload = {"title": title, "content": markdown, "date": dt.date.today().isoformat()}

    status, data, raw = _http_json("POST", url, payload)
    if status < 200 or status >= 300:
        return False, _err_msg(data, raw or f"HTTP {status}")
    for code_key in ("errcode", "code"):
        if code_key in data and data[code_key] not in (0, 200, 1000, None):
            return False, _err_msg(data, f"{code_key}={data[code_key]}")
    return True, ""


# ---------------- 报告组装与研报风 HTML 邮件 ----------------

def _valid_signals(state: dict) -> list[dict]:
    out = []
    for s in state.get("signals") or []:
        lot = int(s.get("lot") or 100)
        shares = int(float(s.get("size") or 0) // lot) * lot
        if shares > 0:
            out.append({**s, "_shares": shares})
    return out


def _short_symbol(sym: str) -> str:
    return sym.split(".")[-1] if "." in sym else sym


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"


def _pct_color(v: float | None) -> str:
    if v is None or abs(v) < 1e-9:
        return "#4A4640"
    return "#B83B2A" if v > 0 else "#2A6B4E"


def _account_perf(acc, st: dict) -> dict | None:
    """提取账户单位净值、累计收益、今日涨跌与同期基准（全部采用比例口径，不暴露绝对资金额）"""
    if not st:
        return None
    try:
        nav = acc.nav()
    except Exception:  # noqa: BLE001
        nav = None

    value = float(st.get("value") or 0.0)
    cash0 = float((acc.broker or {}).get("cash") or 0.0)
    metrics = st.get("metrics") or {}
    dca = nav is not None and not nav.empty and "invested" in nav.columns
    base = float(nav["invested"].iloc[-1]) if dca and float(nav["invested"].iloc[-1]) > 0 else cash0
    unit_nav = (value / base) if base > 0 and value > 0 else None
    total_ret = (unit_nav - 1.0) if unit_nav is not None else metrics.get("total_return")

    day_ret = None
    if nav is not None and len(nav) >= 2:
        prev_v = float(nav["value"].iloc[-2])
        if prev_v > 0:
            pnl = float(nav["value"].iloc[-1] - prev_v)
            if dca:
                pnl -= float(nav["invested"].iloc[-1] - nav["invested"].iloc[-2])
            day_ret = pnl / prev_v

    bench_ret = metrics.get("benchmark_return")
    bench_day = None
    if nav is not None and not nav.empty and "benchmark" in nav.columns:
        b = nav["benchmark"].dropna()
        b = b[b > 0]
        if bench_ret is None and len(b) >= 2:
            bench_ret = float(b.iloc[-1] / b.iloc[0] - 1.0)
        if not dca and len(b) >= 2:
            bench_day = float(b.iloc[-1] / b.iloc[-2] - 1.0)

    if unit_nav is None and total_ret is None and day_ret is None and bench_ret is None:
        return None
    return {
        "as_of": str(st.get("as_of", "")),
        "unit_nav": unit_nav,
        "total_ret": float(total_ret) if total_ret is not None else None,
        "day_ret": float(day_ret) if day_ret is not None else None,
        "bench_ret": float(bench_ret) if bench_ret is not None else None,
        "bench_day": float(bench_day) if bench_day is not None else None,
    }


def _perf_line_md(perf: dict | None) -> str:
    if not perf:
        return ""
    segs = []
    if perf["unit_nav"] is not None:
        segs.append(f"净值 `{perf['unit_nav']:.3f}`")
    if perf["total_ret"] is not None:
        segs.append(f"累计 `{_fmt_pct(perf['total_ret'])}`")
    if perf["day_ret"] is not None:
        segs.append(f"今日 `{_fmt_pct(perf['day_ret'])}`")
    if perf["bench_ret"] is not None:
        b_txt = f"基准 `{_fmt_pct(perf['bench_ret'])}`"
        if perf["bench_day"] is not None:
            b_txt += f"（今日 `{_fmt_pct(perf['bench_day'])}`）"
        segs.append(b_txt)
    return " · ".join(segs)


def _render_email_html(title: str, summary: str, signal_blocks: list[dict],
                       overview_rows: list[dict], error_items: list[tuple[str, str]]) -> str:
    e = _html.escape
    parts = [
        '<div style="background:#FAF8F4;padding:24px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1C1B19;">',
        '<div style="max-width:600px;margin:0 auto;background:#FFFFFF;border:1px solid #E5E0D5;border-top:3px solid #B83B2A;padding:24px 28px;">',
        '<div style="font-size:11px;letter-spacing:1.5px;color:#B83B2A;font-weight:600;">SIMPLEQUANT · 模拟盘简报</div>',
        f'<h2 style="margin:8px 0 10px;font-size:20px;font-weight:600;">{e(title)}</h2>',
        f'<p style="font-size:14px;line-height:1.65;color:#4A4640;margin:0 0 18px;">{e(summary)}</p>',
    ]
    for blk in signal_blocks:
        parts.append('<div style="border-top:1px solid #E5E0D5;padding-top:14px;margin-top:14px;">')
        parts.append(f'<div style="font-size:15px;font-weight:600;margin-bottom:4px;">{e(blk["name"])}</div>')
        parts.append(
            f'<div style="font-size:12px;color:#8A847B;margin-bottom:6px;">'
            f'数据截至 {e(blk["as_of"])} · 计划执行 {e(blk["execute_on"])}</div>'
        )
        pf = blk.get("perf")
        if pf:
            nav_s = f"{pf['unit_nav']:.3f}" if pf["unit_nav"] is not None else "—"
            b_sub = f"（今日 {_fmt_pct(pf['bench_day'])}）" if pf["bench_day"] is not None else ""
            parts.append(
                '<div style="font-size:12px;color:#4A4640;background:#FAF8F4;border:1px solid #EFECE6;padding:6px 10px;margin-bottom:8px;">'
                f'净值 <strong style="font-family:Georgia,serif;">{nav_s}</strong> · '
                f'累计 <strong style="color:{_pct_color(pf["total_ret"])};font-family:Georgia,serif;">{_fmt_pct(pf["total_ret"])}</strong> · '
                f'今日 <strong style="color:{_pct_color(pf["day_ret"])};font-family:Georgia,serif;">{_fmt_pct(pf["day_ret"])}</strong> · '
                f'基准 <span style="color:{_pct_color(pf["bench_ret"])};font-family:Georgia,serif;">{_fmt_pct(pf["bench_ret"])}</span>'
                f'<span style="color:#8A847B;">{e(b_sub)}</span>'
                '</div>'
            )
        parts.append(
            f'<div style="font-size:13px;color:#1C1B19;">已生成 <strong>{blk["total"]}</strong> 笔模拟委托记录，'
            '请打开本机 SimpleQuant 模拟盘页查看明细。</div>'
        )
        parts.append('</div>')

    if overview_rows:
        parts.append('<div style="border-top:1px solid #E5E0D5;padding-top:14px;margin-top:14px;">')
        parts.append('<div style="font-size:14px;font-weight:600;margin-bottom:8px;">账户净值与基准概况</div>')
        parts.append(
            '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            '<tr style="border-bottom:1px solid #E5E0D5;color:#8A847B;text-align:left;">'
            '<th style="padding:6px 4px;font-weight:500;">账户</th>'
            '<th style="padding:6px 4px;text-align:right;font-weight:500;">净值</th>'
            '<th style="padding:6px 4px;text-align:right;font-weight:500;">今日涨跌</th>'
            '<th style="padding:6px 4px;text-align:right;font-weight:500;">累计收益</th>'
            '<th style="padding:6px 4px;text-align:right;font-weight:500;">同期基准</th>'
            '</tr>'
        )
        for ov in overview_rows:
            pf = ov["perf"]
            nav_s = f"{pf['unit_nav']:.3f}" if pf["unit_nav"] is not None else "—"
            parts.append(
                '<tr style="border-bottom:1px solid #F2EFE9;">'
                f'<td style="padding:6px 4px;font-weight:500;">{e(ov["name"])}</td>'
                f'<td style="padding:6px 4px;text-align:right;font-family:Georgia,serif;">{nav_s}</td>'
                f'<td style="padding:6px 4px;text-align:right;font-family:Georgia,serif;color:{_pct_color(pf["day_ret"])};">{_fmt_pct(pf["day_ret"])}</td>'
                f'<td style="padding:6px 4px;text-align:right;font-family:Georgia,serif;color:{_pct_color(pf["total_ret"])};">{_fmt_pct(pf["total_ret"])}</td>'
                f'<td style="padding:6px 4px;text-align:right;font-family:Georgia,serif;color:{_pct_color(pf["bench_ret"])};">{_fmt_pct(pf["bench_ret"])}</td>'
                '</tr>'
            )
        parts.append('</table></div>')

    if error_items:
        parts.append('<div style="border-top:1px solid #E5E0D5;padding-top:14px;margin-top:14px;">')
        parts.append('<div style="font-size:14px;font-weight:600;color:#B83B2A;margin-bottom:6px;">运行异常</div>')
        for name, msg in error_items:
            parts.append(f'<div style="font-size:13px;color:#4A4640;margin:4px 0;"><strong>{e(name)}</strong>：{e(msg)}</div>')
        parts.append('</div>')

    parts.append(
        '<p style="font-size:12px;color:#8A847B;margin:20px 0 0;border-top:1px solid #EFECE6;padding-top:12px;">'
        '以上内容由本机模拟账户按使用者自设策略自动生成，仅供研究记录，不构成任何投资建议。'
        '如需停止接收，请在 SimpleQuant 模拟盘页解除邮箱绑定。</p>'
        '</div></div>'
    )
    return "".join(parts)


def build_report(accounts: list, results: dict[str, str], *, today: str | None = None) -> dict:
    """汇总全部非暂停模拟账户的待执行信号、净值/涨跌/基准与运行异常，生成推送正文与去重指纹"""
    day = today or dt.date.today().isoformat()
    active = [a for a in accounts if getattr(a, "status", "active") != "paused"]
    signal_sections: list[str] = []
    signal_blocks: list[dict] = []
    overview_rows: list[dict] = []
    overview_md_lines: list[str] = []
    error_lines: list[str] = []
    error_items: list[tuple[str, str]] = []
    digest_parts: list[str] = []
    total_signals = 0
    accounts_with_signals = 0

    for acc in active:
        err = results.get(acc.id) or ""
        if err:
            short_err = err[:160]
            error_lines.append(f"- **{acc.name}**：{short_err}")
            error_items.append((acc.name, short_err))
            digest_parts.append(f"ERR:{acc.id}:{err[:120]}")
            continue
        st = acc.state()
        if not st:
            continue
        perf = _account_perf(acc, st)
        if perf:
            overview_rows.append({"name": acc.name, "perf": perf})
            overview_md_lines.append(f"- **{acc.name}**：{_perf_line_md(perf)}")
        sigs = _valid_signals(st)
        if not sigs:
            continue
        accounts_with_signals += 1
        total_signals += len(sigs)
        as_of = str(st.get("as_of", ""))
        exec_on = str(st.get("execute_on", ""))
        lines = [
            f"### {acc.name}",
            f"数据截至 `{as_of}` · 计划执行 `{exec_on}`",
        ]
        if perf:
            lines.append(_perf_line_md(perf))
        lines.append(f"已生成 **{len(sigs)}** 笔模拟委托记录，请打开本机 SimpleQuant 模拟盘页查看明细。")
        for s in sigs:
            side = "buy" if s.get("side") == "buy" else "sell"
            code = _short_symbol(str(s.get("symbol", "")))
            digest_parts.append(f"SIG:{acc.id}:{as_of}:{exec_on}:{side}:{code}:{s['_shares']}")
        signal_sections.append("\n".join(lines))
        signal_blocks.append({"name": acc.name, "as_of": as_of, "execute_on": exec_on,
                              "total": len(sigs), "perf": perf})

    has_signals = total_signals > 0
    has_errors = bool(error_lines)

    if has_signals and has_errors:
        title = f"SimpleQuant 模拟盘更新与异常 · {day}"
        summary = f"{accounts_with_signals} 个模拟账户生成共 {total_signals} 笔模拟委托记录，另有 {len(error_lines)} 个账户更新失败。"
    elif has_signals:
        title = f"SimpleQuant 模拟盘更新简报 · {day}"
        summary = f"{accounts_with_signals} 个模拟账户已生成下一交易日模拟委托记录（共 {total_signals} 笔）。"
    elif has_errors:
        title = f"SimpleQuant 模拟盘运行异常 · {day}"
        summary = f"{len(error_lines)} 个模拟账户更新失败，请查看运行日志。"
    else:
        title = f"SimpleQuant 模拟盘每日简报 · {day}"
        summary = f"全部 {len(active)} 个运行中模拟账户已更新完成，当日无新增模拟委托记录。"
        as_ofs = ",".join(sorted({r["perf"]["as_of"] for r in overview_rows if r["perf"].get("as_of")}))
        digest_parts.append(f"IDLE:{day}:{len(active)}:{as_ofs}")

    blocks = [f"## {title}"]
    if signal_sections:
        blocks.extend(signal_sections)
    if overview_md_lines:
        blocks.append("### 账户净值与基准概况\n" + "\n".join(overview_md_lines))
    if error_lines:
        blocks.append("### 运行异常\n" + "\n".join(error_lines))
    if not signal_sections and not overview_md_lines and not error_lines:
        blocks.append(summary)
    blocks.append("---\n*以上内容由本机模拟账户按使用者自设策略自动生成，仅供研究记录，不构成任何投资建议。如需停止接收，请在 SimpleQuant 模拟盘页解除绑定。*")

    digest = hashlib.sha256("\n".join(sorted(digest_parts)).encode("utf-8")).hexdigest()
    return {
        "day": day,
        "title": title,
        "summary": summary,
        "markdown": "\n\n".join(blocks),
        "html": _render_email_html(title, summary, signal_blocks, overview_rows, error_items),
        "has_signals": has_signals,
        "has_errors": has_errors,
        "signal_count": total_signals,
        "error_count": len(error_lines),
        "digest": digest,
    }


def notify_after_run(accounts: list, results: dict[str, str], *,
                     log=None, root: Path | None = None, today: str | None = None) -> dict:
    """定时任务跑完模拟盘后调用；异常全部捕获并写日志，绝不影响主流程"""
    cfg = load_config(root)
    if not cfg.any_enabled:
        return {"skipped": "disabled"}

    report = build_report(accounts, results, today=today)
    if cfg.mode != "always" and not report["has_signals"] and not report["has_errors"]:
        if log:
            log("notify skipped: no signals or errors")
        return {"skipped": "no_events", "report": report}

    prev = _load_state(root)
    if prev.get("day") == report["day"] and prev.get("digest") == report["digest"]:
        if log:
            log("notify skipped: duplicate digest for today")
        return {"skipped": "duplicate", "report": report}

    sent: dict[str, tuple[bool, str]] = {}
    if cfg.desktop:
        sent["desktop"] = send_desktop(report["title"], report["summary"])
    if cfg.email_enabled and cfg.email_token.strip():
        sent["email"] = send_email(cfg.email_token, report["title"], report["markdown"], report["html"])
    if cfg.webhook_enabled and cfg.webhook_url.strip():
        sent["webhook"] = send_webhook(cfg, report["title"], report["markdown"])

    if any(ok for ok, _ in sent.values()):
        _save_state(report["day"], report["digest"], root)

    if log:
        for ch, (ok, msg) in sent.items():
            log(f"notify [{ch}]: {'ok' if ok else f'failed ({msg})'}")
    return {"sent": sent, "report": report}


def send_test(cfg: NotifyConfig) -> list[tuple[str, bool, str]]:
    """界面「发送测试通知」使用：逐个已启用渠道发送测试消息，绕过同日去重"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    title = "SimpleQuant 模拟盘通知测试"
    summary = f"通知通道已连通（{now}），后续定时任务推进模拟账户后的状态简报将发送至此。"
    md = (
        f"## {title}\n\n"
        f"通知通道验证通过（{now}）。\n\n"
        "每日定时任务推进模拟账户后，将按所选条件汇总发送模拟委托记录、净值概况或运行异常。\n\n"
        "---\n*本消息为 SimpleQuant 本地模拟盘通知测试。如需停止接收，请在模拟盘页解除绑定。*"
    )
    html = _render_email_html(title, summary, [], [], [])
    out: list[tuple[str, bool, str]] = []
    if cfg.desktop:
        ok, msg = send_desktop(title, summary)
        out.append(("desktop", ok, msg))
    if cfg.email_enabled and cfg.email_token.strip():
        ok, msg = send_email(cfg.email_token, title, md, html)
        out.append(("email", ok, msg))
    if cfg.webhook_enabled and cfg.webhook_url.strip():
        ok, msg = send_webhook(cfg, title, md)
        out.append(("webhook", ok, msg))
    return out
