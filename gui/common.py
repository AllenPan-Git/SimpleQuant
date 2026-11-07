"""页面共用：语言、主题（偏好保存在 app.storage.general，单机单用户，重启后保持）"""

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


def t(key: str, **kw) -> str:
    return tr(key, lang(), **kw)


def p(obj) -> str:
    return pick(obj, lang())
