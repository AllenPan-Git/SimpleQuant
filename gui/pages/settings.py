"""设置：AI 模型；关于 SimpleQuant（版本、检查更新、项目链接）"""

from nicegui import run, ui

from simplequant import llm
from simplequant.llm import LLMConfig, PRESETS, PROVIDERS, JSON_MODES
from gui.common import t, p
from gui import update
from gui.layout import frame, page_title
from gui.widgets import notice

DENSE = "outlined dense options-dense"
FIELDS = ("provider", "model", "api_key", "base_url", "json_mode", "timeout", "refusal_fallback")


def page():
    with frame("/settings"):
        page_title("/settings")
        cfg = llm.load_config() or LLMConfig()
        F = {"preset": cfg.preset, **{f: getattr(cfg, f) for f in FIELDS}}

        with ui.column().classes("gap-1"):
            ui.label(t("set.ai")).classes("font-semibold")
            ui.label(t("set.intro")).classes("sq-muted text-sm")
        with ui.card().classes("w-full gap-3"):
            def on_preset(e):
                """切换预设：填入接口类型、地址、示例模型；保留已填的 key（同一接口类型时）"""
                new = LLMConfig.from_preset(e.value)
                keep_key = F["api_key"] if new.provider == F["provider"] else ""
                F.update(preset=e.value, **{f: getattr(new, f) for f in FIELDS})
                F["api_key"] = keep_key
                fill()
            ui.select({k: p(v["label"]) for k, v in PRESETS.items()}, label=t("set.preset"), value=F["preset"],
                      on_change=on_preset).props(DENSE).classes("w-full md:w-1/2").mark("llm_preset")
            with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-2"):
                provider = ui.select({k: p(v) for k, v in PROVIDERS.items()}, label=t("set.provider")) \
                    .props(DENSE).mark("llm_provider")
                model = ui.input(t("set.model")).props("outlined dense").tooltip(t("set.model_help")).mark("llm_model")
                api_key = ui.input(t("set.api_key"), password=True, password_toggle_button=True) \
                    .props("outlined dense").mark("llm_api_key")
                base_url = ui.input(t("set.base_url"), placeholder=t("set.base_url_ph")).props("outlined dense") \
                    .tooltip(t("set.base_url_help")).mark("llm_base_url")
            with ui.row().classes("w-full items-center gap-4"):
                json_mode = ui.select({k: p(v) for k, v in JSON_MODES.items()}, label=t("set.json_mode")) \
                    .props(DENSE).classes("w-56").tooltip(t("set.json_mode_help")).mark("llm_json_mode")
                timeout = ui.number(t("set.timeout"), min=10, max=600, step=10).props("outlined dense").classes("w-36")
                fallback = ui.switch(t("set.fallback")).tooltip(t("set.fallback_help"))
            key_help = ui.label().classes("sq-muted text-xs")
            litellm_hint = ui.label(t("set.litellm_hint")).classes("sq-muted text-xs")

        filling = {"on": False}

        def fill():
            """把 F 写回控件（切换预设时）"""
            filling["on"] = True
            provider.value, model.value, api_key.value = F["provider"], F["model"], F["api_key"]
            base_url.value, json_mode.value, timeout.value = F["base_url"], F["json_mode"], F["timeout"]
            fallback.value = F["refusal_fallback"]
            filling["on"] = False
            extras()

        def extras():
            model.props(f'placeholder="{t(f"set.model_ph_{F['provider']}")}"')
            env = PRESETS.get(F["preset"], {}).get("key_env")
            key_help.text = t("set.api_key_help_env", env=env) if env else t("set.api_key_help")
            fallback.set_visibility(F["provider"] == "anthropic")
            litellm_hint.set_visibility(F["provider"] == "litellm")
            ok = bool(F["model"])
            save_btn.set_enabled(ok)
            test_btn.set_enabled(ok)

        def bind(el, field, cast=None):
            def f(e):
                if filling["on"] or e.value is None:
                    return
                F[field] = cast(e.value) if cast else e.value
                extras()
            el.on_value_change(f)
        for el, field, cast in ((provider, "provider", None), (model, "model", str), (api_key, "api_key", str),
                                (base_url, "base_url", str), (json_mode, "json_mode", None),
                                (timeout, "timeout", float), (fallback, "refusal_fallback", bool)):
            bind(el, field, cast)

        def current() -> LLMConfig:
            return LLMConfig(preset=F["preset"], **{f: F[f] for f in FIELDS})

        msg = ui.column().classes("w-full")

        def save():
            path = llm.save_config(current())
            msg.clear()
            with msg:
                notice(t("set.saved", path=str(path)), "check_circle")

        async def test():
            test_btn.disable()
            msg.clear()
            with msg:
                with ui.row().classes("items-center gap-2") as spin:
                    ui.spinner(size="sm")
                    ui.label(t("set.testing")).classes("sq-muted")
            try:
                reply = await run.io_bound(llm.ping, llm.get_provider(current()))
                text, kind = t("set.test_ok", reply=reply[:80]), "info"
            except llm.LLMError as e:
                text, kind = str(e), "error"
            except Exception as e:  # noqa: BLE001 - 缺少依赖等
                text, kind = f"{type(e).__name__}: {e}", "error"
            finally:
                test_btn.enable()
            spin.delete()
            with msg:
                notice(text, "check_circle" if kind == "info" else "error", kind)

        with ui.row().classes("gap-3"):
            save_btn = ui.button(t("set.save"), icon="save", on_click=save).props("unelevated no-caps") \
                .mark("llm_save")
            test_btn = ui.button(t("set.test"), icon="network_check", on_click=test).props("outline no-caps")
        ui.label(t("set.storage", path=str(llm.CONFIG_PATH))).classes("sq-muted text-xs")
        fill()

        ui.separator().classes("q-my-md")
        update.about()
