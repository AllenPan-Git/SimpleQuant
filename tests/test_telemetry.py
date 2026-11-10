"""匿名使用统计（simplequant/telemetry.py）"""

import json
import re

import pytest

from simplequant import telemetry as T


class Resp:
    def __init__(self, status):
        self.status_code = status


class FakePost:
    """记下每次请求；status 可以是数字或异常"""
    def __init__(self, status=204):
        self.status, self.calls = status, []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json))
        if isinstance(self.status, Exception):
            raise self.status
        return Resp(self.status)


@pytest.fixture
def tele(tmp_path, monkeypatch):
    """启用统计，存到临时文件；日期可以用 day() 改"""
    monkeypatch.delenv("SIMPLEQUANT_UI_SCRIPT", raising=False)
    monkeypatch.delenv("SIMPLEQUANT_TELEMETRY", raising=False)
    today = {"v": "2026-11-06"}
    monkeypatch.setattr(T, "_today", lambda: today["v"])
    path = tmp_path / "telemetry.json"
    assert T.activate(path=path)

    def day(v):
        today["v"] = v
    yield path, day
    T.deactivate()


def saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_inactive_is_noop(tmp_path, monkeypatch):
    T.deactivate()
    monkeypatch.setattr(T, "PATH", tmp_path / "telemetry.json")
    T.count("btn:backtest")
    T.milestone("backtest")
    T.error(ValueError("x"))
    assert not T.active() and T.payload() is None and not T.due() and not T.send(FakePost())
    assert not (tmp_path / "telemetry.json").exists()


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMPLEQUANT_UI_SCRIPT", "walkthrough.py")
    assert not T.activate(path=tmp_path / "t.json")
    monkeypatch.delenv("SIMPLEQUANT_UI_SCRIPT")
    monkeypatch.setenv("SIMPLEQUANT_TELEMETRY", "0")
    assert not T.activate(path=tmp_path / "t.json")
    assert not T.active()


