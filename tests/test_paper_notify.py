"""模拟盘推送通知（邮件 / 桌面通知 / Webhook）与定时任务补跑测试"""

from pathlib import Path

import pandas as pd

from simplequant.paper import notify, schedule
from simplequant.paper.account import PaperAccount


def _make_account(tmp_path: Path, acc_id: str, name: str, signals: list[dict], *, status: str = "active"):
    acc = PaperAccount(
        id=acc_id,
        name=name,
        kind="single",
        spec={"template": "ma_cross"},
        broker={"cash": 100000},
        start="2026-10-01",
        status=status,
    )
    acc_dir = tmp_path / acc_id
    acc_dir.mkdir(parents=True, exist_ok=True)
    acc.save_nav(pd.DataFrame(
        {"value": [100000.0, 102000.0], "cash": [20000.0, 20000.0], "benchmark": [100000.0, 101000.0]},
        index=pd.to_datetime(["2026-11-25", "2026-11-26"]),
    ))
    acc.save_state({
        "as_of": "2026-11-26",
        "execute_on": "2026-11-27",
        "value": 102000.0,
        "metrics": {"total_return": 0.02, "benchmark_return": 0.01},
        "signals": signals,
        "positions": [],
    })
    return acc


def test_config_roundtrip_and_mask(tmp_path):
    cfg = notify.NotifyConfig(
        desktop=True,
        email_enabled=True,
        email_token="em_" + "a" * 32,
        email_address="alice@qq.com",
        webhook_enabled=True,
        webhook_kind="feishu",
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        mode="always",
    )
    notify.save_config(cfg, root=tmp_path)
    loaded = notify.load_config(root=tmp_path)
    assert loaded == cfg
    assert loaded.any_enabled
    assert notify.mask_email("alice@qq.com") == "al***e@qq.com"


def test_build_report_signals_errors_and_html(tmp_path, monkeypatch):
    monkeypatch.setattr("simplequant.paper.account.PAPER_ROOT", tmp_path)
    a1 = _make_account(tmp_path, "acc1", "双均线策略", [
        {"symbol": "sh.510300", "name": "沪深300ETF", "side": "buy", "size": 1200, "lot": 100, "ref_price": 4.123},
        {"symbol": "sh.510500", "name": "中证500ETF", "side": "sell", "size": 50, "lot": 100, "ref_price": 5.5},
    ])
    a2 = _make_account(tmp_path, "acc2", "可转债双低", [])
    rep = notify.build_report([a1, a2], {"acc1": "", "acc2": "network timeout"}, today="2026-11-26")
    assert rep["has_signals"] is True
    assert rep["has_errors"] is True
    assert rep["signal_count"] == 1
    assert rep["error_count"] == 1
    assert "双均线策略" in rep["markdown"]
    assert "1" in rep["markdown"]
    assert "请打开本机 SimpleQuant 模拟盘页查看明细" in rep["html"]
    assert "沪深300ETF" not in rep["markdown"]
    assert "沪深300ETF" not in rep["html"]
    assert "1.020" in rep["html"]
    assert "+2.00%" in rep["html"]
    assert "+1.00%" in rep["html"]
    assert "账户净值与基准概况" in rep["html"]
    assert "network timeout" in rep["html"]


def test_notify_after_run_dedup_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("simplequant.paper.account.PAPER_ROOT", tmp_path)
    a1 = _make_account(tmp_path, "acc1", "无信号账户", [])
    cfg = notify.NotifyConfig(email_enabled=True, email_token="em_" + "b" * 32, mode="signals")
    notify.save_config(cfg, root=tmp_path)

    sent_calls = []
    monkeypatch.setattr(notify, "send_email",
                        lambda tok, sub, txt, html="": (sent_calls.append((tok, sub)) or True, ""))

    r1 = notify.notify_after_run([a1], {"acc1": ""}, root=tmp_path, today="2026-11-26")
    assert r1["skipped"] == "no_events"
    assert len(sent_calls) == 0

    cfg.mode = "always"
    notify.save_config(cfg, root=tmp_path)
    r2 = notify.notify_after_run([a1], {"acc1": ""}, root=tmp_path, today="2026-11-26")
    assert r2["sent"]["email"] == (True, "")
    assert len(sent_calls) == 1

    r3 = notify.notify_after_run([a1], {"acc1": ""}, root=tmp_path, today="2026-11-26")
    assert r3["skipped"] == "duplicate"
    assert len(sent_calls) == 1


def test_detect_webhook_kind():
    assert notify.detect_webhook_kind("https://www.pushplus.plus/send?token=abc") == "pushplus"
    assert notify.detect_webhook_kind("SCT123456abcdef") == "serverchan"
    assert notify.detect_webhook_kind("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1") == "wecom"
    assert notify.detect_webhook_kind("https://open.feishu.cn/open-apis/bot/v2/hook/1") == "feishu"
    assert notify.detect_webhook_kind("https://oapi.dingtalk.com/robot/send?access_token=1") == "dingtalk"
    assert notify.detect_webhook_kind("https://api.day.app/mykey/") == "bark"
    assert notify.detect_webhook_kind("https://example.com/hook") == "generic"


def test_windows_schedule_enables_catchup(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "backend", lambda: "windows")
    monkeypatch.setattr(schedule, "_schtasks", lambda *args: (calls.append(("schtasks", args)) or True, ""))
    monkeypatch.setattr(schedule, "_run", lambda *args, **kw: (calls.append(("run", args)) or True, ""))
    ok, _ = schedule.create_task("19:00")
    assert ok is True
    assert any(kind == "run" and "powershell" in args[0] and "StartWhenAvailable" in " ".join(args)
               for kind, args in calls)
