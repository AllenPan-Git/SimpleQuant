"""云端托管服务（AI 托管额度 + R2 离线数据包）统一入口、余量弹窗与额度耗尽就地引导"""

from __future__ import annotations

from nicegui import run, ui

from simplequant import llm
from simplequant.llm import LLMConfig
from gui.common import t, lang

CLOUD_DOC_URL = "https://simplequant.org/#cloud"

# 进程内余量缓存：(api_key, base_url) -> dict；仅在用户主动查询、保存额度码后更新，切页不发网络请求
_QUOTA_CACHE: dict[tuple[str, str], dict] = {}


def _cache_key(cfg: LLMConfig) -> tuple[str, str]:
    return ((cfg.api_key or "").strip(), (cfg.base_url or "").strip())


def _cfg() -> LLMConfig:
    return llm.load_config() or LLMConfig()


def cached_quota(cfg: LLMConfig | None = None) -> dict | None:
    c = cfg or _cfg()
    if c.preset != "hosted":
        return None
    return _QUOTA_CACHE.get(_cache_key(c))


def set_cached_quota(cfg: LLMConfig, data: dict) -> None:
    if cfg.preset == "hosted":
        _QUOTA_CACHE[_cache_key(cfg)] = dict(data)


def invalidate_quota() -> None:
    _QUOTA_CACHE.clear()


def format_quota(q: dict) -> str:
    """与设置页口径一致的余量文案"""
    if q.get("mode") == "voucher":
        ai_t, pack_t = int(q.get("ai_total", 0)), int(q.get("pack_total", 0))
        if ai_t > 0 and pack_t > 0:
            return t("set.quota_voucher", ai_left=q.get("ai_left", 0), ai_total=ai_t,
                     pack_left=q.get("pack_left", 0), pack_total=pack_t)
        if ai_t > 0:
            return t("set.quota_voucher_ai", ai_left=q.get("ai_left", 0), ai_total=ai_t)
        return t("set.quota_voucher_pack", pack_left=q.get("pack_left", 0), pack_total=pack_t)
    return t("set.quota_trial", ai_left=q.get("ai_left", 0), ai_total=q.get("ai_total", 5))


def _short_status(cfg: LLMConfig) -> str:
    """侧栏与首页卡片的简短状态行"""
    if cfg.preset != "hosted":
        return t("cloud.sub_custom")
    q = cached_quota(cfg)
    if q is not None:
        if q.get("mode") == "voucher":
            ai_l, pack_l = int(q.get("ai_left", 0)), int(q.get("pack_left", 0))
            if int(q.get("pack_total", 0)) > 0:
                return t("cloud.sub_both", ai=ai_l, pack=pack_l)
            return t("cloud.sub_ai", ai=ai_l)
        return t("cloud.sub_trial_n", ai=int(q.get("ai_left", 0)))
    if (cfg.api_key or "").strip():
        return t("cloud.sub_voucher_ready")
    return t("cloud.sub_trial")


