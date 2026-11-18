# -*- coding: utf-8 -*-
"""猪猪 (piggo.me) 站点一体化插件。

一个插件覆盖两件事：

  索引：把 PiggoSpider 注入 MP 索引器体系，给 piggo.me 注册索引配置，
        并启动本地下载代理，让站点搜索与下载可用。
  辅种：拉全站列表建本地索引，按「总字节数相等 + 文件清单完全一致」
        跨站匹配本地已完成种子，校验通过后自动做种。

索引优先走个人中心的实验室 API（Bearer 令牌 + added_at 增量），没配令牌
或 API 失败时回退前台列表；种子下载与搜索仍走前台 SRP-6a 登录，token 失效
自动重登。

签到功能已于 2.2 移除：站点把签到做成万年历（/calendar），风控命中必返
422 need_captcha，而 /v1/web/captcha 图片码接口在站点侧未正确配置，真实
浏览器同样签不了，只能人工去网页点。留着只会每天推一条失败通知。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

from .engine import (
    DEFAULT_BASE,
    PiggoApiClient,
    PiggoClient,
    PiggoError,
    file_signature,
    match_by_size,
    torrent_files,
    torrent_infohash,
)
from .spider import PiggoSpider, start_dl_server

_PARSER_NAME = "Piggo"
_DEFAULT_DOMAIN = "piggo.me"
_DEFAULT_VISITORID = "99e9943aec19cff88977be9093dd2e12"
_DEFAULT_PROXY = "http://192.168.0.11:7890"
_DEFAULT_SEED_CRON = "15 5 * * *"

_INDEX_FILE = "piggo_index.json"
_SEEDED_KEY = "seed_seeded"
_SEED_STATS_KEY = "seed_stats"
_API_CURSOR_KEY = "piggo_api_cursor"


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """兼容 transmission_rpc 不同版本的属性命名。

    transmission_rpc 的 Torrent 是 dict 子类，__getattr__ 直接取 self[name]，
    缺键时抛的是 KeyError 而不是 AttributeError，所以 hasattr 会炸，
    必须逐个 try。
    """
    for name in names:
        try:
            value = getattr(obj, name)
        except (AttributeError, KeyError, TypeError):
            continue
        if value is not None:
            return value
    if isinstance(obj, dict):
        flat = {str(k).replace("_", "").lower(): v for k, v in obj.items()}
        for name in names:
            key = name.replace("_", "").lower()
            if key in flat and flat[key] is not None:
                return flat[key]
    return default


def _files_of(torrent: Any) -> List[Tuple[str, int]]:
    """取种子文件清单。

    transmission_rpc 的 Torrent.get_files() 依赖 priorities / wanted 字段，
    请求时漏掉会抛 KeyError('priorities')；再不行就直接读原始 fields。
    """
    items: Any = None
    for name in ("get_files", "files"):
        try:
            value = getattr(torrent, name)
        except (AttributeError, KeyError, TypeError):
            continue
        if callable(value):
            try:
                items = value()
            except Exception:  # noqa: BLE001
                items = None
        else:
            items = value
        if items:
            break
    if not items:
        try:
            fields = getattr(torrent, "fields", None) or {}
            items = fields.get("files") if isinstance(fields, dict) else None
        except Exception:  # noqa: BLE001
            items = None
    out: List[Tuple[str, int]] = []
    for item in items or []:
        name = str(_attr(item, "name", default="") or "")
        size = int(_attr(item, "size", "length", default=0) or 0)
        if name:
            out.append((name, size))
    return out


_NOISE_WORDS = {
    "web", "webdl", "webrip", "dl", "bluray", "bdrip", "hdtv", "remux",
    "h264", "h265", "x264", "x265", "hevc", "avc", "aac", "ac3", "flac",
    "dts", "ddp", "truehd", "atmos", "10bit", "8bit",
}


def search_keyword(name: str, words: int = 3) -> str:
    """从种子名里挑几个词当站内搜索关键词。

    站点列表接口把总数封顶在 100 页 10000 条，索引覆盖不到的老种只能
    靠搜索去捞。名字前几个词基本就是资源主标题，后面的分辨率、编码
    只会把结果带偏，所以先剔掉噪声词再截断。
    """
    text = re.sub(r"[\[\]()（）【】]", " ", str(name or ""))
    picked: List[str] = []
    for piece in re.split(r"[.\s_]+", text):
        token = piece.strip("-_")
        if not token:
            continue
        low = token.lower()
        if low in _NOISE_WORDS or re.fullmatch(r"\d{3,4}[pi]", low):
            continue
        picked.append(token)
        if len(picked) >= words:
            break
    return " ".join(picked)


class Piggo(_PluginBase):
    # 插件名称
    plugin_name = "猪猪"
    # 插件描述
    plugin_desc = "猪猪站一体化：索引搜索、自动辅种。"
    # 插件图标
    plugin_icon = "spider.png"
    # 插件版本
    plugin_version = "2.2"
    # 插件作者
    plugin_author = "hermes"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "piggoindexer_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 2

    # ---- 站点与索引
    _enabled: bool = False
    _token: str = ""
    _username: str = ""
    _password: str = ""
    _domain: str = _DEFAULT_DOMAIN
    _visitorid: str = _DEFAULT_VISITORID
    _site_id: int = 14
    _proxy: str = _DEFAULT_PROXY
    _api_token: str = ""

    # ---- 辅种
    _seed_enabled: bool = False
    _seed_api_index: bool = True
    _seed_skip_site_seeding: bool = True
    _seed_cron: str = _DEFAULT_SEED_CRON
    _seed_downloader: str = ""
    _seed_source_downloader: str = ""
    _seed_pages: int = 0
    _seed_max_add: int = 20
    _seed_label: str = "猪猪辅种"
    _seed_min_seeders: int = 0
    _seed_auto_start: bool = True
    _seed_dryrun: bool = False
    _seed_notify: bool = True
    _seed_onlyonce: bool = False
    _seed_search: bool = True
    _seed_search_limit: int = 400

    # ---- 签到已移除（2.2）：站点风控无解，老配置里的 sign_* 键留着无害，不再读取

    _seed_running: bool = False
    _seed_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[dict] = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled"))
            self._token = str(config.get("token") or "").strip()
            self._username = str(config.get("username") or "").strip()
            self._password = str(config.get("password") or "").strip()
            self._api_token = str(config.get("api_token") or "").strip()
            domain = str(config.get("domain") or "").strip().lower()
            if domain:
                self._domain = domain
            visitorid = str(config.get("visitorid") or "").strip()
            if visitorid:
                self._visitorid = visitorid
            proxy = config.get("proxy")
            if proxy is not None:
                self._proxy = str(proxy).strip()
            try:
                site_id = int(config.get("site_id") or 0)
                if site_id > 0:
                    self._site_id = site_id
            except (TypeError, ValueError):
                pass

            self._seed_enabled = bool(config.get("seed_enabled"))
            self._seed_api_index = bool(config.get("seed_api_index", True))
            self._seed_skip_site_seeding = bool(
                config.get("seed_skip_site_seeding", True)
            )
            self._seed_cron = str(config.get("seed_cron") or _DEFAULT_SEED_CRON).strip()
            self._seed_downloader = str(config.get("seed_downloader") or "").strip()
            self._seed_source_downloader = self.__join_names(
                config.get("seed_source_downloader")
            )
            self._seed_label = str(config.get("seed_label") or "猪猪辅种").strip()
            self._seed_auto_start = bool(config.get("seed_auto_start", True))
            self._seed_dryrun = bool(config.get("seed_dryrun"))
            self._seed_notify = bool(config.get("seed_notify", True))
            self._seed_onlyonce = bool(config.get("seed_onlyonce"))
            self._seed_pages = self.__to_int(config.get("seed_pages"), 0)
            self._seed_max_add = self.__to_int(config.get("seed_max_add"), 20)
            self._seed_min_seeders = self.__to_int(config.get("seed_min_seeders"), 0)
            self._seed_search = bool(config.get("seed_search", True))
            self._seed_search_limit = self.__to_int(config.get("seed_search_limit"), 400)

        try:
            self._install()
        except Exception as err:  # noqa: BLE001
            logger.error(f"猪猪：索引安装失败 {err}")

        if self._seed_onlyonce:
            self._seed_onlyonce = False
            self.__save_config()
            logger.info("猪猪辅种：收到立即执行指令，60 秒后开始")
            threading.Timer(60, self.run_seed_task).start()

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        pass

    # ------------------------------------------------------------------ 索引安装
    def _build_spider(self) -> PiggoSpider:
        return PiggoSpider({
            "domain": "https://piggo.me/",
            "token": self._token,
            "username": self._username,
            "password": self._password,
            "visitorid": self._visitorid,
            "name": "猪猪",
        })

    def _install(self) -> None:
        from app.helper.sites import SitesHelper
        from app.modules.indexer import SPIDER_PARSER_CLASSES, _SPECIALIZED_SEARCH_ARGUMENTS

        SPIDER_PARSER_CLASSES[_PARSER_NAME] = PiggoSpider
        _SPECIALIZED_SEARCH_ARGUMENTS[_PARSER_NAME] = ("keyword", "mtype", "cat", "page")

        if not self._enabled:
            logger.info("猪猪：插件未启用，仅注入解析器")
            return
        if not self._username or not self._password:
            logger.warning("猪猪：已启用但未配置账号密码，站点搜索会失败")

        try:
            start_dl_server(self._build_spider())
        except Exception as err:  # noqa: BLE001
            logger.error(f"猪猪：下载代理启动失败 {err}")

        SitesHelper().add_indexer(self._domain, {
            "id": self._site_id,
            "name": "猪猪",
            "domain": "https://piggo.me/",
            "parser": _PARSER_NAME,
            "token": self._token,
            "username": self._username,
            "password": self._password,
            "visitorid": self._visitorid,
            "proxy": False,
        })
        logger.info(
            f"猪猪：已注册索引配置 {self._domain} -> {_PARSER_NAME} (id={self._site_id})"
        )

    # ------------------------------------------------------------------ 定时服务
    def get_service(self) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        if not self._enabled or not self._username or not self._password:
            return services

        if self._seed_enabled:
            trigger = self.__cron(self._seed_cron, "辅种")
            if trigger:
                services.append({
                    "id": "PiggoCrossSeed",
                    "name": "猪猪自动辅种",
                    "trigger": trigger,
                    "func": self.run_seed_task,
                    "kwargs": {},
                })
                services.append({
                    "id": "PiggoCrossSeedTrack",
                    "name": "猪猪辅种校验跟踪",
                    "trigger": "interval",
                    "func": self.track_verify,
                    "kwargs": {"minutes": 5},
                })

        return services

    @staticmethod
    def __cron(expr: str, label: str):
        try:
            return CronTrigger.from_crontab(expr)
        except ValueError:
            logger.error(f"猪猪{label}：cron 表达式无法解析 {expr}")
            return None

    # ------------------------------------------------------------------ 配置页
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        from .config_form import build_form
        return build_form(self.__current_config())

    def __current_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "domain": self._domain,
            "site_id": self._site_id,
            "username": self._username,
            "password": self._password,
            "visitorid": self._visitorid,
            "proxy": self._proxy,
            "token": self._token,
            "api_token": self._api_token,
            "seed_enabled": self._seed_enabled,
            "seed_cron": self._seed_cron,
            "seed_notify": self._seed_notify,
            "seed_api_index": self._seed_api_index,
            "seed_skip_site_seeding": self._seed_skip_site_seeding,
            "seed_source_downloader": [
                item.strip()
                for item in self._seed_source_downloader.split(",")
                if item.strip()
            ],
            "seed_downloader": self._seed_downloader,
            "seed_pages": self._seed_pages,
            "seed_max_add": self._seed_max_add,
            "seed_label": self._seed_label,
            "seed_min_seeders": self._seed_min_seeders,
            "seed_auto_start": self._seed_auto_start,
            "seed_dryrun": self._seed_dryrun,
            "seed_search": self._seed_search,
            "seed_search_limit": self._seed_search_limit,
        }

    # ------------------------------------------------------------------ 详情页
    def get_page(self) -> List[dict]:
        """统计页：KPI + 最近辅种记录 + 上轮运行统计。

        全部读本地缓存，渲染时不去登录站点：辅种一轮要几分钟，为了刷新
        一个数字去开一次会话不值当。
        """
        stats = self.get_data(_SEED_STATS_KEY) or {}
        seeded = self.get_data(_SEEDED_KEY) or {}
        records = sorted(
            [r for r in seeded.values() if isinstance(r, dict)],
            key=lambda x: str(x.get("at") or ""),
            reverse=True,
        )
        seeding = sum(1 for r in records if r.get("state") == "seeding")
        verifying = sum(1 for r in records if r.get("state") == "verifying")
        nodata = sum(1 for r in records if r.get("state") == "nodata")

        def kpi(icon: str, label: str, value: str, color: str = "",
                note: str = "") -> dict:
            value_cls = (f"text-h6 font-weight-bold text-{color}" if color
                         else "text-h6 font-weight-bold")
            children = [
                {"component": "VIcon",
                 "props": {"color": color or "primary", "size": "28", "class": "mb-1"},
                 "text": icon},
                {"component": "span", "props": {
                    "class": value_cls,
                    "style": "display:block; font-size: clamp(0.8rem, 3.8vw, 1.25rem);"
                             " white-space: normal; overflow-wrap: anywhere;"
                             " word-break: break-all; line-height: 1.2;",
                }, "text": value},
                {"component": "span",
                 "props": {"class": "text-caption text-medium-emphasis d-block mt-1"},
                 "text": label},
            ]
            if note:
                children.append({"component": "div",
                                 "props": {"class": "text-caption text-medium-emphasis"},
                                 "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [{"component": "VCardText",
                             "props": {"class": "text-center pa-3"},
                             "content": children}],
            }

        source = "实验室 API" if stats.get("index_source") == "api" else "前台列表"
        last_run = str(stats.get("finished_at") or "") or "尚未运行"
        kpis = [
            ("mdi-database", "索引条数",
             f"{int(stats.get('index_total') or 0):,}", "primary",
             f"本轮刷新 {int(stats.get('index_new') or 0):,} 条 · {source}"),
            ("mdi-file-check", "本地已完成",
             f"{int(stats.get('local_done') or 0):,}", "info",
             f"体积命中 {int(stats.get('hit') or 0):,}"),
            ("mdi-upload-network", "当前做种",
             f"{seeding:,}", "success",
             f"校验中 {verifying:,} · 无数据 {nodata:,}"),
            ("mdi-clock-outline", "上次运行", last_run, "warning",
             f"耗时 {stats.get('elapsed', 0)} 秒" if stats else "点「立即辅种一次」试跑"),
        ]

        page: List[dict] = [{
            "component": "VRow",
            "props": {"dense": True},
            "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 6},
                 "content": [kpi(*kpis[0])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6},
                 "content": [kpi(*kpis[1])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6},
                 "content": [kpi(*kpis[2])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6},
                 "content": [kpi(*kpis[3])]},
            ],
        }]

        state_map = {
            "seeding": ("做种中", "success"),
            "verifying": ("校验中", "warning"),
            "nodata": ("无数据", "error"),
        }
        rows = []
        for idx, item in enumerate(records[:30], start=1):
            state_text, state_color = state_map.get(
                str(item.get("state")), ("未知", "medium-emphasis")
            )
            name = str(item.get("name") or "")
            rows.append([
                (str(idx), "text-body-2 text-medium-emphasis"),
                (str(item.get("at") or "—"), "text-body-2 text-no-wrap"),
                (f"PIG#{item.get('pig_id')}", "text-body-2 text-no-wrap"),
                (name[:46] + ("…" if len(name) > 46 else ""), "text-body-2"),
                (str(item.get("dl") or "—"), "text-body-2 text-center"),
                (f"TR#{item.get('tr_id')}" if item.get("tr_id") else "—",
                 "text-body-2 text-center"),
                (state_text, f"text-body-2 text-center text-{state_color}"),
            ])

        header_cells = [
            ("#", "text-caption text-medium-emphasis"),
            ("时间", "text-caption text-medium-emphasis"),
            ("种子", "text-caption text-medium-emphasis"),
            ("名称", "text-caption text-medium-emphasis"),
            ("下载器", "text-caption text-center text-medium-emphasis"),
            ("任务", "text-caption text-center text-medium-emphasis"),
            ("状态", "text-caption text-center text-medium-emphasis"),
        ]

        table_content = [{
            "component": "VTable",
            "props": {"density": "comfortable"},
            "content": [
                {"component": "thead", "content": [
                    {"component": "tr", "content": [
                        {"component": "th", "props": {"class": c}, "text": t}
                        for t, c in header_cells
                    ]},
                ]},
                {"component": "tbody", "content": [
                    {"component": "tr", "content": [
                        {"component": "td", "props": {"class": c}, "text": t}
                        for t, c in row
                    ]}
                    for row in rows
                ]},
            ],
        }] if rows else [
            {"component": "VAlert",
             "props": {"type": "info", "variant": "tonal",
                       "text": "还没有辅种记录，配置里打开「立即辅种一次」或等定时任务执行"}},
        ]

        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {"component": "VCardTitle",
                         "props": {"class": "d-flex align-center"},
                         "content": [
                             {"component": "VIcon",
                              "props": {"color": "primary", "class": "mr-2"},
                              "text": "mdi-history"},
                             {"component": "span", "text": "最近辅种（最多 30 条）"},
                         ]},
                        {"component": "VCardText", "props": {"class": "pa-2"},
                         "content": table_content},
                    ],
                }],
            }],
        })

        def stat_cell(label: str, value: Any, color: str = "") -> dict:
            cls = "text-subtitle-1 font-weight-bold" + (f" text-{color}" if color else "")
            return {
                "component": "VCol",
                "props": {"cols": 6, "md": 3},
                "content": [
                    {"component": "div",
                     "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                    {"component": "div", "props": {"class": cls},
                     "text": "—" if value is None else str(value)},
                ],
            }

        detail = [
            stat_cell("清单一致", stats.get("matched"), "success"),
            stat_cell("本轮新增", stats.get("added"), "success"),
            stat_cell("已收录跳过", stats.get("skipped")),
            stat_cell("失败", stats.get("failed"), "error"),
            stat_cell("站点做种清单", stats.get("site_seeding")),
            stat_cell("据此跳过", stats.get("site_skipped")),
            stat_cell("主下载器", "、".join(stats.get("sources") or []) or "自动"),
            stat_cell("辅种下载器", stats.get("downloader") or "自动"),
        ]

        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {"component": "VCardTitle",
                         "props": {"class": "d-flex align-center"},
                         "content": [
                             {"component": "VIcon",
                              "props": {"color": "info", "class": "mr-2"},
                              "text": "mdi-chart-box"},
                             {"component": "span", "text": "上轮运行统计"},
                         ]},
                        {"component": "VCardText",
                         "content": [{"component": "VRow", "content": detail}]},
                    ],
                }],
            }],
        })

        note = str(stats.get("message") or "")
        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {
                        "type": "warning" if note else "info",
                        "variant": "tonal",
                        "text": (note or
                                 "索引优先走个人中心的实验室 API 增量刷新，没配令牌或 API 失败时回退前台列表；"
                                 "签到功能已于 2.2 移除，站点风控无解，只能去网页日历手动签。"),
                    },
                }],
            }],
        })

        return page


    # ------------------------------------------------------------------ API
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/seed",
                "endpoint": self.api_seed,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "立即执行辅种",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询状态",
            },
        ]

    async def api_seed(self) -> dict:
        if self._seed_running:
            return {"success": False, "message": "辅种任务正在运行"}
        threading.Thread(target=self.run_seed_task, daemon=True).start()
        return {"success": True, "message": "已启动辅种"}

    async def api_status(self) -> dict:
        return {
            "success": True,
            "seed_running": self._seed_running,
            "seed_stats": self.get_data(_SEED_STATS_KEY) or {},
            "seeded": len(self.get_data(_SEEDED_KEY) or {}),
        }

    # ------------------------------------------------------------------ 辅种主流程
    def run_seed_task(self) -> None:
        if not self._seed_enabled:
            logger.info("猪猪辅种：未启用，跳过")
            return
        with self._seed_lock:
            if self._seed_running:
                logger.warning("猪猪辅种：上一轮尚未结束，跳过本次")
                return
            self._seed_running = True
        started = time.time()
        stats: Dict[str, Any] = {"message": ""}
        client = None
        try:
            client = self.__client()
            logger.info("猪猪辅种：开始登录站点")
            client.login()

            index = self.__load_index()
            logger.info(f"猪猪辅种：已有索引 {len(index)} 条，开始刷新")
            fresh, source = self.__refresh_index(client, index)
            index.update(fresh)
            self.__save_index(index)
            stats["index_total"] = len(index)
            stats["index_new"] = len(fresh)
            stats["index_source"] = source
            logger.info(
                f"猪猪辅种：索引更新完成（{source}），共 {len(index)} 条"
            )

            target = self.__pick_downloader()
            if not target:
                stats["message"] = "没有可用的辅种下载器"
                logger.error(f"猪猪辅种：{stats['message']}")
                return
            instance = target.instance
            target_rpc = getattr(instance, "trc", None)
            if target_rpc is None:
                stats["message"] = f"辅种下载器 {target.name} 连接未就绪"
                logger.error(f"猪猪辅种：{stats['message']}")
                return
            stats["downloader"] = target.name

            # 辅种机上已有的任务先拍一张 infohash 快照。判重必须在
            # add_torrent 之前做：MP 带 is_paused=True 添加时，遇到已存在
            # 的种子会把它暂停并返回旧任务，光看返回值分不清新老，还会把
            # 正在做种的种子拖去重新校验。
            # 读取失败时置 None：这种情况宁可这轮不新增，也不能当"机器上
            # 什么都没有"硬加，那会把已有的种子全部重新校验一遍。
            existing: Optional[Dict[str, Any]] = {}
            try:
                try:
                    others = target_rpc.get_torrents(arguments=[
                        "hashString", "id", "status", "percentDone",
                    ])
                except TypeError:
                    others = target_rpc.get_torrents()
                for item in others or []:
                    ihash = str(_attr(item, "hash_string", "hashString",
                                      default="") or "").lower()
                    if ihash:
                        existing[ihash] = item
                logger.info(
                    f"猪猪辅种：{target.name} 现有任务 {len(existing)} 个"
                )
            except Exception as err:  # noqa: BLE001
                existing = None
                logger.warning(
                    f"猪猪辅种：读取 {target.name} 现有任务失败，本轮不新增 {err}"
                )

            sources = self.__seed_sources()
            if not sources:
                stats["message"] = "没有可用的主下载器"
                logger.error(f"猪猪辅种：{stats['message']}")
                return
            stats["sources"] = [item.name for item in sources]
            logger.info(
                "猪猪辅种：主 " + "/".join(stats["sources"])
                + f" -> 辅 {target.name}"
            )

            # 主下载器只读，逐个取已完成种子再按 infohash 合并去重。
            local: List[dict] = []
            seen_hash: set = set()
            for service in sources:
                rpc = getattr(service.instance, "trc", None)
                if rpc is None:
                    logger.warning(f"猪猪辅种：主下载器 {service.name} 连接未就绪")
                    continue
                part = self.__local_completed(rpc)
                logger.info(
                    f"猪猪辅种：主 {service.name} 已完成种子 {len(part)} 个"
                )
                for item in part:
                    key = item["hash"] or f"{item['name']}@{item['size']}"
                    if key in seen_hash:
                        continue
                    seen_hash.add(key)
                    item["source"] = service.name
                    local.append(item)
            stats["local_done"] = len(local)
            logger.info(f"猪猪辅种：主下载器合计已完成种子 {len(local)} 个")

            hits = match_by_size(local, index)
            covered = {id(torrent) for torrent, _ in hits}
            missing = [item for item in local if id(item) not in covered]
            if self._seed_search and missing:
                limit = self._seed_search_limit or 0
                logger.info(
                    f"猪猪辅种：索引未命中 {len(missing)} 个，按名称搜索兜底"
                    + (f"（本轮最多 {limit} 个）" if limit else "")
                )
                searched = found = 0
                for torrent in missing:
                    if limit and searched >= limit:
                        stats["message"] = (
                            f"搜索兜底达到上限 {limit} 个，剩余下轮继续"
                        )
                        break
                    searched += 1
                    cands = self.__search_candidates(
                        client, torrent.get("name") or ""
                    )
                    same = [
                        item for item in cands
                        if int(item.get("size") or 0) == int(torrent.get("size") or 0)
                    ]
                    if not same:
                        continue
                    found += 1
                    hits.append((
                        torrent,
                        sorted(same, key=lambda x: -(x.get("seeders") or 0)),
                    ))
                stats["search_hit"] = found
                logger.info(
                    f"猪猪辅种：搜索兜底 {searched} 个，体积命中 {found} 个"
                )
            if self._seed_min_seeders > 0:
                hits = [
                    (t, [c for c in cands if c["seeders"] >= self._seed_min_seeders])
                    for t, cands in hits
                ]
                hits = [(t, c) for t, c in hits if c]
            stats["hit"] = len(hits)
            logger.info(f"猪猪辅种：体积粗筛命中 {len(hits)} 个种子")

            added = skipped = failed = matched = site_skipped = 0
            # 同一个猪猪种子只会有一个 infohash，TR 不接受重复添加；
            # 多个本地条目命中同一个站内种子时只加一次。
            added_pig: set = set()
            site_seeding = self.__site_seeding_ids(stats)
            for torrent, candidates in hits:
                if added >= self._seed_max_add:
                    stats["message"] = f"达到单轮上限 {self._seed_max_add}，剩余下轮继续"
                    break
                if not candidates:
                    continue
                local_sig = file_signature(torrent["files"])
                picked = None
                for candidate in candidates:
                    if candidate["id"] in added_pig:
                        skipped += 1
                        break
                    if candidate["id"] in site_seeding:
                        # 站点已把这个种子算在本账号做种里，不必再下载
                        # .torrent 校验；换辅种机重辅时关掉这个开关。
                        skipped += 1
                        site_skipped += 1
                        break
                    seeded_now = self.get_data(_SEEDED_KEY) or {}
                    # 同一个猪猪种子在同一台辅种机上只会有一个任务，所以判重
                    # 只看 pig_id + 目标机；用本地 infohash 会让同一资源在不
                    # 同下载器上各记一遍，每轮都白跑一次添加。
                    key = f"{candidate['id']}@{target.name}"
                    if key in seeded_now:
                        skipped += 1
                        break
                    blob = client.download(candidate["id"])
                    if not blob:
                        failed += 1
                        continue
                    try:
                        info = torrent_files(blob)
                    except Exception as err:  # noqa: BLE001
                        failed += 1
                        logger.warning(
                            f"猪猪辅种：种子解析失败 PIG#{candidate['id']} {err}"
                        )
                        continue
                    if file_signature(info["files"]) != local_sig:
                        continue
                    matched += 1
                    picked = (candidate, blob)
                    break

                if not picked:
                    continue
                candidate, blob = picked
                if self._seed_dryrun:
                    logger.info(
                        f"猪猪辅种：[演练] 命中 PIG#{candidate['id']} "
                        f"{torrent.get('source', '')} -> {target.name} "
                        f"{torrent['dir']} {torrent['name'][:50]}"
                    )
                    added_pig.add(candidate["id"])
                    skipped += 1
                    continue
                result = self.__add_seed(
                    instance, target_rpc, target.name, torrent, candidate, blob,
                    existing,
                )
                if result == "added":
                    added += 1
                    added_pig.add(candidate["id"])
                elif result == "duplicate":
                    added_pig.add(candidate["id"])
                    skipped += 1
                else:
                    failed += 1

            stats.update({
                "matched": matched,
                "added": added,
                "skipped": skipped,
                "site_skipped": site_skipped,
                "failed": failed,
            })
            elapsed = int(time.time() - started)
            stats["elapsed"] = elapsed
            stats["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                f"猪猪辅种完成：清单一致 {matched}，新增 {added}，"
                f"已收录 {skipped}，失败 {failed}，耗时 {elapsed} 秒"
            )
            if self._seed_notify and (added or failed):
                self.__notify_seed(stats)
        except PiggoError as err:
            stats["message"] = str(err)
            logger.error(f"猪猪辅种：站点交互失败 {err}")
        except Exception as err:  # noqa: BLE001
            stats["message"] = f"异常：{err}"
            logger.exception(f"猪猪辅种：执行异常 {err}")
        finally:
            if client is not None:
                client.close()
            stats.setdefault("elapsed", int(time.time() - started))
            stats.setdefault(
                "finished_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            self.save_data(_SEED_STATS_KEY, stats)
            with self._seed_lock:
                self._seed_running = False

    # ------------------------------------------------------------------ 校验跟踪
    def track_verify(self) -> None:
        """按下载器真实状态收敛：校验完才启动，确认在跑才算做种。

        percentDone 在校验开始前就是 1.0，只看它会抢跑：先把任务标成已做种，
        等 TR 校验完又把暂停态还回来，之后就再也不会被重新启动。
        """
        if not self._seed_enabled:
            return
        seeded = self.get_data(_SEEDED_KEY) or {}
        if not seeded:
            return
        service = self.__pick_downloader()
        if not service:
            return
        instance = service.instance
        rpc = getattr(instance, "trc", None)
        if rpc is None:
            return

        ready: List[int] = []
        stuck: List[Tuple[str, int]] = []
        seeding_now = 0
        changed = False
        target_name = service.name.lower()
        for key, record in seeded.items():
            # 记录里带了下载器名：换了辅种机之后同号 id 会撞上无关种子，
            # 拿旧 id 去新机器上 start 是会误伤别人的。
            if str(record.get("dl") or "").lower() != target_name:
                continue
            torrent_id = record.get("tr_id")
            if not torrent_id:
                continue
            try:
                torrent = rpc.get_torrent(torrent_id)
            except Exception:  # noqa: BLE001
                continue
            status = str(_attr(torrent, "status", default="") or "")
            progress = float(
                _attr(torrent, "percent_done", "percentDone", default=0) or 0
            )
            if status == "seeding":
                seeding_now += 1
                if record.get("state") != "seeding":
                    record["state"] = "seeding"
                    record["start_tries"] = 0
                    record["finished_at"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    changed = True
                continue
            if status in ("checking", "check pending", "check_pending"):
                continue
            if progress >= 0.999:
                # 已标做种但其实停着的（校验完被 TR 暂停），再拉几次
                tries = int(record.get("start_tries") or 0)
                if tries >= 3:
                    continue
                record["start_tries"] = tries + 1
                record["state"] = "verifying"
                ready.append(torrent_id)
                changed = True
                continue
            # 校验跑完但进度不满：辅种机上没有这份数据。留着它会变成
            # 一个待下载任务，等哪天队列放行就把整包重下一遍，必须清掉。
            if self.__age_seconds(record) > 1200:
                stuck.append((key, torrent_id))

        if ready and self._seed_auto_start:
            try:
                instance.start_torrents(ready)
                logger.info(f"猪猪辅种：{len(ready)} 个已校验，提交做种")
            except Exception as err:  # noqa: BLE001
                logger.warning(f"猪猪辅种：启动做种失败 {err}")
        for key, torrent_id in stuck:
            try:
                rpc.remove_torrent(torrent_id, delete_data=False)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"猪猪辅种：移除无效辅种 TR#{torrent_id} 失败 {err}")
                continue
            record = seeded.get(key) or {}
            record["state"] = "nodata"
            record["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True
            logger.warning(
                f"猪猪辅种：{target.name} 上没有 {str(record.get('name', ''))[:40]} "
                f"的数据，已移除 TR#{torrent_id}"
            )
        if changed:
            self.save_data(_SEEDED_KEY, seeded)
        if seeding_now:
            logger.debug(f"猪猪辅种：当前做种中 {seeding_now} 个")

    # ------------------------------------------------------------------ 内部工具
    def __client(self) -> PiggoClient:
        return PiggoClient(
            base=DEFAULT_BASE,
            username=self._username,
            password=self._password,
            visitorid=self._visitorid,
            proxy=self._proxy,
            logger=logger.info,
        )

    def __api_client(self) -> Optional[PiggoApiClient]:
        """没开开关或没配令牌时返回 None，整条新链路自动退化成旧行为。"""
        if not (self._seed_api_index and self._api_token):
            return None
        return PiggoApiClient(
            token=self._api_token,
            proxy=self._proxy,
            logger=logger.info,
        )

    def __refresh_index(
        self, client: PiggoClient, index: Dict[int, dict]
    ) -> Tuple[Dict[int, dict], str]:
        """优先用实验室 API 增量刷新，失败回退前台列表。"""
        api = self.__api_client()
        if api is not None:
            try:
                saved = self.get_data(_API_CURSOR_KEY) or {}
                last = str(saved.get("last_added_at") or "")
                if not last:
                    # 老索引是前台列表建的，没有 added_at；能推出一个水位
                    # 就少拉一轮全量
                    last = max(
                        (
                            str(item.get("added_at") or "")
                            for item in index.values()
                            if isinstance(item, dict)
                        ),
                        default="",
                    )
                fresh, cursor = api.build_incremental(
                    last_added_at=last,
                    max_pages=self._seed_pages or 100,
                    on_progress=lambda page, total, count: logger.info(
                        f"猪猪辅种：实验室 API 索引 {page}/{total}，本轮 {count} 条"
                    ),
                )
                if cursor and cursor != last:
                    self.save_data(_API_CURSOR_KEY, {
                        "last_added_at": cursor,
                        "updated": int(time.time()),
                    })
                logger.info(
                    f"猪猪辅种：实验室 API 增量 {len(fresh)} 条，"
                    f"水位 {last or '无'} -> {cursor or '无'}"
                )
                return fresh, "api"
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    f"猪猪辅种：实验室 API 索引失败，回退前台列表 {err}"
                )
        fresh = client.build_index(
            pages=self._seed_pages,
            on_progress=lambda page, total, count: logger.info(
                f"猪猪辅种：索引 {page}/{total}，累计 {count} 条"
            ),
        )
        return fresh, "web"

    def __site_seeding_ids(self, stats: dict) -> set:
        """拿站点当前做种清单做一层判重，失败时降级为只用本地记录。"""
        api = self.__api_client()
        if api is None or not self._seed_skip_site_seeding:
            return set()
        try:
            ids = api.seeding_ids()
            stats["site_seeding"] = len(ids)
            logger.info(f"猪猪辅种：站点当前做种 {len(ids)} 个，纳入判重")
            return ids
        except Exception as err:  # noqa: BLE001
            logger.warning(
                f"猪猪辅种：读取站点做种清单失败，本轮只用本地判重 {err}"
            )
            return set()

    @staticmethod
    def __to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __join_names(value: Any) -> str:
        """下载器名统一存成逗号分隔字符串，表单多选给的是列表。"""
        if isinstance(value, (list, tuple, set)):
            return ",".join(
                str(item).strip() for item in value if str(item).strip()
            )
        return str(value or "").strip()

    @staticmethod
    def __age_seconds(record: dict) -> float:
        """记录写入到现在过了多少秒，用于给校验留出宽限期。"""
        raw = str(record.get("at") or "")
        try:
            stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0.0
        return (datetime.now() - stamp).total_seconds()

    @staticmethod
    def __torrent_age(value: Any) -> Optional[float]:
        """种子加入时间距今多少秒，兼容 datetime 与时间戳两种形态。"""
        if value is None:
            return None
        if hasattr(value, "timestamp"):
            try:
                return time.time() - value.timestamp()
            except (OSError, ValueError):
                return None
        try:
            return time.time() - float(value)
        except (TypeError, ValueError):
            return None

    def __save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "domain": self._domain,
            "username": self._username,
            "password": self._password,
            "visitorid": self._visitorid,
            "token": self._token,
            "api_token": self._api_token,
            "site_id": self._site_id,
            "proxy": self._proxy,
            "seed_enabled": self._seed_enabled,
            "seed_api_index": self._seed_api_index,
            "seed_skip_site_seeding": self._seed_skip_site_seeding,
            "seed_cron": self._seed_cron,
            "seed_downloader": self._seed_downloader,
            "seed_source_downloader": self._seed_source_downloader,
            "seed_pages": self._seed_pages,
            "seed_max_add": self._seed_max_add,
            "seed_label": self._seed_label,
            "seed_min_seeders": self._seed_min_seeders,
            "seed_auto_start": self._seed_auto_start,
            "seed_dryrun": self._seed_dryrun,
            "seed_notify": self._seed_notify,
            "seed_onlyonce": False,
            "seed_search": self._seed_search,
            "seed_search_limit": self._seed_search_limit,
        })

    @property
    def __index_path(self) -> str:
        return os.path.join(str(self.get_data_path()), _INDEX_FILE)

    def __load_index(self) -> Dict[int, dict]:
        path = self.__index_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        items = payload.get("items") if isinstance(payload, dict) else payload
        out: Dict[int, dict] = {}
        for key, value in (items or {}).items():
            try:
                out[int(key)] = value
            except (TypeError, ValueError):
                continue
        return out

    def __save_index(self, index: Dict[int, dict]) -> None:
        payload = {"updated": int(time.time()), "items": index}
        temp = self.__index_path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(temp, self.__index_path)
        except OSError as err:
            logger.warning(f"猪猪辅种：索引写入失败 {err}")

    def __transmission_services(self) -> Dict[str, Any]:
        """当前可用的 Transmission 服务，键是 MP 里配置的下载器名。"""
        try:
            services = DownloaderHelper().get_services() or {}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"猪猪辅种：获取下载器列表失败 {err}")
            return {}
        return {
            name: info for name, info in services.items()
            if info and info.type == "transmission"
        }

    def __pick_downloader(self):
        """辅种下载器：新加的辅种只落这一台，不做静默降级。

        主辅分离之后，辅种下错机器等于把整包重下一遍，宁可本轮跳过。
        """
        services = self.__transmission_services()
        if not services:
            logger.warning("猪猪辅种：MP 里没有配置 Transmission 下载器")
            return None
        if not self._seed_downloader:
            for name, info in services.items():
                if info.instance and not info.instance.is_inactive():
                    logger.warning(f"猪猪辅种：未指定辅种下载器，退回使用 {name}")
                    return info
            return None
        want = self._seed_downloader.strip().lower()
        for name, info in services.items():
            if name.lower() != want:
                continue
            if not info.instance or info.instance.is_inactive():
                logger.warning(f"猪猪辅种：辅种下载器 {name} 未连接，本轮跳过")
                return None
            return info
        logger.warning(
            f"猪猪辅种：找不到辅种下载器 {self._seed_downloader}，"
            f"可用 {list(services)}"
        )
        return None

    def __seed_sources(self) -> List[Any]:
        """主下载器：只提供「本地已完成种子」清单，不往上写任何东西。"""
        services = self.__transmission_services()
        wants = [
            item.strip().lower()
            for item in self._seed_source_downloader.split(",")
            if item.strip()
        ]
        picked: List[Any] = []
        for name, info in services.items():
            if wants and name.lower() not in wants:
                continue
            if not info.instance or info.instance.is_inactive():
                logger.warning(f"猪猪辅种：主下载器 {name} 未连接，跳过")
                continue
            picked.append(info)
        if wants:
            for name in wants:
                if all(name != info.name.lower() for info in picked):
                    logger.warning(f"猪猪辅种：找不到主下载器 {name}")
        return picked

    def __local_completed(self, rpc) -> List[dict]:
        """取下载器里已完成种子，必须带 files 字段用于清单比对。"""
        try:
            # Transmission 只返回 fields 里点名的字段：漏 percentDone 会让
            # 判断全部打成未完成，漏 priorities 会让 get_files() 直接抛错。
            torrents = rpc.get_torrents(arguments=[
                "files", "priorities", "wanted", "percentDone",
                "totalSize", "hashString", "downloadDir", "name", "status",
            ])
        except TypeError:
            torrents = rpc.get_torrents()
        torrents = list(torrents or [])
        logger.info(f"猪猪辅种：下载器返回 {len(torrents)} 个种子")
        out: List[dict] = []
        for torrent in torrents or []:
            progress = float(
                _attr(torrent, "percent_done", "percentDone", default=0) or 0
            )
            if progress < 0.999:
                continue
            files = _files_of(torrent)
            if not files:
                continue
            out.append({
                "id": _attr(torrent, "id"),
                "name": str(_attr(torrent, "name", default="") or ""),
                "hash": str(
                    _attr(torrent, "hash_string", "hashString", default="") or ""
                ),
                "size": int(_attr(torrent, "total_size", "totalSize", default=0) or 0),
                "dir": str(_attr(torrent, "download_dir", "downloadDir", default="") or ""),
                "files": files,
            })
        return out

    def __search_candidates(self, client: PiggoClient, name: str) -> List[dict]:
        """按名称搜站内种子，用来捞索引覆盖不到的资源。

        列表接口 total_capped=True，最多只给 100 页；站点真实种子上限
        远超这个数，所以体积粗筛没命中的种子要再走一次关键词搜索。
        """
        keyword = search_keyword(name)
        if not keyword:
            return []
        raw = client.api("/v1/web/torrents?page=1&q=" + quote(keyword))
        if not raw:
            return []
        try:
            data = json.loads(raw).get("data") or {}
        except (ValueError, AttributeError):
            return []
        return [item for item in (data.get("items") or []) if isinstance(item, dict)]

    def __add_seed(
        self,
        instance,
        rpc,
        target_name: str,
        torrent: dict,
        candidate: dict,
        blob: bytes,
        existing: Optional[Dict[str, Any]] = None,
    ) -> str:
        key = f"{candidate['id']}@{target_name}"
        labels = [self._seed_label] if self._seed_label else None
        seeded = self.get_data(_SEEDED_KEY) or {}

        # 判重放在 add_torrent 之前，用站点种子自己的 infohash 去比对辅种机上
        # 的现有任务。不能靠 add_torrent 的返回值分辨：MP 带 is_paused=True
        # 添加时遇到同一个种子，会把正在做种的任务暂停，再把旧任务返回回来，
        # 插件看着像刚加的，就顺手触发了一次校验。
        if existing is None:
            logger.warning(
                f"猪猪辅种：{target_name} 现有任务未知，跳过 PIG#{candidate['id']}"
            )
            return "failed"
        info_hash = torrent_infohash(blob)
        if not info_hash:
            logger.warning(
                f"猪猪辅种：PIG#{candidate['id']} 种子解析不出 infohash，跳过"
            )
            return "failed"
        found = existing.get(info_hash)
        if found is not None:
            tr_id = _attr(found, "id")
            status = str(_attr(found, "status", default="") or "")
            percent = float(
                _attr(found, "percent_done", "percentDone", default=1.0) or 0
            )
            done = percent >= 0.999
            if done and status in ("stopped", "paused"):
                try:
                    rpc.start_torrent(tr_id)
                    logger.info(
                        f"猪猪辅种：{target_name} 上 PIG#{candidate['id']} "
                        f"TR#{tr_id} 被暂停过，已恢复做种"
                    )
                except Exception as err:  # noqa: BLE001
                    logger.warning(
                        f"猪猪辅种：恢复做种失败 TR#{tr_id} {err}"
                    )
            seeded[key] = {
                "tr_id": tr_id,
                "dl": target_name,
                "pig_id": candidate["id"],
                "name": torrent["name"],
                "dir": torrent["dir"],
                "state": "seeding" if done else "verifying",
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dup": True,
            }
            self.save_data(_SEEDED_KEY, seeded)
            logger.info(
                f"猪猪辅种：{target_name} 上已有 PIG#{candidate['id']} "
                f"TR#{tr_id}（{status or '未知'}），跳过"
            )
            return "duplicate"

        try:
            created = instance.add_torrent(
                content=blob,
                is_paused=True,
                download_dir=torrent["dir"],
                labels=labels,
            )
        except Exception as err:  # noqa: BLE001
            message = str(err).lower()
            if "duplicate" in message or "already" in message:
                seeded[key] = {
                    "tr_id": None,
                    "dl": target_name,
                    "pig_id": candidate["id"],
                    "name": torrent["name"],
                    "dir": torrent["dir"],
                    "state": "seeding",
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "dup": True,
                }
                self.save_data(_SEEDED_KEY, seeded)
                return "duplicate"
            logger.warning(f"猪猪辅种：添加失败 PIG#{candidate['id']} {err}")
            return "failed"

        new_id = _attr(created, "id")
        if not new_id:
            logger.warning(f"猪猪辅种：添加未返回 id PIG#{candidate['id']}")
            return "failed"
        # 走到这里说明 infohash 快照里没有这个种子，理论上是真新增。再加两道
        # 兜底，因为一旦误判就会把别人正在做种的任务拖去全盘校验：
        #   added_date 比 15 分钟早 → 老任务；
        #   status 是 seeding → 带 is_paused 加进来的不可能已经在做种；
        #   percent_done 已是 100% → TR 不会在没校验前给新任务满进度。
        age = self.__torrent_age(_attr(created, "added_date", "addedDate"))
        status = str(_attr(created, "status", default="") or "")
        progress = float(
            _attr(created, "percent_done", "percentDone", default=0) or 0
        )
        # 直接问下载器要这个任务的真实状态，不用 add_torrent 的返回对象
        # （MP 可能对字段做过转换）。新加的任务在 TR 里必然 0%，只有旧任务
        # 才会满进度或已经在做种。
        try:
            live = rpc.get_torrent(new_id)
            live_status = str(_attr(live, "status", default="") or "")
            live_progress = float(
                _attr(live, "percent_done", "percentDone", default=0) or 0
            )
            if live_status == "seeding" or live_progress >= 0.999:
                status = live_status or status
                progress = max(progress, live_progress)
        except Exception:  # noqa: BLE001
            pass
        if (
            (age is not None and age > 900)
            or status == "seeding"
            or progress >= 0.999
        ):
            seeded[key] = {
                "tr_id": new_id,
                "dl": target_name,
                "pig_id": candidate["id"],
                "name": torrent["name"],
                "dir": torrent["dir"],
                "state": "seeding" if status == "seeding" else "verifying",
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dup": True,
            }
            self.save_data(_SEEDED_KEY, seeded)
            logger.info(
                f"猪猪辅种：{target_name} 上已有 PIG#{candidate['id']} "
                f"TR#{new_id}，跳过"
            )
            return "duplicate"
        try:
            rpc.verify_torrent(new_id)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"猪猪辅种：触发校验失败 TR#{new_id} {err}")
        seeded[key] = {
            "tr_id": new_id,
            "dl": target_name,
            "pig_id": candidate["id"],
            "name": torrent["name"],
            "dir": torrent["dir"],
            "state": "verifying",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data(_SEEDED_KEY, seeded)
        logger.info(
            f"猪猪辅种：{torrent.get('source', '')} -> {target_name} "
            f"PIG#{candidate['id']} TR#{new_id} {torrent['name'][:50]}"
        )
        return "added"

    def __notify_seed(self, stats: dict) -> None:
        sources = stats.get("sources") or []
        text = (
            f"主 {'/'.join(sources) or '自动'} -> 辅 {stats.get('downloader') or '自动'}\n"
            f"索引 {stats.get('index_total', 0)} 条（本轮刷新 {stats.get('index_new', 0)}）\n"
            f"索引来源 {'实验室 API' if stats.get('index_source') == 'api' else '前台列表'}\n"
            f"本地已完成 {stats.get('local_done', 0)} 个\n"
            f"体积命中 {stats.get('hit', 0)} / 清单一致 {stats.get('matched', 0)}\n"
            f"新增 {stats.get('added', 0)}，已收录 {stats.get('skipped', 0)}，"
            f"失败 {stats.get('failed', 0)}\n"
            f"耗时 {stats.get('elapsed', 0)} 秒"
        )
        if stats.get("message"):
            text += f"\n说明：{stats['message']}"
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【猪猪自动辅种任务完成】",
                text=text,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"猪猪辅种：发送通知失败 {err}")
