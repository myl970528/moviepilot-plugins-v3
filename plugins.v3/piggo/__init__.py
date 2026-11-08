# -*- coding: utf-8 -*-
"""猪猪 (piggo.me) 站点一体化插件。

一个插件覆盖三件事：

  索引：把 PiggoSpider 注入 MP 索引器体系，给 piggo.me 注册索引配置，
        并启动本地下载代理，让站点搜索与下载可用。
  辅种：拉全站列表建本地索引，按「总字节数相等 + 文件清单完全一致」
        跨站匹配本地已完成种子，校验通过后自动做种。
  签到：站点把签到做成万年历（/calendar），每日调 /calendar/attendance，
        风控命中时用图片验证码补交。

登录统一走站点自研 SRP-6a，token 失效由各自模块自动重登。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from apscheduler.triggers.cron import CronTrigger

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

from .engine import (
    DEFAULT_BASE,
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
_DEFAULT_VISITORID = ""
_DEFAULT_PROXY = ""
_DEFAULT_SEED_CRON = "15 5 * * *"
_DEFAULT_SIGN_CRON = "20 8 * * *"

_INDEX_FILE = "piggo_index.json"
_SEEDED_KEY = "seed_seeded"
_SEED_STATS_KEY = "seed_stats"
_SIGN_STATS_KEY = "sign_stats"


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
    plugin_desc = "猪猪站一体化：索引搜索、自动辅种、每日签到。"
    # 插件图标
    plugin_icon = "spider.png"
    # 插件版本
    plugin_version = "2.0"
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

    # ---- 辅种
    _seed_enabled: bool = False
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

    # ---- 签到
    _sign_enabled: bool = False
    _sign_cron: str = _DEFAULT_SIGN_CRON
    _sign_notify: bool = True
    _sign_captcha: bool = True
    _sign_onlyonce: bool = False

    _seed_running: bool = False
    _seed_lock = threading.Lock()
    _ocr: Any = None

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[dict] = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled"))
            self._token = str(config.get("token") or "").strip()
            self._username = str(config.get("username") or "").strip()
            self._password = str(config.get("password") or "").strip()
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

            self._sign_enabled = bool(config.get("sign_enabled"))
            self._sign_cron = str(config.get("sign_cron") or _DEFAULT_SIGN_CRON).strip()
            self._sign_notify = bool(config.get("sign_notify", True))
            self._sign_captcha = bool(config.get("sign_captcha", True))
            self._sign_onlyonce = bool(config.get("sign_onlyonce"))

        try:
            self._install()
        except Exception as err:  # noqa: BLE001
            logger.error(f"猪猪：索引安装失败 {err}")

        if self._seed_onlyonce:
            self._seed_onlyonce = False
            self.__save_config()
            logger.info("猪猪辅种：收到立即执行指令，60 秒后开始")
            threading.Timer(60, self.run_seed_task).start()
        if self._sign_onlyonce:
            self._sign_onlyonce = False
            self.__save_config()
            logger.info("猪猪签到：收到立即执行指令，20 秒后开始")
            threading.Timer(20, self.run_sign_task).start()

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

        if self._sign_enabled:
            trigger = self.__cron(self._sign_cron, "签到")
            if trigger:
                services.append({
                    "id": "PiggoSignIn",
                    "name": "猪猪每日签到",
                    "trigger": trigger,
                    "func": self.run_sign_task,
                    "kwargs": {},
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
        downloaders = []
        try:
            services = DownloaderHelper().get_services()
            downloaders = [
                {"title": f"{name} ({info.type})", "value": name}
                for name, info in (services or {}).items()
                if info and info.type == "transmission"
            ]
        except Exception as err:  # noqa: BLE001
            logger.warning(f"猪猪：读取下载器列表失败 {err}")

        return [
            {
                "component": "VForm",
                "content": [
                    self.__section(
                        "站点与索引",
                        "账号密码用于自动登录，token 留空即可（插件自动获取并续期）。"
                        "开启索引后，站点管理里的猪猪站可搜索与下载。",
                    ),
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__switch("enabled", "启用索引")),
                            self.__col(3, self.__field("domain", "域名")),
                            self.__col(3, self.__field("username", "账号")),
                            self.__col(3, self.__field("password", "密码", "password")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(6, self.__field("visitorid", "visitorid")),
                            self.__col(6, self.__field("proxy", "代理地址（留空为直连）")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(12, self.__field(
                                "token", "token（可留空，自动维护）")),
                        ],
                    },

                    self.__section(
                        "自动辅种",
                        "主辅分离：主下载器只负责提供「本地已完成种子」清单，"
                        "辅种下载器只负责新增辅种，两台各管一头。"
                        "按总字节数相等 + 文件清单完全一致跨站匹配，校验满 100% 才开始做种。"
                        "索引页数填 0 表示拉全站（约 3-5 分钟）。",
                    ),
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__switch("seed_enabled", "启用辅种")),
                            self.__col(3, self.__field("seed_cron", "执行周期")),
                            self.__col(3, self.__switch("seed_notify", "辅种通知")),
                            self.__col(3, self.__switch("seed_onlyonce", "立即辅种一次")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(6, {
                                "component": "VSelect",
                                "props": {
                                    "model": "seed_source_downloader",
                                    "label": "主下载器（扫描已完成种子）",
                                    "items": downloaders,
                                    "multiple": True,
                                    "chips": True,
                                    "clearable": True,
                                    "hint": "可多选；留空表示所有可用的 Transmission",
                                    "persistent-hint": True,
                                },
                            }),
                            self.__col(6, {
                                "component": "VSelect",
                                "props": {
                                    "model": "seed_downloader",
                                    "label": "辅种下载器（只加辅种）",
                                    "items": downloaders,
                                    "clearable": True,
                                    "hint": "新增辅种只加这一台；留空退回第一个可用 Transmission",
                                    "persistent-hint": True,
                                },
                            }),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__field("seed_pages", "索引页数（0 全量）")),
                            self.__col(3, self.__field("seed_max_add", "单轮最多添加")),
                            self.__col(3, self.__field("seed_label", "做种标签")),
                            self.__col(3, self.__field("seed_min_seeders", "最低做种人数")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(6, self.__switch("seed_auto_start", "校验完自动做种")),
                            self.__col(6, self.__switch("seed_dryrun", "只匹配不添加")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(6, self.__switch(
                                "seed_search", "索引未命中时按名称搜索兜底")),
                            self.__col(6, self.__field(
                                "seed_search_limit", "单轮搜索上限")),
                        ],
                    },

                    self.__section(
                        "每日签到",
                        "站点签到做成万年历，接口为 /v1/web/calendar。"
                        "风控命中时返回 422 要求人机验证，站点前端没有配置任何"
                        "验证码组件，浏览器同样签不了，只能去网页手动签到；"
                        "插件会把结果推送提醒。",
                    ),
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(3, self.__switch("sign_enabled", "启用签到")),
                            self.__col(3, self.__field("sign_cron", "执行周期")),
                            self.__col(3, self.__switch("sign_notify", "签到通知")),
                            self.__col(3, self.__switch("sign_onlyonce", "立即签到一次")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__col(4, self.__switch(
                                "sign_captcha", "尝试识别图片验证码（通常无效）")),
                        ],
                    },
                ],
            }
        ], {
            "enabled": self._enabled,
            "domain": self._domain,
            "username": self._username,
            "password": self._password,
            "visitorid": self._visitorid,
            "token": self._token,
            "site_id": self._site_id,
            "proxy": self._proxy,
            "seed_enabled": self._seed_enabled,
            "seed_cron": self._seed_cron,
            "seed_downloader": self._seed_downloader,
            "seed_source_downloader": [
                item.strip()
                for item in self._seed_source_downloader.split(",")
                if item.strip()
            ],
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
            "sign_enabled": self._sign_enabled,
            "sign_cron": self._sign_cron,
            "sign_notify": self._sign_notify,
            "sign_captcha": self._sign_captcha,
            "sign_onlyonce": False,
        }

    @staticmethod
    def __section(title: str, desc: str) -> dict:
        return {
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "density": "compact",
                        "title": title,
                        "text": desc,
                    },
                }],
            }],
        }

    @staticmethod
    def __col(cols: int, component: dict) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": cols},
            "content": [component],
        }

    @staticmethod
    def __switch(model: str, label: str) -> dict:
        return {"component": "VSwitch", "props": {"model": model, "label": label}}

    @staticmethod
    def __field(model: str, label: str, field_type: str = "text") -> dict:
        return {
            "component": "VTextField",
            "props": {"model": model, "label": label, "type": field_type},
        }

    # ------------------------------------------------------------------ 详情页
    def get_page(self) -> List[dict]:
        seed_stats = self.get_data(_SEED_STATS_KEY) or {}
        seeded = self.get_data(_SEEDED_KEY) or {}
        sign_stats = self.get_data(_SIGN_STATS_KEY) or {}

        lines: List[str] = ["【索引】"]
        if self._enabled:
            lines.append(f"  已启用，索引 ID {self._site_id}，域名 {self._domain}")
        else:
            lines.append("  未启用")

        lines.append("")
        lines.append("【辅种】")
        lines.append(
            f"  主下载器：{self._seed_source_downloader or '全部 Transmission'}"
            "（只扫描已完成种子）"
        )
        lines.append(
            f"  辅种下载器：{self._seed_downloader or '自动选第一个 Transmission'}"
            "（只新增辅种）"
        )
        if seed_stats:
            lines.append(f"  上次运行：{seed_stats.get('finished_at', '-')}"
                         f"，耗时 {seed_stats.get('elapsed', 0)} 秒")
            lines.append(f"  索引 {seed_stats.get('index_total', 0)} 条，"
                         f"本地已完成 {seed_stats.get('local_done', 0)} 个")
            lines.append(f"  体积命中 {seed_stats.get('hit', 0)}，"
                         f"清单一致 {seed_stats.get('matched', 0)}，"
                         f"新增 {seed_stats.get('added', 0)}，"
                         f"已收录 {seed_stats.get('skipped', 0)}，"
                         f"失败 {seed_stats.get('failed', 0)}")
            if seed_stats.get("message"):
                lines.append(f"  说明：{seed_stats['message']}")
        else:
            lines.append("  尚未运行")
        seeding = sum(1 for v in seeded.values() if v.get("state") == "seeding")
        nodata = sum(1 for v in seeded.values() if v.get("state") == "nodata")
        verifying = len(seeded) - seeding - nodata
        lines.append(
            f"  已做种 {seeding} 个，校验中 {verifying} 个，无数据 {nodata} 个"
        )

        lines.append("")
        lines.append("【签到】")
        if sign_stats:
            lines.append(f"  上次运行：{sign_stats.get('finished_at', '-')}")
            lines.append(f"  结果：{sign_stats.get('message', '-')}")
        else:
            lines.append("  尚未运行")

        recent = sorted(
            seeded.values(), key=lambda x: x.get("at", ""), reverse=True
        )[:10]
        if recent:
            lines.append("")
            lines.append("最近辅种：")
            for item in recent:
                state = {
                    "seeding": "做种",
                    "nodata": "无数据",
                }.get(str(item.get("state")), "校验中")
                lines.append(
                    f"  [{state}] {item.get('dl', '')} TR#{item.get('tr_id')} "
                    f"PIG#{item.get('pig_id')} {str(item.get('name', ''))[:48]}"
                )

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [{
                                    "component": "VCardText",
                                    "props": {
                                        "style": "white-space: pre-wrap;"
                                                 "font-family: monospace;"
                                                 "line-height: 1.7;",
                                    },
                                    "text": "\n".join(lines),
                                }],
                            }],
                        }],
                    }
                ],
            }
        ]

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
                "path": "/sign",
                "endpoint": self.api_sign,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "立即执行签到",
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

    async def api_sign(self) -> dict:
        threading.Thread(target=self.run_sign_task, daemon=True).start()
        return {"success": True, "message": "已启动签到"}

    async def api_status(self) -> dict:
        return {
            "success": True,
            "seed_running": self._seed_running,
            "seed_stats": self.get_data(_SEED_STATS_KEY) or {},
            "seeded": len(self.get_data(_SEEDED_KEY) or {}),
            "sign_stats": self.get_data(_SIGN_STATS_KEY) or {},
        }

    # ------------------------------------------------------------------ 签到
    def run_sign_task(self) -> None:
        if not self._sign_enabled:
            logger.info("猪猪签到：未启用，跳过")
            return
        client = None
        message = ""
        success = False
        try:
            client = self.__client()
            client.login()
            success, message = client.sign_in(self.__captcha_solver())
            log = logger.info if success else logger.warning
            log(f"猪猪签到：{message}")
        except PiggoError as err:
            message = f"站点交互失败：{err}"
            logger.error(f"猪猪签到：{message}")
        except Exception as err:  # noqa: BLE001
            message = f"异常：{err}"
            logger.exception(f"猪猪签到：{message}")
        finally:
            if client is not None:
                client.close()
            self.save_data(_SIGN_STATS_KEY, {
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": success,
                "message": message,
            })
            if self._sign_notify:
                try:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【猪猪签到】" + ("成功" if success else "未完成"),
                        text=message,
                    )
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"猪猪签到：发送通知失败 {err}")

    def __captcha_solver(self) -> Optional[Callable[[bytes], str]]:
        """图片验证码识别。ddddocr 缺失时返回 None，签到降级为提示人工。"""
        if not self._sign_captcha:
            return None
        if self._ocr is None:
            try:
                # ddddocr 装在 /config 下而非镜像里，容器重建不会丢；
                # 需要手工把这个目录挂到 sys.path 上。
                packages = "/config/py_packages"
                if os.path.isdir(packages) and packages not in sys.path:
                    sys.path.insert(0, packages)
                import ddddocr  # noqa: PLC0415

                self._ocr = ddddocr.DdddOcr(show_ad=False)
                logger.info("猪猪签到：验证码识别器已加载")
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    f"猪猪签到：ddddocr 不可用（{err}），"
                    f"签到遇验证码将提示人工处理"
                )
                return None
        ocr = self._ocr
        return lambda image: ocr.classification(image)

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
            fresh = client.build_index(
                pages=self._seed_pages,
                on_progress=lambda page, total, count: logger.info(
                    f"猪猪辅种：索引 {page}/{total}，累计 {count} 条"
                ),
            )
            index.update(fresh)
            self.__save_index(index)
            stats["index_total"] = len(index)
            stats["index_new"] = len(fresh)
            logger.info(f"猪猪辅种：索引更新完成，共 {len(index)} 条")

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

            added = skipped = failed = matched = 0
            # 同一个猪猪种子只会有一个 infohash，TR 不接受重复添加；
            # 多个本地条目命中同一个站内种子时只加一次。
            added_pig: set = set()
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
            "site_id": self._site_id,
            "proxy": self._proxy,
            "seed_enabled": self._seed_enabled,
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
            "sign_enabled": self._sign_enabled,
            "sign_cron": self._sign_cron,
            "sign_notify": self._sign_notify,
            "sign_captcha": self._sign_captcha,
            "sign_onlyonce": False,
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