def open_cloud_dialog(on_saved=None):
    """研报风「云端托管服务」对话框：查看两项服务说明、查余量、就地填写额度码并立即生效"""
    cfg = _cfg()
    saved_voucher = (cfg.preset_keys or {}).get("hosted", "") or (cfg.api_key if cfg.preset == "hosted" else "")

    with ui.context.client.layout, ui.dialog() as dlg, \
            ui.column().classes("sq-sheet gap-0 px-7 pt-6 pb-5 w-[540px] max-w-full").mark("cloud_dialog"):
        ui.label(t("cloud.eyebrow")).classes("sq-eyebrow")
        ui.label(t("cloud.title")).classes("sq-serif text-2xl mt-1 mb-2")
        ui.label(t("cloud.desc")).classes("text-sm leading-relaxed mb-4").style("color: var(--sq-text2)")

        # 两项托管服务一览（细线分栏）
        with ui.column().classes("w-full gap-2 py-3 mb-4").style(
                "border-top: 1px solid var(--sq-border); border-bottom: 1px solid var(--sq-border2)"):
            with ui.row().classes("items-start gap-2 no-wrap"):
                ui.icon("auto_awesome", size="18px").classes("sq-red mt-0.5")
                with ui.column().classes("gap-0"):
                    ui.label(t("cloud.feat_ai_title")).classes("text-sm font-semibold")
                    ui.label(t("cloud.feat_ai_body")).classes("sq-muted text-xs leading-relaxed")
            with ui.row().classes("items-start gap-2 no-wrap mt-1"):
                ui.icon("cloud_download", size="18px").classes("sq-red mt-0.5")
                with ui.column().classes("gap-1"):
                    ui.label(t("cloud.feat_pack_title")).classes("text-sm font-semibold")
                    ui.label(t("cloud.feat_pack_body")).classes("sq-muted text-xs leading-relaxed")
                    with ui.row().classes("items-center gap-2 flex-wrap mt-0.5"):
                        ui.button(t("cloud.go_pack_stocks"), icon="download",
                                  on_click=lambda: _go_pack("stocks")) \
                            .props("outline dense no-caps").classes("text-xs").mark("cloud_go_pack_stocks")
                        ui.button(t("cloud.go_pack_cb"), icon="download",
                                  on_click=lambda: _go_pack("cb")) \
                            .props("outline dense no-caps").classes("text-xs").mark("cloud_go_pack_cb")

        # 额度码输入与查询
        code_in = ui.input(
            t("set.voucher_code"), value=saved_voucher, placeholder="sq-...",
            password=True, password_toggle_button=True,
        ).props("outlined dense").classes("w-full").mark("cloud_voucher_input")
        status = ui.label(
            format_quota(cached_quota(cfg)) if cached_quota(cfg) is not None else t("cloud.status_hint")
        ).classes("text-xs sq-muted mt-1 mb-3").mark("cloud_quota_status")

        def _go_pack(tab: str):
            key = (code_in.value or "").strip()
            if not key:
                msg = t("cloud.pack_need_code")
                if not status.is_deleted:
                    status.text = msg
                ui.notify(msg, type="warning")
                code_in.run_method("focus")
                return
            from gui import state
            from gui.layout import navigate
            state.STATE["data_tab"] = tab
            state.STATE["open_pack"] = True
            dlg.close()
            navigate("/data", refresh_same=True)

        async def _refresh_quota(key: str):
            status.text = t("set.quota_checking")
            try:
                q = await run.io_bound(llm.query_quota, key, 10.0, lang())
                tmp = LLMConfig.from_preset("hosted", api_key=key)
                set_cached_quota(tmp, q)
                txt = format_quota(q)
            except llm.HostedError as e:
                txt = str(e)
            except Exception as e:  # noqa: BLE001
                txt = f"{type(e).__name__}: {e}"
            if not status.is_deleted:
                status.text = txt

        async def on_check():
            check_btn.disable()
            try:
                await _refresh_quota((code_in.value or "").strip())
            finally:
                if not check_btn.is_deleted:
                    check_btn.enable()

        async def on_apply():
            key = (code_in.value or "").strip()
            cur = llm.load_config() or LLMConfig()
            keys = dict(cur.preset_keys or {})
            if cur.preset and cur.api_key:
                keys[cur.preset] = cur.api_key
            if key:
                keys["hosted"] = key
            else:
                keys.pop("hosted", None)
            new_cfg = LLMConfig.from_preset("hosted", api_key=key, preset_keys=keys)
            llm.save_config(new_cfg)
            invalidate_quota()
            save_btn.disable()
            try:
                await _refresh_quota(key)
            finally:
                if not save_btn.is_deleted:
                    save_btn.enable()
            ui.notify(t("cloud.saved"), type="positive")
            if on_saved:
                on_saved()

        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            save_btn = ui.button(t("cloud.apply"), icon="check", on_click=on_apply) \
                .props("unelevated no-caps color=primary").mark("cloud_apply")
            check_btn = ui.button(t("set.quota_btn"), icon="account_balance_wallet", on_click=on_check) \
                .props("outline no-caps").mark("cloud_check_quota")
            ui.button(t("cloud.get_voucher"), icon="open_in_new") \
                .props(f'outline no-caps href="{CLOUD_DOC_URL}" target="_blank"').mark("cloud_get_voucher")

        with ui.row().classes("w-full items-center justify-between pt-3 mt-4") \
                .style("border-top: 1px solid var(--sq-border2)"):
            ui.button(t("cloud.go_settings"), on_click=lambda: ui.navigate.to("/settings"), color=None) \
                .props("flat dense no-caps").classes("text-xs px-1").style("color: var(--sq-muted)") \
                .mark("cloud_go_settings")
            ui.button(t("cloud.close"), on_click=dlg.close, color=None) \
                .props("flat dense no-caps").classes("text-xs px-1").style("color: var(--sq-muted)") \
                .mark("cloud_close")

    dlg.on_value_change(lambda e: e.value or dlg.delete())
    dlg.open()


