"""栖影签到 - 配置页 Vuetify JSON 拼装（结构仿癫影自动签到）"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """返回插件配置表单"""

    def card(title_icon: str, title_color: str, title_text: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                    {"component": "VIcon", "props": {"color": title_color, "class": "mr-2"},
                     "text": title_icon},
                    {"component": "span", "text": title_text},
                ]},
                {"component": "VDivider"},
                {"component": "VCardText", "content": rows},
            ],
        }

    def col(md: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    def _props(model: str, label: str, kwargs: dict) -> dict:
        props = {"model": model, "label": label}
        for k, v in kwargs.items():
            props[k.replace("_", "-")] = v
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

    def row(items: List[dict]) -> dict:
        return {"component": "VRow", "content": items}

    form = [
        {
            "component": "VForm",
            "content": [
                # ── 基础设置 ──
                card("mdi-cog", "primary", "基础设置", [
                    row([
                        col(4, switch("enabled", "启用插件", "primary",
                                      hint="总开关，打开后才按定时计划自动签到", persistent_hint=True)),
                        col(8, switch("notify", "签到结果通知", "success",
                                      hint="签到成功 / 已签到 / 失败时发送消息通知", persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("username", "栖影账号",
                                         placeholder="站点用户名",
                                         hint="hdhaven.com 的登录用户名",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("password", "登录密码",
                                         placeholder="登录密码",
                                         hint="仅保存在 MoviePilot 插件配置中",
                                         persistent_hint=True, clearable=True, type="password")),
                    ]),
                ]),

                # ── 签到设置 ──
                card("mdi-calendar-check", "success", "签到设置", [
                    row([
                        col(4, switch("lucky_mode", "赌狗签模式", "warning",
                                      hint="开 = 赌狗签（4 倍 10% / 1.5 倍 35% / 1 倍 25% / 白签 10% / 血亏 -1 倍 20%）；"
                                           "关 = 普通签固定积分。两者每天只能二选一，赌狗签不加经验",
                                      persistent_hint=True)),
                        col(4, select("retry", "失败重试次数",
                                      items=[
                                          {"title": "不重试", "value": 0},
                                          {"title": "重试 1 次", "value": 1},
                                          {"title": "重试 2 次", "value": 2},
                                          {"title": "重试 3 次", "value": 3},
                                      ],
                                      hint="每次重试约 40 秒（Browserless 全新会话）",
                                      persistent_hint=True)),
                        col(4, textfield("cron", "定时表达式",
                                         placeholder="20 8 * * *",
                                         hint="Cron 表达式，默认每天 08:20 北京时间",
                                         persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("browserless_url", "Browserless 地址",
                                         placeholder="https://production-sfo.browserless.io",
                                         hint="与「PT站点签到」插件共用同一份即可",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("browserless_token", "Browserless 令牌",
                                         placeholder="后台生成的 API Token",
                                         hint="用于解 Cloudflare Turnstile，不填无法签到",
                                         persistent_hint=True, clearable=True, type="password")),
                    ]),
                ]),

                # ── 调试 ──
                card("mdi-bug", "info", "调试", [
                    row([
                        col(12, switch("onlyonce", "立即运行一次", "info",
                                       hint="保存后约 15 秒执行一次签到（不影响定时计划），用于验证账号与网络",
                                       persistent_hint=True)),
                    ]),
                ]),

                alert("info", "栖影登录强制 Cloudflare Turnstile，且会话绑定出口 IP 与设备指纹，机房 IP 直连一律卡在「正在验证」，"
                              "复用别处的 Cookie 会被判定异地直接吊销。本插件用 Browserless 的 BrowserQL 在云端浏览器内解验证、"
                              "填表登录，并在同一会话里调用签到接口，全程不落地 Cookie。普通签与赌狗签每天二选一："
                              "赌狗签按站点档位随机翻倍，加权期望约 0.975 倍且不加经验，长期看略亏；想要稳定升级请关掉赌狗签。"),
            ],
        },
    ]

    return form, {
        "enabled": False,
        "notify": True,
        "username": "",
        "password": "",
        "lucky_mode": False,
        "cron": "20 8 * * *",
        "retry": 2,
        "browserless_url": "https://production-sfo.browserless.io",
        "browserless_token": "",
        "onlyonce": False,
    }
