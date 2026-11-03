"""页面共用：语言、主题、界面版本（偏好保存在 app.storage.general，单机单用户，重启后保持）"""

from nicegui import app

from simplequant.i18n import pick, tr, DEFAULT_LANG, LANGS
import ui.texts  # noqa: F401  界面文字合并进 TEXT


def lang() -> str:
    lg = app.storage.general.get("lang", DEFAULT_LANG)
    return lg if lg in LANGS else DEFAULT_LANG


def set_lang(lg: str):
    app.storage.general["lang"] = lg


def dark() -> bool | None:
    """None = 跟随系统"""
    return app.storage.general.get("dark")


def set_dark(value: bool | None):
    app.storage.general["dark"] = value


UI_CHOICES = ("classic", "new")
UI_DEFAULT = "classic"      # 用户没选过时用哪套界面；3.0 起改为 "new"（界面重构计划 4.7）


def ui_choice() -> str | None:
    """用户在设置页选的界面；None = 没选过（按 UI_DEFAULT）。

    「没选过」和「选了经典」要分开：3.0 改默认时，没选过的人跟着切到新版，明确选了经典的保持经典"""
    v = app.storage.general.get("ui")
    return v if v in UI_CHOICES else None


def set_ui(choice: str):
    app.storage.general["ui"] = choice


def new_ui() -> bool:
    """当前是否使用新版界面（侧栏导航；两套界面并存期间，见界面重构计划 4.0）"""
    return (ui_choice() or UI_DEFAULT) == "new"


def t(key: str, **kw) -> str:
    return tr(key, lang(), **kw)


def p(obj) -> str:
    return pick(obj, lang())