def sidebar_cloud_card():
    """左侧栏常驻「云端托管」入口：宽窗口显示细线研报卡，窄窗口自动折为单图标 + tooltip"""
    cfg = _cfg()
    sub = _short_status(cfg)
    with ui.element("div").classes("sq-side-cloud").on("click", lambda: open_cloud_dialog()) \
            .mark("nav_cloud") as card:
        ui.icon("cloud_queue", size="18px").classes("sq-side-cloud-ico")
        with ui.column().classes("sq-side-cloud-text gap-0 min-w-0"):
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.label(t("cloud.nav_title")).classes("sq-side-cloud-title")
                if cfg.preset == "hosted" and not (cfg.api_key or "").strip():
                    ui.label(t("cloud.badge_trial")).classes("sq-side-cloud-tag")
            ui.label(sub).classes("sq-side-cloud-sub")
        ui.tooltip(f"{t('cloud.nav_title')} · {sub}").classes("sq-tip sq-side-tip") \
            .props('anchor="center right" self="center left" :offset="[8, 0]"')
    return card


def home_cloud_strip():
    """首页研报细线横条：突出 5 次免配置 AI 体验与云端数据包，提供一键管理入口"""
    cfg = _cfg()
    has_voucher = cfg.preset == "hosted" and bool((cfg.api_key or "").strip())
    with ui.row().classes("sq-cloud-strip w-full items-center justify-between gap-6 flex-wrap") \
            .mark("home_cloud_strip"):
        with ui.column().classes("gap-1 min-w-0 grow"):
            with ui.row().classes("items-center gap-2"):
                ui.label(t("cloud.nav_title")).classes("sq-serif text-base font-semibold")
                if not has_voucher:
                    ui.label(t("cloud.badge_trial")).classes("sq-side-cloud-tag")
            ui.label(t("cloud.home_active" if has_voucher else "cloud.home_trial")).classes("text-sm leading-relaxed") \
                .style("color: var(--sq-text2)")
        with ui.row().classes("items-center gap-3 shrink-0"):
            ui.button(t("cloud.manage_btn"), icon="vpn_key", on_click=lambda: open_cloud_dialog()) \
                .props("outline no-caps").classes("px-4").mark("home_cloud_manage")
            ui.button(t("cloud.get_voucher"), icon="open_in_new") \
                .props(f'flat no-caps href="{CLOUD_DOC_URL}" target="_blank"') \
                .classes("sq-red px-3").mark("home_cloud_doc")


def is_quota_error(err: Exception) -> bool:
    """判断异常是否属于托管额度用尽 / 额度码无效，便于就地弹出额度引导"""
    if isinstance(err, llm.HostedError) and err.code in (
        "trial_exhausted", "quota_exhausted", "ai_not_included", "invalid_voucher",
    ):
        return True
    s = str(err)
    return any(k in s for k in (
        "trial_exhausted", "quota_exhausted", "ai_not_included", "invalid_voucher",
        "免费体验次数已用完", "使用次数已用完", "额度码无效", "不包含 AI 托管额度",
        "Free trial quota", "quota for this voucher", "Invalid voucher",
    ))


