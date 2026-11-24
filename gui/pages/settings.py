"""设置：AI 模型；使用统计与反馈；关于 SimpleQuant（版本、检查更新、项目链接）"""

from nicegui import run, ui

from simplequant import llm
from simplequant.llm import LLMConfig, PRESETS, PROVIDERS, JSON_MODES
from simplequant.paper import library_sync
from gui.common import t, p, lang
from gui import update, telemetry_ui, data_location_ui, cloud_ui
from gui.layout import frame, page_title
from gui.widgets import folder_btn, notice

DENSE = "outlined dense options-dense"
FIELDS = ("provider", "model", "api_key", "base_url", "json_mode", "timeout", "refusal_fallback")


def page():
    with frame("/settings"):
        page_title("/settings")
        cfg = llm.load_config() or LLMConfig()
        F = {"preset": cfg.preset, **{f: getattr(cfg, f) for f in FIELDS}}
        preset_keys = dict(cfg.preset_keys or {})
        if cfg.api_key and cfg.preset:
            preset_keys[cfg.preset] = cfg.api_key

        with ui.column().classes("gap-1"):
            ui.label(t("set.ai")).classes("font-semibold")
            ui.label(t("set.intro")).classes("sq-muted text-sm")
        with ui.card().classes("w-full gap-3"):
            def on_preset(e):
                """切换预设：自动暂存当前预设的密钥，并恢复目标预设已保存的密钥"""
                old_preset = F["preset"]
                if old_preset:
                    if F["api_key"]:
                        preset_keys[old_preset] = F["api_key"]
                    else:
                        preset_keys.pop(old_preset, None)
                new = LLMConfig.from_preset(e.value)
                F.update(preset=e.value, **{f: getattr(new, f) for f in FIELDS})
                F["api_key"] = preset_keys.get(e.value, "")
                quota_info.text = ""
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
            with ui.row().classes("w-full items-center gap-4") as adv_row:
                json_mode = ui.select({k: p(v) for k, v in JSON_MODES.items()}, label=t("set.json_mode")) \
                    .props(DENSE).classes("w-56").tooltip(t("set.json_mode_help")).mark("llm_json_mode")
                timeout = ui.number(t("set.timeout"), min=10, max=600, step=10).props("outlined dense").classes("w-36")
                fallback = ui.switch(t("set.fallback")).tooltip(t("set.fallback_help"))
            def _fmt_quota(q: dict) -> str:
                if q.get("mode") == "voucher":
                    ai_t, pack_t = int(q.get("ai_total", 0)), int(q.get("pack_total", 0))
                    if ai_t > 0 and pack_t > 0:
                        return t("set.quota_voucher", ai_left=q.get("ai_left", 0), ai_total=ai_t,
                                 pack_left=q.get("pack_left", 0), pack_total=pack_t)
                    if ai_t > 0:
                        return t("set.quota_voucher_ai", ai_left=q.get("ai_left", 0), ai_total=ai_t)
                    return t("set.quota_voucher_pack", pack_left=q.get("pack_left", 0), pack_total=pack_t)
                return t("set.quota_trial", ai_left=q.get("ai_left", 0), ai_total=q.get("ai_total", 5))

            with ui.row().classes("w-full items-center gap-3 flex-wrap") as hosted_row:
                async def check_quota():
                    quota_btn.disable()
                    quota_info.text = t("set.quota_checking")
                    try:
                        q = await run.io_bound(llm.query_quota, F["api_key"], 10.0, lang())
                        cloud_ui.set_cached_quota(current(), q)
                        txt = _fmt_quota(q)
                    except llm.HostedError as e:
                        txt = str(e)
                    except Exception as e:  # noqa: BLE001
                        txt = f"{type(e).__name__}: {e}"
                    finally:
                        if not quota_btn.is_deleted:
                            quota_btn.enable()
                    if not quota_info.is_deleted:
                        quota_info.text = txt
                quota_btn = ui.button(t("set.quota_btn"), icon="account_balance_wallet", on_click=check_quota) \
                    .props("outline dense no-caps").mark("llm_quota")
                ui.link(t("set.cloud_link"), "https://simplequant.org/#cloud", new_tab=True) \
                    .classes("text-xs sq-muted").mark("link:set.cloud_link")
                quota_info = ui.label().classes("text-xs sq-muted").mark("llm_quota_info")
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
            hosted = F["preset"] == "hosted"
            provider.set_visibility(not hosted)
            model.set_visibility(not hosted)
            base_url.set_visibility(not hosted)
            adv_row.set_visibility(not hosted)
            hosted_row.set_visibility(hosted)
            if hosted:
                api_key.props(f'label="{t("set.voucher_code")}" placeholder="sq-..."')
                key_help.text = t("set.hosted_help")
            else:
                api_key.props(f'label="{t("set.api_key")}" placeholder=""')
                model.props(f'placeholder="{t(f"set.model_ph_{F['provider']}")}"')
                env = PRESETS.get(F["preset"], {}).get("key_env")
                key_help.text = t("set.api_key_help_env", env=env) if env else t("set.api_key_help")
            fallback.set_visibility(not hosted and F["provider"] == "anthropic")
            litellm_hint.set_visibility(not hosted and F["provider"] == "litellm")
            ok = hosted or bool(F["model"])
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
            keys = dict(preset_keys)
            if F["preset"]:
                if F["api_key"]:
                    keys[F["preset"]] = F["api_key"]
                else:
                    keys.pop(F["preset"], None)
            return LLMConfig(preset=F["preset"], preset_keys=keys, **{f: F[f] for f in FIELDS})

        msg = ui.column().classes("w-full")

        def save():
            path = llm.save_config(current())
            cloud_ui.invalidate_quota()
            msg.clear()
            with msg:
                notice(t("set.saved"), "check_circle")

        async def test():
            test_btn.disable()
            msg.clear()
            with msg:
                with ui.row().classes("items-center gap-2") as spin:
                    ui.spinner(size="sm")
                    ui.label(t("set.testing")).classes("sq-muted")
            try:
                if F["preset"] == "hosted":
                    q = await run.io_bound(llm.query_quota, F["api_key"], 10.0, lang())
                    q_txt = _fmt_quota(q)
                    if not quota_info.is_deleted:
                        quota_info.text = q_txt
                    text, kind = t("set.test_hosted_ok", quota=q_txt), "info"
                else:
                    reply = await run.io_bound(llm.ping, llm.get_provider(current()))
                    text, kind = t("set.test_ok", reply=reply[:80]), "info"
            except (llm.LLMError, llm.HostedError) as e:
                text, kind = str(e), "error"
            except Exception as e:  # noqa: BLE001 - 缺少依赖等
                text, kind = f"{type(e).__name__}: {e}", "error"
            finally:
                if not test_btn.is_deleted:
                    test_btn.enable()
            if msg.is_deleted:               # 测试期间离开了设置页
                return
            if not spin.is_deleted:          # 测试期间点了保存，msg.clear() 已经把它删掉
                spin.delete()
            with msg:
                notice(text, "check_circle" if kind == "info" else "error", kind)

        with ui.row().classes("gap-3"):
            save_btn = ui.button(t("set.save"), icon="save", on_click=save).props("unelevated no-caps") \
                .mark("llm_save")
            test_btn = ui.button(t("set.test"), icon="network_check", on_click=test).props("outline no-caps") \
                .mark("llm_test")
        with ui.row().classes("items-center gap-2"):
            ui.label(t("set.storage")).classes("sq-muted text-xs")
            folder_btn(t("set.open_folder"), llm.CONFIG_PATH.parent)
        fill()

        ui.separator().classes("q-my-md")
        data_location_ui.settings_section()
        with ui.column().classes("gap-1 w-full mt-2"):
            ui.switch(t("set.lib_sync"), value=library_sync.enabled(),
                      on_change=lambda e: library_sync.set_enabled(bool(e.value))).mark("set_lib_sync")
            ui.label(t("set.lib_sync_help")).classes("sq-muted text-sm")
        ui.separator().classes("q-my-md")
        telemetry_ui.settings_section()
        ui.separator().classes("q-my-md")
        update.about()