def test_first_launch_vs_upgrade(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMPLEQUANT_UI_SCRIPT", raising=False)
    T.activate(path=tmp_path / "a.json")
    assert saved(tmp_path / "a.json")["milestones"] == {"first_launch": 0}
    T.activate(existing_user=True, path=tmp_path / "b.json")
    s = saved(tmp_path / "b.json")
    assert s["milestones"] == {"upgrade": 0} and s["consent"] is None
    first_id = s["install_id"]
    T.activate(existing_user=True, path=tmp_path / "b.json")     # 再次启动：编号不变
    assert saved(tmp_path / "b.json")["install_id"] == first_id
    T.deactivate()


def test_records_counts_milestones_errors(tele):
    path, day = tele
    T.count("page:/timing")
    T.count("page:/timing")
    T.count("C:\\Users\\me\\a.csv")                    # 路径进不来
    T.count("股票")
    day("2026-11-09")
    T.milestone("backtest")
    T.milestone("backtest")
    day("2026-11-10")
    T.milestone("backtest")                            # 只记第一次
    s = saved(path)
    assert s["days"]["2026-11-06"]["counts"] == {"page:/timing": 2}
    assert s["milestones"]["backtest"] == 3


def test_error_has_no_message_or_path(tele, tmp_path):
    """出错位置取调用栈里最深的一层本项目代码（模块.函数:行号）；错误消息（这里含路径）不记"""
    path, _ = tele
    from simplequant.data import library
    with pytest.raises(OSError) as ei:
        library.load("secret_600519", tmp_path / "no_such_dir")
    T.error(ei.value)
    errs = T.payload()["days"][0]["errors"]
    assert len(errs) == 1 and errs[0]["type"] == "FileNotFoundError" and errs[0]["n"] == 1
    assert re.fullmatch(r"simplequant\.data\.library\.load:\d+", errs[0]["location"])
    assert "secret" not in path.read_text(encoding="utf-8")


def _raised(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError("没有报错")


def _run_as(module: str, name: str, body: str, **names):
    """造一层「模块 module 里的函数 name」的调用栈（出错位置只看模块名和函数名）"""
    ns = {"__name__": module, **names}
    exec(f"def {name}(*a):\n    {body}", ns)
    return ns[name]


def test_error_location_without_project_frames():
    """调用栈里没有本项目代码：记最深一层第三方库（线上的 FileNotFoundError 之前只有 unknown，查不出是哪个库）；
    全是标准库时记最深一层"""
    import json
    lib = _run_as("nicegui.persistence.file_persistent_dict", "backup", "open(a[0])")
    e = _raised(lambda: lib("/no/such/dir/secret.json"))
    assert T.error_location(e) == "nicegui.persistence.file_persistent_dict.backup:2"
    e = _raised(lambda: json.loads("{"))
    e.__traceback__ = e.__traceback__.tb_next.tb_next          # 去掉测试文件自己的两层，只剩标准库
    assert T.error_location(e).startswith("json.")
    assert T.error_location(ValueError("没有调用栈")) == "unknown"


def test_error_location_adds_caller_of_shared_widget():
    """错在到处都在用的界面小构件里（线上的 RuntimeError @ gui.widgets.notice，117 处在用）：加上调用它的那一层"""
    notice = _run_as("gui.widgets", "notice", "raise RuntimeError('The client this element belongs to has been deleted.')")
    download = _run_as("gui.pages.selection.data_tab", "download", "notice()", notice=notice)
    loc = T.error_location(_raised(download))
    assert loc == "gui.widgets.notice:2.from.gui.pages.selection.data_tab.download:2" and T.LOC_RE.match(loc)
    assert T.error_location(_raised(notice)) == "gui.widgets.notice:2"           # 没有调用方
    long = _run_as("gui.pages." + "x" * 120, "f", "notice()", notice=notice)      # 加上就超长：只留构件本身
    assert T.error_location(_raised(long)) == "gui.widgets.notice:2"


def test_payload_matches_server_format(tele):
    T.count("btn:backtest")
    T.set_ui_info("en", "classic")
    p = T.payload()
    assert set(p) == {"install_id", "version", "os", "lang", "ui", "milestones", "days"}
    assert p["lang"] == "en" and p["ui"] == "classic" and p["os"] in ("windows", "macos", "linux")
    assert p["days"] == [{"day": "2026-11-06", "counts": {"btn:backtest": 1}, "errors": []}]
    T.set_ui_info("zh", "new")


def test_no_send_before_consent(tele):
    post = FakePost()
    assert T.consent() is None and not T.due() and not T.send(post)
    assert post.calls == []


def test_decline_clears_and_stops(tele):
    path, _ = tele
    T.count("btn:backtest")
    T.set_consent(False)
    T.count("btn:backtest")
    T.milestone("paper")
    s = saved(path)
    assert s["consent"] is False and s["days"] == {} and s["milestones"] == {}
    assert not T.send(FakePost())


def test_send_once_a_day_and_resend_full_totals(tele):
    path, day = tele
    T.set_consent(True)
    T.count("btn:backtest")
    post = FakePost()
    assert T.send(post)
    assert post.calls[0][0].endswith("/v1/ping")
    assert not T.send(post) and len(post.calls) == 1       # 同一天只发一次
    T.count("btn:backtest")                                 # 发送后当天还在用
    day("2026-11-07")
    T.count("page:/")
    assert T.send(post)
    days = {d["day"]: d["counts"] for d in post.calls[1][1]["days"]}
    assert days == {"2026-11-06": {"btn:backtest": 2}, "2026-11-07": {"page:/": 1}}   # 前一天按完整数字重发
    assert list(saved(path)["days"]) == ["2026-11-07"]      # 发完的日子删掉，只留今天


def test_send_failure_keeps_everything(tele):
    path, _ = tele
    T.set_consent(True)
    T.count("btn:backtest")
    before = saved(path)
    assert not T.send(FakePost(500))
    assert not T.send(FakePost(OSError("offline")))
    assert saved(path) == before and T.due()


def test_backlog_capped_at_30_days(tele):
    _, day = tele
    T.set_consent(True)
    for i in range(1, 32):
        day(f"2026-10-{i:02d}")
        T.count("page:/")
    day("2026-11-06")
    assert len(T.payload()["days"]) == T.MAX_DAYS


def test_limits_on_keys(tele):
    for i in range(T.MAX_KEYS + 10):
        T.count(f"page:/p{i}")
    assert len(T.payload()["days"][0]["counts"]) == T.MAX_KEYS


def test_feedback_results(tele):
    post = FakePost()
    assert T.send_feedback("  ", post) == "empty"
    assert T.send_feedback("字" * 2001, post) == "too_long"
    assert T.send_feedback(" 很好用 ", post) == "ok"
    url, body = post.calls[0]
    assert url.endswith("/v1/feedback") and body["text"] == "很好用" and "install_id" not in body
    assert T.send_feedback("x", FakePost(429)) == "rate_limited"
    assert T.send_feedback("x", FakePost(OSError())) == "failed"
    assert T.send_feedback("x", FakePost(400)) == "failed"


def test_feedback_without_consent_or_activation(tmp_path, monkeypatch):
    """反馈是用户主动提交的：没同意统计、甚至没启用统计也能发"""
    T.deactivate()
    post = FakePost()
    assert T.send_feedback("建议", post) == "ok" and len(post.calls) == 1