def handle_ai_error(err: Exception, box=None, on_retry=None):
    """统一的 AI 调用报错处理：若为额度不足且提供 box，就地展示「填写额度码 / 获取方式 / 自有 Key」引导条"""
    msg = str(err)
    if is_quota_error(err) and box is not None and not box.is_deleted:
        box.clear()
        with box, ui.column().classes("w-full gap-2 sq-note py-3").mark("ai_quota_banner"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("vpn_key", size="18px").classes("sq-red")
                ui.label(msg).classes("text-sm font-medium")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.button(t("cloud.enter_voucher"), icon="vpn_key",
                          on_click=lambda: open_cloud_dialog(on_saved=on_retry)) \
                    .props("unelevated dense no-caps color=primary").mark("ai_quota_enter")
                ui.button(t("cloud.get_voucher"), icon="open_in_new") \
                    .props(f'outline dense no-caps href="{CLOUD_DOC_URL}" target="_blank"') \
                    .mark("ai_quota_doc")
                ui.button(t("cloud.use_own_key"), on_click=lambda: ui.navigate.to("/settings"), color=None) \
                    .props("flat dense no-caps").classes("text-xs sq-muted")
        return
    ui.notify(msg, type="negative", multi_line=True)


def iterate_dialog(explanation: str, run_fn, on_apply):
    """基于 AI 回测解读一键生成改进版策略草案的共用对话框。
    run_fn(change_request: str, cfg: LLMConfig) -> NLResult（同步函数，将在后台线程运行）
    on_apply(res: NLResult)：用户确认采用后回填编辑器/表单
    """
    from simplequant import strategies, telemetry
    trimmed = explanation.strip()
    if len(trimmed) > 550:
        trimmed = trimmed[:550].rstrip() + "…"
    default_req = t("ai.iterate_default_req", text=trimmed)
    draft = {"res": None}

    with ui.context.client.layout, ui.dialog() as dlg, \
            ui.column().classes("sq-sheet gap-3 px-7 pt-6 pb-5 w-[640px] max-w-full").mark("ai_iterate_dialog"):
        ui.label(t("ai.iterate_eyebrow")).classes("sq-eyebrow")
        ui.label(t("ai.iterate_title")).classes("sq-serif text-xl")
        ui.label(t("ai.iterate_help")).classes("sq-muted text-xs")
        req = ui.textarea(t("ai.iterate_req_label"), value=default_req).props("outlined autogrow").classes("w-full") \
            .mark("ai_iterate_input")
        err_box = ui.column().classes("w-full")
        preview = ui.column().classes("w-full gap-2").mark("ai_iterate_preview")

        async def go():
            text = (req.value or "").strip()
            if not text:
                return
            c = _cfg()
            if not (c and c.ready):
                return
            err_box.clear()
            preview.clear()
            gen_btn.disable()
            spin.set_visibility(True)
            telemetry.ai("iterate")
            try:
                res = await run.io_bound(run_fn, text, c)
                draft["res"] = res
                invalidate_quota()
            except Exception as e:  # noqa: BLE001
                handle_ai_error(e, err_box)
                return
            finally:
                if not gen_btn.is_deleted:
                    gen_btn.enable()
                if not spin.is_deleted:
                    spin.set_visibility(False)
            if preview.is_deleted:
                return
            with preview:
                ui.label(res.understood or "—").classes("text-sm sq-note py-2 w-full")
                if res.unsupported:
                    ui.label(t("ai.unsupported") + "：" + "；".join(res.unsupported)).classes("text-xs text-warning")
                for e in res.errors:
                    ui.label(e).classes("text-xs text-negative")
                if res.ok:
                    ui.label(strategies.describe(res.spec, lang())).classes("sq-code w-full text-xs")
                    ui.button(t("ai.iterate_apply"), icon="check", on_click=lambda: (on_apply(res), dlg.close())) \
                        .props("unelevated no-caps color=primary").classes("self-start").mark("ai_iterate_apply")

        with ui.row().classes("w-full items-center gap-3"):
            gen_btn = ui.button(t("ai.iterate_run"), icon="auto_awesome", on_click=go) \
                .props("unelevated no-caps color=primary").mark("ai_iterate_run")
            spin = ui.row().classes("items-center gap-2")
            with spin:
                ui.spinner(size="sm")
                ui.label(t("ai.thinking")).classes("sq-muted text-sm")
            spin.set_visibility(False)
            ui.button(t("cloud.close"), on_click=dlg.close, color=None).props("flat no-caps").classes("ml-auto sq-muted")

    dlg.on_value_change(lambda e: e.value or dlg.delete())
    dlg.open()
