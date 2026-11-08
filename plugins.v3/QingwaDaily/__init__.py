"""
青蛙（qingwapt）蝌蚪商店 - 每日福利自动购买。

站点把商店做成了前端 AJAX 接口：
  GET  /api/bonus-shop/getItems   返回商品列表（含 id、名称、价格、库存、限购）
  POST /api/bonus-shop/exchange   multipart 提交 id + amount 完成兑换

「每日福利：1000蝌蚪」的实际定义是花 1 蝌蚪换 1000 蝌蚪，limit_duration=1、
limit_amount=1，即每天一次。重复购买时接口返回 {"success": false,
"msg": "超过限购数量。"}，这不算失败，只说明今天已经领过。
"""

import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.network import RequestUtils


class QingwaDaily(_PluginBase):
    # 插件名称
    plugin_name = "青蛙每日福利"
    # 插件描述
    plugin_desc = "每天自动在青蛙蝌蚪商店购买「每日福利：1000蝌蚪」，复用站点已配置的 Cookie 与代理。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "hermes"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "qingwadaily_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 2

    # 站点与接口
    _SITE_DOMAIN = "qingwapt.com"
    _SITE_URL = "https://www.qingwapt.com"
    _ITEM_KEYWORD = "每日福利"
    _FALLBACK_ITEM_ID = 28
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    # 已购买提示，命中即视为今天已经领过
    _ALREADY_BOUGHT_KEYWORDS = ("限购", "已购买", "已经购买", "已兑换", "已经兑换")

    # 配置
    _enabled: bool = False
    _notify: bool = False
    _cron: str = "10 8 * * *"
    _cookie: str = ""
    _use_proxy: bool = True

    _HISTORY_KEY = "history"
    _HISTORY_LIMIT = 30

    def init_plugin(self, config: Optional[dict] = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify"))
            cron = str(config.get("cron") or "").strip()
            self._cron = cron or "10 8 * * *"
            self._cookie = str(config.get("cookie") or "").strip()
            proxy = config.get("proxy")
            self._use_proxy = True if proxy is None else bool(proxy)

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ 调度

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            if str(self._cron).strip().count(" ") != 4:
                logger.error(f"青蛙每日福利：cron 表达式不合法：{self._cron}")
                return []
            return [{
                "id": "QingwaDaily",
                "name": "青蛙每日福利",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_task,
                "kwargs": {},
            }]
        except Exception as err:
            logger.error(f"青蛙每日福利：注册定时任务失败：{err}")
            return []

    def stop_service(self):
        pass

    # ------------------------------------------------------------------ 接口

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET"],
            "summary": "立即购买每日福利",
            "description": "手动执行一次购买，不影响定时任务",
        }]

    def api_run(self) -> schemas.Response:
        ok, msg = self.run_task()
        return schemas.Response(success=ok, message=msg, data={"success": ok, "message": msg})

    # ------------------------------------------------------------------ 业务

    def run_task(self) -> Tuple[bool, str]:
        """执行一次购买并记录结果。"""
        try:
            ok, msg, gain = self._purchase()
        except Exception as err:
            logger.error(f"青蛙每日福利：执行异常 {err}\n{traceback.format_exc()}")
            ok, msg, gain = False, f"执行异常：{err}", 0

        self._append_history(ok, msg)
        if ok:
            logger.info(f"青蛙每日福利：{msg}")
        else:
            logger.error(f"青蛙每日福利：{msg}")

        # 只有真的买到（gain>0）或出错才通知，重复执行命中"已购买"不打扰
        if self._notify and (gain > 0 or not ok):
            title = "【青蛙每日福利】"
            text = msg if ok else f"执行失败：{msg}"
            try:
                self.post_message(title=title, text=text)
            except Exception as err:
                logger.error(f"青蛙每日福利：发送通知失败 {err}")
        return ok, msg

    def _purchase(self) -> Tuple[bool, str, int]:
        site = self._load_site()
        cookie = self._cookie or (getattr(site, "cookie", "") or "")
        if not cookie:
            return False, "青蛙站点未配置 Cookie，请在站点管理中更新登录信息，或在插件配置里手填", 0

        ua = (getattr(site, "ua", "") or "").strip() or self._DEFAULT_UA
        use_proxy = self._use_proxy and bool(getattr(site, "proxy", 1))
        proxies = settings.PROXY if use_proxy else None
        timeout = int(getattr(site, "timeout", 0) or 30)

        client = RequestUtils(
            cookies=cookie,
            headers={
                "User-Agent": ua,
                "Referer": f"{self._SITE_URL}/bonusshop.php",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            proxies=proxies,
            timeout=timeout,
        )

        item, err = self._find_item(client)
        if not item:
            return False, err, 0

        item_id = item.get("id")
        cost = item.get("a_amount")
        gain = int(item.get("b_amount") or 0)

        res = client.post_res(
            url=f"{self._SITE_URL}/api/bonus-shop/exchange",
            files={"id": (None, str(item_id)), "amount": (None, "1")},
        )
        if res is None:
            return False, "购买请求无响应，站点可能超时或代理不可用", 0
        if res.status_code != 200:
            return False, f"购买请求返回状态码 {res.status_code}", 0

        try:
            payload = res.json()
        except Exception:
            return False, "购买返回内容无法解析，Cookie 可能已失效", 0
        if not isinstance(payload, dict):
            return False, "购买返回格式异常", 0

        message = str(payload.get("msg") or "").strip()
        if payload.get("success"):
            detail = f"购买成功，{cost if cost is not None else 1} 蝌蚪换 {gain} 蝌蚪"
            return True, detail, gain
        if any(key in message for key in self._ALREADY_BOUGHT_KEYWORDS):
            return True, f"今日已购买过（{message}）", 0
        return False, message or "购买失败，站点未返回原因", 0

    def _load_site(self):
        try:
            return SiteOper().get_by_domain(self._SITE_DOMAIN)
        except Exception as err:
            logger.warning(f"青蛙每日福利：读取站点配置失败 {err}")
            return None

    def _find_item(self, client: RequestUtils) -> Tuple[Optional[dict], str]:
        res = client.get_res(url=f"{self._SITE_URL}/api/bonus-shop/getItems")
        if res is None:
            return None, "商品列表请求无响应，站点可能超时或代理不可用"
        if res.status_code != 200:
            return None, f"商品列表返回状态码 {res.status_code}"
        try:
            items = res.json()
        except Exception:
            return None, "商品列表无法解析，Cookie 可能已失效"
        if not isinstance(items, list):
            return None, "商品列表格式异常"

        for item in items:
            if not isinstance(item, dict):
                continue
            if self._ITEM_KEYWORD in str(item.get("name") or ""):
                return item, ""
        for item in items:
            if isinstance(item, dict) and item.get("id") == self._FALLBACK_ITEM_ID:
                return item, ""
        return None, f"商店里没有找到含「{self._ITEM_KEYWORD}」的商品"

    # ------------------------------------------------------------------ 记录

    def _append_history(self, ok: bool, message: str) -> None:
        history = self.get_data(self._HISTORY_KEY) or []
        if not isinstance(history, list):
            history = []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(ok),
            "msg": message,
        })
        self.save_data(self._HISTORY_KEY, history[: self._HISTORY_LIMIT])

    # ------------------------------------------------------------------ 页面

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'proxy', 'label': '使用站点代理设置'}}
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，如 10 8 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': 'Cookie（留空则使用站点配置）',
                                            'placeholder': 'qw_session=...'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '默认读取站点管理中「青蛙」的 Cookie、UA 和代理开关，'
                                                    '站点 Cookie 更新后插件自动跟随，无需改动这里。'
                                                    '商店商品按名称含「每日福利」自动识别，站点改 id 也不影响。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "proxy": True,
            "cron": "10 8 * * *",
            "cookie": "",
        }

    def get_page(self) -> List[dict]:
        history = self.get_data(self._HISTORY_KEY) or []
        if not isinstance(history, list):
            history = []

        last = history[0] if history else None
        if last:
            last_text = f"{last.get('time', '')}　{'成功' if last.get('ok') else '失败'}　{last.get('msg', '')}"
        else:
            last_text = "暂无执行记录"

        rows = []
        for record in history[:15]:
            rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-sm'}, 'text': str(record.get('time', ''))},
                    {'component': 'td', 'props': {'class': 'text-sm'},
                     'text': '成功' if record.get('ok') else '失败'},
                    {'component': 'td', 'props': {'class': 'text-sm'}, 'text': str(record.get('msg', ''))},
                ]
            })
        if not rows:
            rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td',
                     'props': {'colspan': 3, 'class': 'text-sm text-medium-emphasis'},
                     'text': '还没有执行过，可以点插件右上角的按钮立即执行一次'}
                ]
            })

        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'success' if (last and last.get('ok')) else 'info',
                                    'variant': 'tonal',
                                    'text': f"最近一次：{last_text}"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {'hover': True, 'density': 'compact'},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [
                                                    {'component': 'th', 'props': {'class': 'text-start'},
                                                     'text': '时间'},
                                                    {'component': 'th', 'props': {'class': 'text-start'},
                                                     'text': '结果'},
                                                    {'component': 'th', 'props': {'class': 'text-start'},
                                                     'text': '说明'},
                                                ]
                                            }
                                        ]
                                    },
                                    {'component': 'tbody', 'content': rows}
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
