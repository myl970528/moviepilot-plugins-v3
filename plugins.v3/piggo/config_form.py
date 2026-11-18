"""猪猪 - 配置页 Vuetify JSON 拼装（结构仿癫影自动签到 / 栖影签到）"""

from typing import Any, Dict, List, Tuple

from app.helper.downloader import DownloaderHelper
from app.log import logger


def _downloader_items() -> List[dict]:
    """只列 Transmission：辅种要靠 trc 的 verify / start 接口，其他下载器做不到。"""
    try:
        services = DownloaderHelper().get_services() or {}
    except Exception as err:  # noqa: BLE001
        logger.warning(f"猪猪：读取下载器列表失败 {err}")
        return []
    return [
        {"title": f"{name} ({info.type})", "value": name}
        for name, info in services.items()
        if info and info.type == "transmission"
    ]


def build_form(values: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    """返回插件配置表单与当前值"""

    def card(icon: str, color: str, title: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center"},
                 "content": [
                     {"component": "VIcon", "props": {"color": color, "class": "mr-2"},
                      "text": icon},
                     {"component": "span", "text": title},
                 ]},
                {"component": "VDivider"},
                {"component": "VCardText", "content": rows},
            ],
        }

    def col(md: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    def row(items: List[dict]) -> dict:
        return {"component": "VRow", "content": items}

    def _props(model: str, label: str, kwargs: dict) -> dict:
        props = {"model": model, "label": label}
        for key, value in kwargs.items():
            props[key.replace("_", "-")] = value
        return props

    def textfield(model: str, label: str, **kwargs) -> dict:
        return {"component": "VTextField", "props": _props(model, label, kwargs)}

    def switch(model: str, label: str, color: str = "primary", **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["color"] = color
        return {"component": "VSwitch", "props": props}

    def select(model: str, label: str, items: List[dict], **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["items"] = items
        return {"component": "VSelect", "props": props}

    def alert(atype: str, text: str, **kwargs) -> dict:
        props = {"type": atype, "variant": "tonal", "text": text}
        props.update(kwargs)
        return {"component": "VAlert", "props": props}

    downloaders = _downloader_items()

    form = [
        {
            "component": "VForm",
            "content": [
                # ── 站点与索引 ──
                card("mdi-web", "primary", "站点与索引", [
                    row([
                        col(4, switch("enabled", "启用索引", "primary",
                                      hint="打开后向 MoviePilot 注册猪猪索引，站内搜索与下载可用",
                                      persistent_hint=True)),
                        col(4, textfield("domain", "域名", placeholder="piggo.me",
                                         hint="一般不用改", persistent_hint=True,
                                         clearable=True)),
                        col(4, textfield("site_id", "站点 ID", type="number",
                                         hint="对应站点管理里的猪猪站，默认 14",
                                         persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("username", "账号", placeholder="站点用户名",
                                         hint="SRP-6a 登录用，搜索与下载种子依赖它",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("password", "密码", type="password",
                                         placeholder="登录密码",
                                         hint="只保存在 MoviePilot 插件配置里",
                                         persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(6, textfield("visitorid", "visitorid",
                                         hint="站点设备指纹，换掉会被风控",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("proxy", "代理地址",
                                         placeholder="http://192.168.0.11:7890",
                                         hint="留空为直连；家宽 IP 访问站点不稳时填旁路由",
                                         persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(6, textfield("token", "token",
                                         hint="留空即可，插件自动登录并续期",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("api_token", "实验室 API 令牌", type="password",
                                         hint="个人中心生成，用于索引增量刷新，可留空",
                                         persistent_hint=True, clearable=True)),
                    ]),
                ]),

                # ── 自动辅种 ──
                card("mdi-seed", "success", "自动辅种", [
                    row([
                        col(3, switch("seed_enabled", "启用辅种", "success",
                                      hint="总开关", persistent_hint=True)),
                        col(3, textfield("seed_cron", "执行周期",
                                         placeholder="15 5 * * *",
                                         hint="Cron，默认每天 05:15",
                                         persistent_hint=True)),
                        col(3, switch("seed_notify", "辅种通知", "info",
                                      hint="跑完推送统计", persistent_hint=True)),
                        col(3, switch("seed_api_index", "API 增量刷新索引", "primary",
                                      hint="需填实验室令牌；关掉则拉前台列表",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, select("seed_source_downloader", "主下载器（扫描已完成种子）",
                                      items=downloaders, multiple=True, chips=True,
                                      clearable=True,
                                      hint="可多选；留空表示全部 Transmission",
                                      persistent_hint=True)),
                        col(6, select("seed_downloader", "辅种下载器（只新增辅种）",
                                      items=downloaders, clearable=True,
                                      hint="留空退回第一个可用 Transmission",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(3, textfield("seed_pages", "索引页数", type="number",
                                         hint="0 表示全量（约 100 页 4 分钟）",
                                         persistent_hint=True)),
                        col(3, textfield("seed_max_add", "单轮最多添加", type="number",
                                         hint="防止一次刷太多校验", persistent_hint=True)),
                        col(3, textfield("seed_label", "做种标签",
                                         hint="写进 Transmission 的标签", persistent_hint=True)),
                        col(3, textfield("seed_min_seeders", "最低做种人数", type="number",
                                         hint="0 表示不限", persistent_hint=True)),
                    ]),
                    row([
                        col(3, switch("seed_auto_start", "校验完自动做种", "success",
                                      hint="校验 100% 后自动 start", persistent_hint=True)),
                        col(3, switch("seed_dryrun", "只匹配不添加", "warning",
                                      hint="试跑用，不写进下载器", persistent_hint=True)),
                        col(3, switch("seed_skip_site_seeding", "跳过站点已在做种",
                                      "primary",
                                      hint="换辅种机时请关掉", persistent_hint=True)),
                        col(3, switch("seed_search", "索引未命中时搜索兜底", "info",
                                      hint="覆盖 100 页以外的老种", persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("seed_search_limit", "单轮搜索上限", type="number",
                                         hint="默认 400，越大越慢", persistent_hint=True)),
                    ]),
                ]),

                # ── 调试 ──
                card("mdi-bug", "info", "调试", [
                    row([
                        col(12, switch("seed_onlyonce", "立即辅种一次", "info",
                                       hint="保存后 60 秒开始，不影响定时计划",
                                       persistent_hint=True)),
                    ]),
                ]),

                alert("info", "主辅分离：主下载器只负责提供「本地已完成种子」清单，辅种下载器只负责新增辅种，"
                              "两台各管一头。按「总字节数相等 + 文件清单完全一致」跨站匹配，"
                              "校验满 100% 才开始做种。"),
                alert("warning", "签到功能已在 2.2 移除：站点风控命中必返 422 need_captcha，"
                                 "而 /v1/web/captcha 图片码接口在站点侧没配置好，真实浏览器同样签不了，"
                                 "只能去网页日历手动点。留着只会每天推一条失败通知。"),
            ],
        },
    ]

    return form, {
        "enabled": values.get("enabled", False),
        "domain": values.get("domain", "piggo.me"),
        "site_id": values.get("site_id", 14),
        "username": values.get("username", ""),
        "password": values.get("password", ""),
        "visitorid": values.get("visitorid", ""),
        "proxy": values.get("proxy", ""),
        "token": values.get("token", ""),
        "api_token": values.get("api_token", ""),
        "seed_enabled": values.get("seed_enabled", False),
        "seed_cron": values.get("seed_cron", "15 5 * * *"),
        "seed_notify": values.get("seed_notify", True),
        "seed_api_index": values.get("seed_api_index", True),
        "seed_skip_site_seeding": values.get("seed_skip_site_seeding", True),
        "seed_source_downloader": values.get("seed_source_downloader", []),
        "seed_downloader": values.get("seed_downloader", ""),
        "seed_pages": values.get("seed_pages", 0),
        "seed_max_add": values.get("seed_max_add", 20),
        "seed_label": values.get("seed_label", "猪猪辅种"),
        "seed_min_seeders": values.get("seed_min_seeders", 0),
        "seed_auto_start": values.get("seed_auto_start", True),
        "seed_dryrun": values.get("seed_dryrun", False),
        "seed_search": values.get("seed_search", True),
        "seed_search_limit": values.get("seed_search_limit", 400),
        "seed_onlyonce": False,
    }
