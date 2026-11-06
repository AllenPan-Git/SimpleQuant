"""匿名使用统计与反馈的界面（待办 57）：首页询问一次、「查看会发送什么」、设置页开关与反馈框。
逻辑在 simplequant/telemetry.py；没启用统计时（界面测试、截图）首页不询问，设置页只显示反馈框"""

import json

from nicegui import run, ui

from simplequant import telemetry
from simplequant.update import DISCUSSIONS_PAGE
from gui.common import t
from gui.widgets import notice


def should_ask() -> bool:
    return telemetry.active() and telemetry.consent() is None


def preview_dialog():
    """显示 telemetry.payload() 原文：下次真正发送的就是这份"""
    body = telemetry.payload() if telemetry.consent() is not False else None
    with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-full gap-3").mark("tele_preview_dialog"):
        ui.label(t("tele.preview_title")).classes("sq-h2")
        if body:
            ui.label(t("tele.preview_note")).classes("sq-muted text-sm")
            ui.code(json.dumps(body, ensure_ascii=False, indent=2), language="json").classes("w-full max-h-[50vh] overflow-auto")
        else:
            ui.label(t("tele.preview_off")).classes("text-sm")
        ui.button(t("tele.close"), on_click=dlg.close).props("flat no-caps").classes("self-end")
    dlg.open()


def consent_note():
    """首页：第一次打开时询问。默认勾选「同意」，但点「确定」之前什么都不发送"""
    def ok():
        telemetry.set_consent(bool(agree.value))
        box.delete()
        if telemetry.due():
            telemetry.send_async(1)

    with ui.column().classes("w-full gap-2 sq-note sq-note-info").mark("home_tele") as box:
        ui.markdown(t("tele.ask")).classes("text-sm sq-md-tight")
        with ui.row().classes("items-center gap-4"):
            agree = ui.checkbox(t("tele.agree"), value=True).mark("home_tele_agree")
            ui.button(t("tele.preview"), on_click=preview_dialog).props("flat dense no-caps").mark("home_tele_preview")
            ui.button(t("tele.ok"), on_click=ok).props("outline no-caps").mark("home_tele_ok")


def settings_section():
    """设置页：统计开关（启用了才显示）+ 反馈框"""
    with ui.column().classes("gap-2 w-full"):
        ui.label(t("set.tele")).classes("font-semibold")
        if telemetry.active():
            def on_change(e):
                telemetry.set_consent(bool(e.value))
                if telemetry.due():
                    telemetry.send_async(1)
            with ui.row().classes("items-center gap-4"):
                ui.switch(t("tele.agree"), value=telemetry.consent() is True, on_change=on_change) \
                    .mark("set_tele")
                ui.button(t("tele.preview"), on_click=preview_dialog).props("flat dense no-caps") \
                    .mark("set_tele_preview")
            ui.label(t("set.tele_help")).classes("sq-muted text-sm")
        _feedback()


def _feedback():
    msg = ui.column().classes("w-full")

    async def send():
        btn.disable()
        try:
            res = await run.io_bound(telemetry.send_feedback, text.value or "")
        finally:
            btn.enable()
        msg.clear()
        with msg:
            if res == "ok":
                text.value = ""
                notice(t("set.fb_ok"), "check_circle")
            else:
                notice(t(f"set.fb_{res}"), "error", "error")

    ui.label(t("set.fb")).classes("text-sm mt-2")
    text = ui.textarea(placeholder=t("set.fb_ph")).props(f"outlined autogrow maxlength={telemetry.FEEDBACK_CHARS}") \
        .classes("w-full md:w-2/3").mark("set_fb_text")
    with ui.row().classes("items-center gap-4"):
        btn = ui.button(t("set.fb_send"), icon="send", on_click=send).props("outline no-caps").mark("set_fb_send")
        ui.link(t("set.fb_reply"), DISCUSSIONS_PAGE, new_tab=True).classes("text-sm").mark("link:set.fb_reply")
