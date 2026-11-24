# -*- coding: utf-8 -*-
"""拆包辅种 PackCrossSeed。

从 MoviePilot 已配置的下载器里读「已完成任务」，把整包按电影 / 单集拆开，
逐个到 MP 已配置的站点搜索独立种子，做 piece 级内容校验，确认一致后用
硬链接生成不占空间的副本，交给目标下载器重新校验并辅种。原包原任务不动。

隐私约定（重要）：
  - 源码不含任何站点域名 / IP / 账号 / token / 目录等个人信息，默认值全空。
  - 站点列表、下载器列表在 get_form() 时从 MoviePilot 运行时读取并注入表单。
  - 站点 Cookie 只在内存里交给 MP 的种子下载服务使用，不写日志、不落盘。
  - 公开发布以中性维护者身份进行。
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.sdk.logging import logger
from app.sdk.network import RequestUtils, SitesHelper
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

from .engine import (
    Candidate,
    Operation,
    Unit,
    _is_video,
    discover_units,
    episode_info,
    episodes_of_meta,
    is_auxiliary,
    is_full_pack_title,
    materialize,
    parse_torrent,
    proper_subset,
    screen_candidates,
    verify_local_file_in_torrent,
    verify_torrent_content,
)
from . import config_form

_PLUGIN_ID = "packcrossseed"
_KEY_PLAN = "pcs_plan"           # 当前试跑计划
_KEY_RESULT = "pcs_result"       # 上轮结果
_KEY_HISTORY = "pcs_history"     # 执行历史
_KEY_OPS = "pcs_operations"      # 写操作 journal
_KEY_UNIT_CACHE = "pcs_unit_cache"   # 单元级结论缓存：key -> {size, verdict, at}
_KEY_BLOB_CACHE = "pcs_blob_cache"   # 候选种子文件缓存：site|title|size -> {b64, at}
_KEY_HEALTH = "pcs_health"           # 最近一次做种健康巡查结果


class PackCrossSeed(_PluginBase):
    plugin_name = "拆包辅种"
    plugin_desc = "整包按电影/单集拆分，跨站匹配独立种子并做 piece 校验，硬链接生成副本后辅种。原包不动。"
    plugin_icon = "package.png"
    plugin_version = "1.2.6"
    plugin_author = "sdcsmyl"
    author_url = ""
    plugin_config_prefix = "packcrossseed_"
    plugin_order = 50
    auth_level = 2

    # ---- 配置字段（默认值全空 / 中性，个人信息不落源码）
    _enabled: bool = False
    _dry_run: bool = True
    _source_downloaders: List[str] = []
    _target_downloader: str = ""
    _output_dir: str = ""
    _torrent_dirs_raw: str = ""
    _path_mappings: str = ""
    _min_size_mb: int = 200
    _max_per_task: int = 0
    _max_units: int = 0
    _only_finished: bool = True
    _include_keywords: str = ""
    _exclude_keywords: str = ""
    _sites: List[str] = []
    _max_candidates: int = 5
    _size_tolerance: float = 8.0
    _min_similarity: float = 0.25
    _pieces_to_check: int = 3
    _accept_other_release: bool = True
    # 只做拆分作种：候选必须是源种子集号的真子集（单集/两集组合），
    # 完整整包交给 iyuu 等通用辅种插件。默认开启。
    _split_only: bool = True
    _link_mode: str = "auto"
    _label: str = ""
    _auto_start: bool = True
    # 加种可靠性：加种后校验任务真实状态、失败时重试、跟踪 recheck（借鉴 IYUU/青蛙/猪猪）
    _verify_after_add: bool = True
    # 下载种子前检查站点流控，命中则跳过该候选（借鉴 IYUU 的 sites_helper.check）
    _check_site_flow: bool = True
    # 增量缓存：记住每个单元的结论，下轮跳过已辅种/已失败的单元，避免每轮全量重跑
    _use_cache: bool = True
    # 缓存有效期（天）：失败的结论过期后会重试（站点可能新发种），成功的长期有效
    _cache_days: int = 7
    _notify: bool = True
    _cron: str = ""
    _auto_mode: bool = False
    _onlyonce: bool = False
    _debug: bool = False

    _running: bool = False
    # 单轮运行内的搜索缓存：同一关键词只打一次站点（每轮开始清空）
    _search_cache: Dict[str, List[dict]] = {}
    _ep_index: Dict[str, Dict[Any, List[Tuple[str, int]]]] = {}
    _all_instances: List[Any] = []
    # 本轮候选种子 blob 缓存（run_once 里从持久化加载，结束时写回）
    _blob_cache: Dict[str, dict] = {}
    _stop_flag: bool = False
    _lock = threading.Lock()
    _progress: Dict[str, Any] = {}

    # ================================================================ 生命周期
    def init_plugin(self, config: Optional[dict] = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled"))
            self._dry_run = bool(config.get("dry_run", True))
            self._source_downloaders = self.__as_list(config.get("source_downloaders"))
            self._target_downloader = str(config.get("target_downloader") or "").strip()
            self._output_dir = str(config.get("output_dir") or "").strip()
            self._torrent_dirs_raw = str(config.get("torrent_dirs") or "").strip()
            self._path_mappings = str(config.get("path_mappings") or "").strip()
            self._min_size_mb = self.__as_int(config.get("min_size_mb"), 200)
            self._max_per_task = self.__as_int(config.get("max_per_task"), 0)
            self._max_units = self.__as_int(config.get("max_units"), 0)
            self._only_finished = bool(config.get("only_finished", True))
            self._include_keywords = str(config.get("include_keywords") or "").strip()
            self._exclude_keywords = str(config.get("exclude_keywords") or "").strip()
            self._sites = self.__as_list(config.get("sites"))
            self._max_candidates = self.__as_int(config.get("max_candidates"), 5)
            self._size_tolerance = float(config.get("size_tolerance") or 8.0)
            self._min_similarity = float(config.get("min_similarity") or 0.25)
            self._pieces_to_check = self.__as_int(config.get("pieces_to_check"), 3)
            self._accept_other_release = bool(config.get("accept_other_release", True))
            self._split_only = bool(config.get("split_only", True))
            self._link_mode = str(config.get("link_mode") or "auto").strip()
            self._label = str(config.get("label") or "").strip()
            self._auto_start = bool(config.get("auto_start", True))
            self._verify_after_add = bool(config.get("verify_after_add", True))
            self._check_site_flow = bool(config.get("check_site_flow", True))
            self._use_cache = bool(config.get("use_cache", True))
            self._cache_days = self.__as_int(config.get("cache_days"), 7)
            self._notify = bool(config.get("notify", True))
            self._cron = str(config.get("cron") or "").strip()
            self._auto_mode = bool(config.get("auto_mode"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._debug = bool(config.get("debug"))
        if self._onlyonce:
            self._onlyonce = False
            self.update_config({"onlyonce": False})
            logger.info("拆包辅种：收到立即运行指令，15 秒后开始")
            threading.Timer(15, self.run_once).start()

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def stop_service(self) -> None:
        """插件被重载或停用时，通知当前一轮在当前单元结束处退出。"""
        if self._running:
            self._stop_flag = True
            logger.info("拆包辅种：收到卸载/重载信号，当前单元结束后退出")

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._auto_mode or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error(f"拆包辅种：cron 表达式无法解析 {self._cron}")
            return []
        return [{
            "id": f"{_PLUGIN_ID}_cron",
            "name": "拆包辅种定时任务",
            "trigger": trigger,
            "func": self.run_once,
        }]

    # ================================================================ 配置 / 页面
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return config_form.build_form(
            self.__current_config(),
            downloader_choices=self.__downloader_choices(),
            site_choices=self.__site_choices(),
        )

    def __current_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "source_downloaders": self._source_downloaders,
            "target_downloader": self._target_downloader,
            "output_dir": self._output_dir,
            "torrent_dirs": self._torrent_dirs_raw,
            "path_mappings": self._path_mappings,
            "min_size_mb": self._min_size_mb,
            "max_per_task": self._max_per_task,
            "max_units": self._max_units,
            "only_finished": self._only_finished,
            "include_keywords": self._include_keywords,
            "exclude_keywords": self._exclude_keywords,
            "sites": self._sites,
            "max_candidates": self._max_candidates,
            "size_tolerance": self._size_tolerance,
            "min_similarity": self._min_similarity,
            "pieces_to_check": self._pieces_to_check,
            "accept_other_release": self._accept_other_release,
            "split_only": self._split_only,
            "link_mode": self._link_mode,
            "label": self._label,
            "auto_start": self._auto_start,
            "verify_after_add": self._verify_after_add,
            "check_site_flow": self._check_site_flow,
            "use_cache": self._use_cache,
            "cache_days": self._cache_days,
            "notify": self._notify,
            "cron": self._cron,
            "auto_mode": self._auto_mode,
            "onlyonce": self._onlyonce,
            "debug": self._debug,
        }

    def __downloader_choices(self) -> List[dict]:
        """运行时从 MP 拉取下载器列表（只回名字，不含地址/账号）。"""
        try:
            services = DownloaderHelper().get_services() or {}
            return [{"title": name, "value": name} for name in services]
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：拉取下载器列表失败 {err}")
            return []

    def __site_choices(self) -> List[dict]:
        """运行时从 MP 拉取站点列表（只回 ID 和名称，不含 cookie）。"""
        out: List[dict] = []
        try:
            indexers = SitesHelper().get_indexers() or []
            for s in indexers:
                if not isinstance(s, dict):
                    continue
                sid = s.get("id")
                name = str(s.get("name") or s.get("domain") or "")
                if sid is not None and name:
                    out.append({"title": name, "value": str(sid)})
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：拉取站点列表失败 {err}")
        return out

    def get_page(self) -> List[dict]:
        """详情页：KPI + 做种健康 + 执行历史 + 说明。全部读本地缓存。"""
        result = self.get_data(_KEY_RESULT) or {}
        history = self.get_data(_KEY_HISTORY) or []
        if isinstance(history, dict):
            history = list(history.values())
        history = sorted(
            [h for h in history if isinstance(h, dict)],
            key=lambda x: str(x.get("at") or ""), reverse=True,
        )

        def kpi(icon: str, label: str, value: Any, color: str = "", note: str = "") -> dict:
            vcls = f"text-h6 font-weight-bold text-{color}" if color else "text-h6 font-weight-bold"
            children = [
                {"component": "VIcon", "props": {"color": color or "primary", "size": "28", "class": "mb-1"}, "text": icon},
                {"component": "span", "props": {
                    "class": vcls,
                    "style": "display:block;font-size:clamp(0.8rem,3.8vw,1.25rem);white-space:normal;"
                             "overflow-wrap:anywhere;word-break:break-all;line-height:1.2;",
                }, "text": str(value)},
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis d-block mt-1"}, "text": label},
            ]
            if note:
                children.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": note})
            return {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"},
                    "content": [{"component": "VCardText", "props": {"class": "text-center pa-3"}, "content": children}]}

        kpis = [
            ("mdi-package-variant", "发现单元", int(result.get("units") or 0), "primary",
             f"来源任务 {result.get('tasks') or 0} 个"),
            ("mdi-magnify", "校验通过", int(result.get("verified") or 0), "info",
             f"已有 {int(result.get('dup') or 0)} · 缓存跳 {int(result.get('cached') or 0)} · 候选 {result.get('candidates') or 0} 个"),
            ("mdi-file-link", "已物化副本", int(result.get("materialized") or 0), "success",
             "硬链优先，源文件只读"),
            ("mdi-upload-network", "已加种", int(result.get("seeded") or 0), "warning",
             f"试跑模式" if self._dry_run else f"目标 {self._target_downloader or '默认'}"),
        ]
        page: List[dict] = [{
            "component": "VRow", "props": {"dense": True},
            "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [kpi(*kpis[0])]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [kpi(*kpis[1])]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [kpi(*kpis[2])]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [kpi(*kpis[3])]},
            ],
        }]

        # 做种健康（最近一次巡查结果；只查本地，不访问站点）
        health = self.get_data(_KEY_HEALTH) or {}
        if isinstance(health, dict) and health.get("at"):
            bad = int(health.get("missing") or 0) + int(health.get("unmounted") or 0) \
                + int(health.get("tracker") or 0)
            hk = [
                ("mdi-heart-pulse", "做种健康", int(health.get("ok") or 0), "success",
                 f"巡查 {health.get('total') or 0} 个 · 异常 {bad}"),
                ("mdi-alert-circle-outline", "文件缺失", int(health.get("missing") or 0),
                 "error", "任务声明的文件已不在磁盘"),
                ("mdi-harddisk-remove", "存储未挂载", int(health.get("unmounted") or 0),
                 "warning", "盘未挂载，未计为缺失"),
                ("mdi-access-point-network-off", "tracker 异常",
                 int(health.get("tracker") or 0), "deep-orange", "下载器回报 tracker 报错"),
            ]
            page.append({"component": "VRow", "props": {"dense": True},
                         "content": [
                             {"component": "VCol", "props": {"cols": 6, "md": 3},
                              "content": [kpi(*hk[0])]},
                             {"component": "VCol", "props": {"cols": 6, "md": 3},
                              "content": [kpi(*hk[1])]},
                             {"component": "VCol", "props": {"cols": 6, "md": 3},
                              "content": [kpi(*hk[2])]},
                             {"component": "VCol", "props": {"cols": 6, "md": 3},
                              "content": [kpi(*hk[3])]},
                         ]})
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "density": "compact",
                "class": "mt-2",
                "text": f"做种健康巡查于 {health.get('at')} 完成，共 {health.get('total')} 个做种任务"
                        f"（{health.get('scope_detail') or '目录级检查'}）"
                        + (f"；{health.get('note')}" if health.get("note") else ""),
            }})
            hitems = health.get("items") or []
            if hitems:
                hrows: List[List[Tuple[str, str]]] = []
                for it in hitems[:20]:
                    hrows.append([
                        (str(it.get("state") or ""), "text-body-2"),
                        (str(it.get("downloader") or ""), "text-body-2"),
                        (str(it.get("task") or "")[:40], "text-body-2"),
                        (f"×{int(it.get('hit_count') or 1)}", "text-body-2 text-center"),
                        (str(it.get("detail") or "")[:60], "text-caption text-medium-emphasis"),
                    ])
                page.append({"component": "VTable", "props": {"density": "compact"},
                             "content": [
                                 {"component": "thead", "content": [{"component": "tr", "content": [
                                     {"component": "th",
                                      "props": {"class": "text-caption text-medium-emphasis"},
                                      "text": t} for t in ["状态", "下载器", "任务", "影响", "说明"]
                                 ]}]},
                                 {"component": "tbody", "content": [{"component": "tr", "content": [
                                     {"component": "td",
                                      "props": {"class": cls}, "text": txt} for txt, cls in row
                                 ]} for row in hrows]},
                             ]})

        # 执行历史
        if history:
            hrows: List[List[Tuple[str, str]]] = []
            for h in history[:20]:
                hrows.append([
                    (str(h.get("at") or ""), "text-body-2 text-no-wrap"),
                    (str(h.get("mode") or ""), "text-body-2 text-center"),
                    (str(h.get("units") or 0), "text-body-2 text-center"),
                    (str(h.get("verified") or 0), "text-body-2 text-center text-info"),
                    (str(h.get("seeded") or 0), "text-body-2 text-center text-success"),
                    (str(h.get("elapsed") or 0) + "s", "text-body-2 text-center"),
                ])
            hheads = ["时间", "模式", "单元", "校验通过", "加种", "耗时"]
            htable = [{
                "component": "VTable", "props": {"density": "comfortable"},
                "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [
                        {"component": "th", "props": {"class": "text-caption text-medium-emphasis"}, "text": t} for t in hheads
                    ]}]},
                    {"component": "tbody", "content": [{"component": "tr", "content": [
                        {"component": "td", "props": {"class": c}, "text": t} for t, c in row
                    ]} for row in hrows]},
                ],
            }]
            page.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VIcon", "props": {"color": "info", "class": "mr-2"}, "text": "mdi-history"},
                        {"component": "span", "text": "执行历史（最多 20 条）"},
                    ]},
                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": htable},
                ],
            }]}]})

        note = str(result.get("message") or "")
        page.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
            "component": "VAlert",
            "props": {
                "type": "warning" if note else "info",
                "variant": "tonal",
                "text": note or (
                    "使用顺序：先在配置里选好来源与目标下载器、填写副本输出目录；"
                    "建议先开试跑模式点「执行一轮」看校验结果（不写盘），"
                    "确认无误后关掉试跑模式再正式执行。"
                    "站点与下载器信息均从 MoviePilot 运行时读取，源码不保存任何个人信息。"
                ),
            },
        }]}]})
        return page

    # ================================================================ API
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/scan", "endpoint": self.api_scan, "methods": ["POST"], "auth": "apikey", "summary": "扫描生成拆包计划（只读）"},
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"], "auth": "apikey", "summary": "执行一轮（按试跑模式决定是否写盘）"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"], "auth": "apikey", "summary": "查询状态与进度"},
            {"path": "/stop", "endpoint": self.api_stop, "methods": ["POST"], "auth": "apikey", "summary": "停止当前一轮（处理完当前单元后退出）"},
            {"path": "/rollback", "endpoint": self.api_rollback, "methods": ["POST"], "auth": "apikey", "summary": "回滚上一轮的写操作"},
            {"path": "/clear_cache", "endpoint": self.api_clear_cache, "methods": ["POST"], "auth": "apikey", "summary": "清除增量缓存（单元结论与候选种子）"},
            {"path": "/health", "endpoint": self.api_health, "methods": ["GET"], "auth": "apikey", "summary": "做种健康巡查（只查本地文件与 tracker，不访问站点）"},
        ]

    async def api_scan(self) -> dict:
        with self._lock:
            if self._running:
                return {"success": False, "message": "任务正在运行"}
        threading.Thread(target=self._scan_task, daemon=True).start()
        return {"success": True, "message": "已启动扫描"}

    async def api_run(self) -> dict:
        with self._lock:
            if self._running:
                return {"success": False, "message": "任务正在运行"}
        threading.Thread(target=self.run_once, daemon=True).start()
        return {"success": True, "message": "已启动执行"}

    async def api_stop(self) -> dict:
        if not self._running:
            return {"success": False, "message": "当前没有运行中的任务"}
        self._stop_flag = True
        self._log("收到停止指令，当前单元处理完后退出")
        return {"success": True, "message": "已请求停止"}

    async def api_status(self) -> dict:
        return {
            "success": True,
            "running": self._running,
            "progress": dict(self._progress),
            "result": self.get_data(_KEY_RESULT) or {},
            "dry_run": self._dry_run,
        }

    async def api_rollback(self) -> dict:
        ops = self.get_data(_KEY_OPS) or []
        removed = 0
        for op in reversed(ops):
            if not isinstance(op, dict):
                continue
            target = str(op.get("target") or "")
            action = str(op.get("action") or "")
            try:
                if action in ("hardlink", "symlink", "copy") and target and os.path.lexists(target):
                    os.unlink(target)
                    removed += 1
                elif action == "mkdir" and target and os.path.isdir(target):
                    try:
                        os.rmdir(target)
                    except OSError:
                        pass
            except OSError as err:
                logger.warning(f"拆包辅种：回滚失败 {target} {err}")
        self.save_data(_KEY_OPS, [])
        return {"success": True, "message": f"已清理 {removed} 个新副本（下载器任务需手动删除）"}

    async def api_clear_cache(self) -> dict:
        """清除增量缓存，下一轮恢复全量扫描。"""
        unit_cache = self.get_data(_KEY_UNIT_CACHE) or {}
        blob_cache = self.get_data(_KEY_BLOB_CACHE) or {}
        nu = len(unit_cache) if isinstance(unit_cache, dict) else 0
        nb = len(blob_cache) if isinstance(blob_cache, dict) else 0
        self.save_data(_KEY_UNIT_CACHE, {})
        self.save_data(_KEY_BLOB_CACHE, {})
        return {"success": True,
                "message": f"已清除增量缓存：单元结论 {nu} 条、候选种子 {nb} 条"}

    async def api_health(self) -> dict:
        """做种健康巡查：只读本地下载器与文件系统，不访问任何站点。"""
        from . import seedhealth
        downloaded: List[Tuple[Any, str]] = []
        try:
            services = DownloaderHelper().get_services() or {}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"读取下载器失败：{err}"}
        for name, info in services.items():
            inst = getattr(info, "instance", None)
            if not inst:
                continue
            try:
                if getattr(inst, "is_inactive", lambda: False)():
                    continue
                torrents = inst.get_completed_torrents() or []
            except Exception as err:  # noqa: BLE001
                logger.warning(f"拆包辅种：巡查读取 {name} 失败 {err}")
                continue
            for t in torrents:
                downloaded.append((t, str(name)))
        report = seedhealth.build_report(
            downloaded, self.__attr,
            at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        # 最近一次巡查结果存起来，页面直接读缓存值
        self.save_data(_KEY_HEALTH, {
            "at": report.at, "total": report.total, "ok": report.ok,
            "missing": report.missing, "unmounted": report.unmounted,
            "tracker": report.tracker, "unknown": report.unknown,
            "items": report.items, "note": report.note,
            "scope": report.scope, "scope_detail": report.scope_detail,
        })
        return {
            "success": True, "at": report.at, "total": report.total,
            "ok": report.ok, "missing": report.missing,
            "unmounted": report.unmounted, "tracker": report.tracker,
            "unknown": report.unknown, "items": report.items,
            "note": report.note, "scope": report.scope,
            "scope_detail": report.scope_detail,
        }

    # ================================================================ 任务编排
    def _scan_task(self) -> None:
        """只读扫描，生成计划存到 _KEY_PLAN。"""
        self._running = True
        self._progress = {"phase": "发现", "done": 0, "total": 0}
        try:
            tasks = self.__apply_include(self.__collect_tasks())
            self._progress["total"] = len(tasks)
            self._progress["done"] = len(tasks)
            units = discover_units(
                tasks,
                min_size=self._min_size_mb * 1024 * 1024,
                max_per_task=self._max_per_task,
                log=self._log,
            )
            units = self.__limit_units(self.__apply_exclude(units))
            self.save_data(_KEY_PLAN, {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tasks": len(tasks),
                "units": [self.__unit_brief(u) for u in units],
            })
            self._log(f"扫描完成：{len(tasks)} 个任务，{len(units)} 个可拆单元")
            self.post_message(
                title="拆包辅种 · 扫描完成",
                text=f"发现 {len(tasks)} 个已完成任务，拆出 {len(units)} 个单元。请到插件详情页查看计划。",
            )
        except Exception as err:  # noqa: BLE001
            logger.error(f"拆包辅种：扫描失败 {err}")
            self.post_message(title="拆包辅种 · 扫描失败", text=str(err)[:300])
        finally:
            self._running = False
            self._progress = {}

    def run_once(self) -> None:
        """完整一轮：发现 → 搜索 → 下载种子 → 校验 → 物化 → 加种。"""
        with self._lock:
            if self._running:
                self._log("上一轮尚未结束，跳过")
                return
            self._running = True
            self._stop_flag = False
            self._search_cache = {}
            self._ep_index = {}
            self._all_instances = []
            try:
                for _info in (DownloaderHelper().get_services() or {}).values():
                    _inst = getattr(_info, "instance", None)
                    if _inst and not getattr(_inst, "is_inactive", lambda: False)():
                        self._all_instances.append(_inst)
            except Exception:  # noqa: BLE001
                pass
        started = time.time()
        stats: Dict[str, Any] = {
            "units": 0, "tasks": 0, "candidates": 0, "verified": 0,
            "materialized": 0, "seeded": 0, "failed": 0, "dup": 0,
            "cached": 0,
            "mode": "试跑" if self._dry_run else "执行",
            "message": "",
        }
        ops_new: List[dict] = []
        unit_cache: Dict[str, dict] = {}
        blob_cache: Dict[str, dict] = {}
        try:
            # --- 1. 发现
            self._progress = {"phase": "发现", "done": 0, "total": 0}
            tasks = self.__apply_include(self.__collect_tasks())
            units = discover_units(
                tasks, self._min_size_mb * 1024 * 1024, self._max_per_task, self._log,
            )
            units = self.__limit_units(self.__apply_exclude(units))
            stats["tasks"] = len(tasks)
            stats["units"] = len(units)
            self._log(f"发现 {len(units)} 个可拆单元")

            # --- 2~3. 搜索 + 校验
            self._progress = {"phase": "匹配校验", "done": 0, "total": len(units)}
            target = self.__pick_target_downloader()
            if not self._dry_run and not self._output_dir:
                raise RuntimeError("执行模式必须填写辅种副本输出目录")
            if not self._dry_run and target is None:
                raise RuntimeError("找不到加种下载器，请检查配置")

            # 加载增量缓存（试跑模式下不读也不写，保证试跑始终全量看真实情况）
            if self._use_cache and not self._dry_run:
                unit_cache = self.__load_unit_cache()
                blob_cache = self.get_data(_KEY_BLOB_CACHE) or {}
                if not isinstance(blob_cache, dict):
                    blob_cache = {}
                if unit_cache:
                    self._log(f"增量缓存: 已载入 {len(unit_cache)} 条单元结论")
            self._blob_cache = blob_cache

            for i, unit in enumerate(units, 1):
                if self._stop_flag:
                    stats["message"] = f"已手动停止，剩余 {len(units) - i + 1} 个单元未处理"
                    self._log(stats["message"])
                    break
                self._progress["done"] = i
                # 命中缓存直接跳过：已辅种 / 目标已存在 / 无候选 / 校验不过，下轮不再重跑
                cached_verdict = self.__unit_cache_get(unit_cache, unit)
                if cached_verdict:
                    stats["cached"] += 1
                    if cached_verdict in ("seeded", "dup"):
                        stats["dup"] += 1
                    if self._debug:
                        self._log(f"  缓存命中跳过 {unit.name[:36]}: {cached_verdict}")
                    continue
                try:
                    raw = self.__search(unit)
                    cands = screen_candidates(
                        unit, raw,
                        size_tolerance=self._size_tolerance / 100.0,
                        min_similarity=self._min_similarity,
                        max_candidates=self._max_candidates,
                        split_only=self._split_only,
                    )
                    stats["candidates"] += len(cands)
                    if self._debug:
                        self._log(
                            f"  {unit.name[:30]}: 本地 {unit.size / 1024 / 1024:.0f}MB，"
                            f"搜索 {len(raw)} 条，候选 {len(cands)} 条"
                        )
                        for item in cands[:5]:
                            self._log(
                                f"    · {item.size / 1024 / 1024:.0f}MB "
                                f"{item.site[:10]} {item.title[:48]}"
                            )
                    if not cands:
                        # 无候选：站点当下确实没有，缓存一个短结论，过期后自动重试
                        self.__unit_cache_put(unit_cache, unit, "nomatch")
                        continue
                    best: Optional[Candidate] = None
                    for cand in cands:
                        ok, reason = self.__verify_candidate(unit, cand)
                        if self._debug:
                            self._log(f"  {unit.name[:30]} <- {cand.site} {cand.title[:30]}: {reason}")
                        if ok:
                            best = cand
                            break
                    if not best:
                        # 有候选但校验不过（版本不同/合集缺文件）：同样缓存，下轮不再白费请求
                        self.__unit_cache_put(unit_cache, unit, "noveri")
                        continue
                    stats["verified"] += 1

                    # 判重必须早于物化：op-TR 同时是源和目标，源任务自身的
                    # infohash 天然会命中这里。若先物化再判重，会白建一整套
                    # 硬链接、白读几十 GB 算 piece，最后才说「已有该种子」，
                    # 留下没人做种的孤儿副本。
                    seed_blob = best.torrent_blob
                    meta = parse_torrent(seed_blob)
                    if not meta:
                        stats["failed"] += 1
                        self.__unit_cache_put(unit_cache, unit, "noveri")
                        continue
                    if self.__is_duplicate(target, meta):
                        stats["dup"] += 1
                        self.__unit_cache_put(unit_cache, unit, "dup")
                        self._log(f"  目标下载器已有该种子，跳过: {unit.name[:40]}")
                        continue

                    if self._dry_run:
                        unit.candidates = [best]
                        continue

                    # --- 4. 物化
                    ops, missing = materialize(
                        meta,
                        src_base=os.path.dirname(unit.source_path),
                        dst_base=self._output_dir,
                        mode=self._link_mode,
                        log=self._log,
                        target_source=unit.source_path,
                        season=unit.season,
                        episode=unit.episode,
                        episode_resolver=self.__make_episode_resolver(
                            os.path.dirname(unit.source_path)
                        ),
                    )
                    if missing:
                        self._log(
                            f"  {unit.name[:30]}: 这个种子需要 {len(missing)} 个本地没有的文件，"
                            f"加进去会一直校验失败，跳过"
                        )
                        stats["failed"] += 1
                        self.__unit_cache_put(unit_cache, unit, "noveri")
                        continue
                    # unit 源文件名可能与种子内文件名不同，修正硬链接
                    self.__align_single_file(meta, unit, ops)
                    ops_new.extend([o.__dict__ for o in ops])
                    stats["materialized"] += sum(
                        1 for o in ops if o.action in ("hardlink", "symlink", "copy")
                    )
                    # --- 5. 加种（带上候选来源站点名，作为第二个标签）
                    if self.__add_torrent(target, seed_blob, meta, ops, site=best.site):
                        stats["seeded"] += 1
                        self.__unit_cache_put(unit_cache, unit, "seeded")
                except Exception as err:  # noqa: BLE001
                    stats["failed"] += 1
                    # 异常不写缓存，下一轮允许重试
                    logger.warning(f"拆包辅种：处理 {unit.name[:40]} 失败 {err}")

            # 持久化增量缓存：单元结论 + 候选 blob（都做过期/容量清理）
            if self._use_cache and not self._dry_run:
                self.save_data(_KEY_UNIT_CACHE, self.__prune_unit_cache(unit_cache))
                self.save_data(_KEY_BLOB_CACHE, self.__prune_blob_cache(blob_cache))

            if ops_new:
                self.save_data(_KEY_OPS, ops_new)
            # 保存计划（带校验标记）
            self.save_data(_KEY_PLAN, {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tasks": stats["tasks"],
                "units": [self.__unit_brief(u) for u in units],
            })
        except Exception as err:  # noqa: BLE001
            stats["message"] = str(err)[:300]
            logger.error(f"拆包辅种：运行失败 {err}")
        finally:
            stats["elapsed"] = int(time.time() - started)
            self.save_data(_KEY_RESULT, stats)
            hist = self.get_data(_KEY_HISTORY) or []
            if isinstance(hist, dict):
                hist = list(hist.values())
            hist.append({
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **stats,
            })
            self.save_data(_KEY_HISTORY, hist[-50:])
            self._running = False
            self._progress = {}
            if self._notify:
                self.post_message(
                    title="拆包辅种 · 运行完成",
                    text=(f"单元 {stats['units']} · 校验通过 {stats['verified']} · "
                          f"已缓存跳过 {stats['cached']} · 已有 {stats['dup']} · "
                          f"物化 {stats['materialized']} · "
                          f"加种 {stats['seeded']} · 耗时 {stats['elapsed']}s"
                          + (f"\n{stats['message']}" if stats["message"] else "")),
                )

    # ================================================================ 内部方法
    def __collect_tasks(self) -> List[dict]:
        """从已配置下载器收集已完成任务的种子路径与保存目录。"""
        services = DownloaderHelper().get_services() or {}
        wants = {x.lower() for x in self._source_downloaders}
        tasks: List[dict] = []
        for name, info in services.items():
            if wants and str(name).lower() not in wants:
                continue
            instance = getattr(info, "instance", None)
            if not instance or getattr(instance, "is_inactive", lambda: True)():
                logger.warning(f"拆包辅种：下载器 {name} 未连接，跳过")
                continue
            for t in self.__load_torrents(instance, str(name)):
                try:
                    pct = float(self.__attr(t, "percent_done", "percentDone", "progress", default=0))
                except (TypeError, ValueError):
                    continue
                if self._only_finished and pct < 0.999:
                    continue
                save_path = str(self.__attr(t, "download_dir", "downloadDir", "save_path", default="") or "")
                hash_str = str(self.__attr(t, "hash_string", "hashString", "hash", default="") or "").lower()
                torrent_path = self.__find_torrent_file(hash_str)
                if not torrent_path or not save_path:
                    continue
                tasks.append({
                    "downloader": str(name),
                    "task_name": str(self.__attr(t, "name", default="") or ""),
                    "torrent_path": self.__map_torrent_path(torrent_path),
                    "save_path": self.__map_media_path(save_path),
                })
        return tasks

    def __load_torrents(self, instance: Any, name: str) -> List[Any]:
        """读取下载器任务列表。V3 的 get_torrents 返回 (列表, 错误标志)。"""
        try:
            if self._only_finished and hasattr(instance, "get_completed_torrents"):
                return list(instance.get_completed_torrents() or [])
            result = instance.get_torrents()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：读取下载器 {name} 任务失败 {err}")
            return []
        if isinstance(result, tuple):
            return list(result[0] or [])
        return list(result or [])

    def __find_torrent_file(self, hash_str: str) -> str:
        """在下载器种子缓存里按 infohash 找 .torrent 文件。"""
        if not hash_str:
            return ""
        for d in self.__torrent_dirs():
            candidate = os.path.join(d, hash_str + ".torrent")
            if os.path.exists(candidate):
                return candidate
        return ""

    def __torrent_dirs(self) -> List[str]:
        """下载器种子缓存目录（MP 容器内可见路径）。

        优先用配置里填写的目录（逗号分隔），缺省时递归探测常见挂载点。
        不写死任何真实目录。
        """
        dirs: List[str] = [x.strip() for x in self._torrent_dirs_raw.split(",") if x.strip()]
        if not dirs:
            # 通用挂载名探测（都是通用容器路径，非个人信息）
            for base in ("/", "/config", "/mnt"):
                if not os.path.isdir(base):
                    continue
                try:
                    for entry in os.listdir(base):
                        low = entry.lower()
                        if "torrent" in low:
                            candidate = os.path.join(base, entry)
                            if os.path.isdir(candidate):
                                dirs.append(candidate)
                except OSError:
                    continue
        return list(dict.fromkeys(dirs))

    def __map_torrent_path(self, p: str) -> str:
        return p

    def __map_media_path(self, p: str) -> str:
        """下载器看到的路径 → 容器内路径。

        按配置里的 path_mapping（每行 `下载器路径=>容器内路径`）逐条替换；
        未配置则原样返回（多数情况下 downloadDir 就是容器可见路径）。
        """
        result = p
        for line in self._path_mappings.splitlines():
            if "=>" not in line:
                continue
            src, dst = line.split("=>", 1)
            src, dst = src.strip(), dst.strip()
            if src and dst and result.startswith(src):
                result = dst + result[len(src):]
        return result

    @staticmethod
    def __attr(obj: Any, *names: str, default: Any = None) -> Any:
        """兼容 transmission_rpc 的 Torrent（dict 子类，属性即键）。"""
        for name in names:
            try:
                value = getattr(obj, name)
            except (AttributeError, KeyError, TypeError):
                value = None
            if value is None and isinstance(obj, dict):
                value = obj.get(name)
            if value is not None:
                return value
        return default

    def __apply_include(self, tasks: List[dict]) -> List[dict]:
        """只保留任务名命中关键词的任务；留空则全部保留（试点时用来锁定范围）。"""
        words = [w.strip().lower() for w in self._include_keywords.split(",") if w.strip()]
        if not words:
            return tasks
        return [t for t in tasks if any(w in str(t.get("task_name") or "").lower() for w in words)]

    def __limit_units(self, units: List[Unit]) -> List[Unit]:
        """限制单轮处理的单元数。0 或未配置时兜底 20，避免空配置一次跑上千个单元。"""
        limit = self._max_units if self._max_units > 0 else 20
        return units[:limit]

    def __apply_exclude(self, units: List[Unit]) -> List[Unit]:
        words = [w.strip().lower() for w in self._exclude_keywords.split(",") if w.strip()]
        if not words:
            return units
        return [u for u in units if not any(w in u.name.lower() for w in words)]

    def __search(self, unit: Unit) -> List[dict]:
        """用 MP 站内搜索，返回归一化候选（不含持久化的 cookie）。"""
        from app.chain.search import SearchChain
        sites: Optional[List[int]] = None
        if self._sites:
            picked: List[int] = []
            for s in self._sites:
                try:
                    picked.append(int(s))
                except (TypeError, ValueError):
                    continue
            sites = picked or None
        # 同一关键词在一轮里会重复出现：同一个包挂了多个任务副本时，
        # 每集的关键词完全一样，搜一次 398 条、过滤一遍，重复做纯属浪费。
        cache_key = (unit.search_keyword or "").strip().lower()
        if cache_key and cache_key in self._search_cache:
            return self._search_cache[cache_key]
        try:
            # rule_groups 显式传空列表：MP 只在它为 None 时才套用系统配置的
            # SearchFilterRuleGroups（本站当前是「免费优先-画质通用」），传 [] 会
            # 在 filter 里走 `if not rule_groups: return torrents` 直接放行。
            # 拆包辅种只关心文件内容是否一致，不需要画质/免费倾向，
            # 套默认规则会把本可对上的单集种直接过滤掉，表现为「搜索有结果、候选 0」。
            results = SearchChain().search_by_title(
                title=unit.search_keyword,
                page=0,
                sites=sites,
                cache_local=False,
                rule_groups=[],
            ) or []
        except Exception as err:  # noqa: BLE001
            self._log(f"搜索失败 {unit.search_keyword[:30]}: {str(err)[:80]}")
            return []
        out: List[dict] = []
        for ctx in results:
            ti = getattr(ctx, "torrent_info", None)
            if not ti:
                continue
            def gv(*names: str, default: Any = None) -> Any:
                for n in names:
                    try:
                        v = getattr(ti, n)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v is not None:
                        return v
                return default
            site = str(gv("site_name", default="") or "")
            out.append({
                "title": str(gv("title", default="") or ""),
                "size": int(gv("size", default=0) or 0),
                "site": site,
                "site_id": gv("site", default=None),
                "enclosure": str(gv("enclosure", default="") or ""),
                "page_url": str(gv("page_url", default="") or ""),
                "cookie": str(gv("site_cookie", default="") or ""),
                "ua": str(gv("site_ua", default="") or ""),
                "referer": str(gv("page_url", default="") or ""),
            })
        if cache_key:
            self._search_cache[cache_key] = out
        return out

    def __verify_candidate(self, unit: Unit, cand: Candidate) -> Tuple[bool, str]:
        """下载候选种子 → 解析 → piece 校验本地单集文件。"""
        # 候选种子文件缓存：同一个候选不重复下载（省站点请求，尤其馒头换票）
        blob = self.__blob_cache_get(cand)
        if blob:
            cand.verify_reason = "命中种子缓存"
        else:
            blob = self.__download_torrent(cand)
            if blob:
                self.__blob_cache_put(cand, blob, self._blob_cache)
        if not blob:
            cand.verify_reason = "种子下载失败"
            return False, cand.verify_reason
        meta = parse_torrent(blob)
        if not meta:
            cand.verify_reason = "种子解析失败"
            return False, cand.verify_reason
        cand.torrent_blob = blob
        cand.infohash = meta.get("infohash") or ""
        # 拆分作种：候选种子的真实文件集号必须是源种子集号的「非空真子集」。
        # 这是比标题更精确的判定——标题可能不写全集号，但种子文件列表骗不了人。
        # 完整整包（== 源）交给 iyuu，单集 {1} 和两集 {1,2} 才放行。
        if self._split_only:
            src_eps = set(unit.source_episodes)
            cand_eps = episodes_of_meta(meta)
            if len(src_eps) < 2:
                cand.verify_reason = "源不是多集整包，完整资源交 iyuu，跳过"
                return False, cand.verify_reason
            if not proper_subset(cand_eps, src_eps):
                cand.verify_reason = (
                    f"候选集号 {sorted(cand_eps)} 非源 {sorted(src_eps)} 的真子集（整包/超集），跳过"
                )
                return False, cand.verify_reason
        # 整包/合集候选：本地缺正片文件就没必要再做 piece 校验和物化。
        # 注意只统计「必需文件」——站点种子常附带 nfo/字幕/样图，本地拆出来
        # 的通常只有视频，若把这些也算进来，带辅助文件的种子会被整批拒掉。
        base = os.path.dirname(unit.source_path)
        required = [f for f in (meta.get("files") or []) if not is_auxiliary(
            os.path.join(*f["path"]) if f.get("path") else str(meta.get("name") or "")
        ) and not f.get("padding")]
        need = len(required)
        if need > 1:
            have = 0
            for f in required:
                rel = os.path.join(*f["path"]) if f["path"] else ""
                paths = [os.path.join(base, rel), os.path.join(base, os.path.basename(rel))]
                if any(p and os.path.exists(p) for p in paths):
                    have += 1
                    continue
                # 跨站辅种时两边是不同发布组，文件名必然不同，只按名字精确匹配
                # 会把整包全部判成「本地没有」。这里退一步按季集号 + 体积在本地
                # 目录里找同一集，体积差得远的仍视为不同版本、不算命中。
                if self.__match_local_episode(base, f):
                    have += 1
            if have < need:
                cand.verify_reason = (
                    f"站点是 {need} 个正片文件的{'整包' if need > 4 else '合集'}，"
                    f"本地只有 {have} 个，跳过"
                )
                return False, cand.verify_reason
        # 不再用「种子里必须有等大文件」当门槛：压制版本不同时体积必然不同，
        # 但仍是同一个片子，能否复用由 piece 校验按集号定位后判定。
        # 构建完整 local_map：目标集 + 同目录里能按季集号+体积对上的其它集，
        # 这样两集合集候选的「跨集边界 piece」也能被正确校验，而不是一律判未知。
        local_map = self.__build_local_map(meta, unit)
        verdict = verify_torrent_content(
            meta, local_map, pieces_to_check=self._pieces_to_check or None,
        )
        cand.piece_total = verdict.checked
        cand.piece_matched = verdict.verified
        # 通过口径：没有任何内容不符（mismatch=0）且至少有 1 片验过。
        # 若存在跨到缺失文件的片（client_check），仍允许加种——拆包场景本就
        # 只保证本地这一集可用，其余集由下载器按种子补齐或跳过。
        cand.verify_ok = (verdict.mismatch == 0 and verdict.verified > 0)
        if cand.verify_ok:
            cand.verify_reason = f"通过 {verdict.verified} 片（{verdict.level}）"
        else:
            cand.verify_reason = verdict.reason or "校验未通过"
        return cand.verify_ok, cand.verify_reason

    def __build_local_map(self, meta: dict, unit: Unit) -> Dict[str, str]:
        """把候选种子里的文件映射到本地实际文件，供 span 校验使用。

        映射必须确定：**体积先精确相等**，再按 精确文件名 → 季集号 → 唯一同体积
        的顺序判定；任何一步出现多个并列候选即视为「歧义」，宁可不映射也不猜
        （猜错会拿别的集去校验，既浪费 IO 又可能得出错误结论）。
        一个本地文件最多映射给一个种子文件。
        """
        out: Dict[str, str] = {}
        files = meta.get("files") or []
        base = os.path.dirname(unit.source_path)

        # 本地候选池：源目录里能识别出文件的 + 目标集本身
        pool: List[Tuple[str, int]] = []
        try:
            index = self.__local_episode_index(base)
            for items in index.values():
                pool.extend(items)
        except Exception:  # noqa: BLE001
            index = {}
        if os.path.exists(unit.source_path):
            try:
                pool.append((unit.source_path, os.path.getsize(unit.source_path)))
            except OSError:
                pass
        seen: set = set()
        uniq: List[Tuple[str, int]] = []
        for p, s in pool:
            if p in seen:
                continue
            seen.add(p)
            uniq.append((p, int(s)))

        used: set = set()

        # 目标集优先：先把 unit.source_path 绑到「集号相同且体积相等」的种子文件上。
        # 这是拆包辅种的主路径，必须优先确定，不能因为目录里还有别的同体积文件
        # 就被判成歧义（那会把本来能校验的候选全部丢掉）。
        try:
            target_size = os.path.getsize(unit.source_path) if os.path.exists(unit.source_path) else 0
        except OSError:
            target_size = 0
        if target_size:
            target_key = ""
            # ① 集号相同的种子文件
            if unit.episode:
                for f in files:
                    rel_key = "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
                    if int(f.get("length") or 0) != target_size or f.get("padding"):
                        continue
                    _s, _e, is_ep = episode_info(os.path.basename(rel_key))
                    if is_ep and _e == unit.episode:
                        target_key = rel_key
                        break
            # ② 只有一个同体积文件时用它
            if not target_key:
                same_len = [
                    "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
                    for f in files
                    if int(f.get("length") or 0) == target_size and not f.get("padding")
                ]
                if len(same_len) == 1:
                    target_key = same_len[0]
            if target_key:
                out[target_key] = unit.source_path
                used.add(unit.source_path)

        def pick(items: List[Tuple[str, int]]) -> str:
            """唯一命中才返回路径；零个或多个都返回空（多个=歧义，不猜）。"""
            if len(items) == 1:
                return items[0][0]
            return ""

        for f in files:
            rel_key = "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
            length = int(f.get("length") or 0)
            if f.get("padding") or length == 0:
                continue
            if rel_key in out:
                continue
            # 体积必须精确相等：体积不同绝不可能是同一文件
            same = [(p, s) for p, s in uniq if s == length and p not in used]
            if not same:
                continue
            # ① 文件名精确相同
            want_name = os.path.basename(rel_key)
            exact = [(p, s) for p, s in same if os.path.basename(p) == want_name]
            hit = pick(exact)
            if hit:
                out[rel_key] = hit
                used.add(hit)
                continue
            # ② 季集号相同
            _s, _e, is_ep = episode_info(want_name)
            if is_ep and _e:
                ep_hit = [(p, s) for p, s in same
                          if episode_info(os.path.basename(p))[1] == _e]
                hit = pick(ep_hit)
                if hit:
                    out[rel_key] = hit
                    used.add(hit)
                    continue
            # ③ 同体积只有一个，才敢用
            hit = pick(same)
            if hit:
                out[rel_key] = hit
                used.add(hit)
                continue
            # 多个并列 → 歧义，跳过（该文件按缺失处理，校验时判不可用）
        return out

    @staticmethod
    def __split_recipe(raw: str) -> Tuple[str, Optional[dict]]:
        """拆站点编码的「换票配方」。

        部分站点（馒头就是）把真地址写成 `[<base64 配方>]<真实地址>`：
        配方里带请求方法、参数、请求头，必须先按配方换一次票才能拿到种子。
        直接把这种串当 URL 解析，任何地方都会报 Invalid IPv6 URL。
        """
        text = (raw or "").strip().strip("\"'")
        match = re.search(r"\[(.*)](.*)", text)
        if not match:
            return text, None
        encoded, real = match.group(1), match.group(2)
        if not encoded:
            return real or text, None
        try:
            import base64 as _b64
            import json as _json
            recipe = _json.loads(_b64.b64decode(encoded.encode()).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return real or text, None
        return (real or text), (recipe if isinstance(recipe, dict) else None)

    def __fetch_with_recipe(self, url: str, recipe: dict, cand: Candidate) -> bytes:
        """按站点配方换票，拿到真正的种子内容。"""
        method = str(recipe.get("method") or "get").lower()
        headers = recipe.get("header") if isinstance(recipe.get("header"), dict) else None
        params = recipe.get("params") if isinstance(recipe.get("params"), dict) else None
        cookies = (cand.cookie or None) if recipe.get("cookie") else None
        allow = bool(recipe.get("allow_redirects", True))
        if self._debug:
            self._log(
                f"  配方 {cand.site}: {method} 键={sorted(recipe)} 参数={sorted(params or {})}"
            )
        client = RequestUtils(ua=cand.ua or None, cookies=cookies, timeout=30)
        try:
            if method == "get":
                res = client.get_res(url, params=params, headers=headers, allow_redirects=allow)
            elif "json" in recipe:
                res = client.post_res(
                    url, json=recipe.get("json"), params=params,
                    headers=headers, allow_redirects=allow)
            else:
                res = client.post_res(
                    url, params=params, headers=headers, allow_redirects=allow)
        except Exception as err:  # noqa: BLE001
            self._log(f"换票请求异常 {cand.site}: {str(err)[:70]}")
            return b""
        if not res:
            self._log(f"换票无响应 {cand.site}")
            return b""
        code = int(getattr(res, "status_code", 0) or 0)
        body = getattr(res, "content", b"") or b""
        if code == 200 and body[:1] == b"d" and b"announce" in body[:4000]:
            return bytes(body)
        # 有些站点先回一段 JSON / 页面，里面才是临时下载地址。
        # 关键：站点把链接写成 JSON 转义形式 https:\/\/...，不先还原，
        # 下面的 https?:// 正则一条都匹配不到，会误判成「没给链接」。
        text = (body.decode("utf-8", "ignore") if body else "").replace("\\/", "/")
        link = re.search(r"https?://[^\s\"'<>]+\.torrent[^\s\"'<>]*", text) \
            or re.search(r"https?://[^\s\"'<>]{20,}", text)
        if code == 200 and not link:
            # data 里也可能是站内相对路径（/api/torrent/xxx），拼上站点域名再试
            rel = re.search(r"\"(?:data|url|link)\"\s*:\s*\"(/[^\"]{4,})\"", text)
            base = re.match(r"https?://[^/]+", url)
            if rel and base:
                link = re.match(r"(.*)", base.group(0) + rel.group(1))
        if code == 200 and link:
            real = link.group(0).replace("\\/", "/")
            try:
                res2 = RequestUtils(
                    ua=cand.ua or None, cookies=cand.cookie or None, timeout=30
                ).get_res(real, allow_redirects=True)
                body2 = getattr(res2, "content", b"") or b"" if res2 else b""
                code2 = int(getattr(res2, "status_code", 0) or 0)
                if code2 == 200 and not self.__is_error_page(body2):
                    return bytes(body2)
                # 二跳也失败时说清楚是哪一步断的，别让上层只看到「解析失败」
                self._log(
                    f"换票二跳未拿到种子 {cand.site}: HTTP {code2} 长度 {len(body2)}"
                    f"{self.__explain_body(body2)}"
                )
            except Exception as err:  # noqa: BLE001
                self._log(f"换票二跳异常 {cand.site}: {str(err)[:70]}")
        # 失败原因只打状态码、长度与站点自述的原因，避免把票据、令牌带进日志
        detail = self.__explain_body(body)
        self._log(f"换票未拿到种子 {cand.site}: HTTP {code} 长度 {len(body)}{detail}")
        if self._debug:
            self._log(f"  换票结构 {cand.site}: {self.__body_shape(body)}")
        return b""

    @staticmethod
    def __body_shape(body: bytes) -> str:
        """只描述 JSON 结构（键名/类型/长度），不打印任何值。

        站点把「换票失败」写成 200 + {"message": "SUCCESS"} 时，光看长度
        判断不了是没给链接还是给了别的东西，得先把结构摊开。
        """
        if not body:
            return "空响应"
        try:
            import json as _json
            data = _json.loads(body.decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            return "非 JSON"

        def brief(val: Any) -> str:
            if isinstance(val, str):
                low = val[:8].lower()
                tag = " 链接" if low.startswith(("http", "magnet")) else ""
                return f"str({len(val)}){tag}"
            if isinstance(val, dict):
                return "dict" + str(sorted(val))[:60]
            if isinstance(val, list):
                return f"list({len(val)})"
            return type(val).__name__

        if not isinstance(data, dict):
            return f"根类型 {type(data).__name__}"
        parts = [f"{k}:{brief(v)}" for k, v in data.items()]
        return " ".join(parts)[:160]

    @staticmethod
    def __explain_body(body: bytes) -> str:
        """从失败响应里提炼站点给的原因。

        站点常把错误写成 `{"message": "..."}` 或网页，直接丢给种子解析器只会得到
        「Unable to interpret `{` char」。这里把原因摘出来，同时清掉链接与长串。
        """
        if not body:
            return ""
        text = body.decode("utf-8", "ignore").strip()
        if not text:
            return ""
        head = text[:1]
        if head in "{[":
            try:
                import json as _json
                data = _json.loads(text)
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict):
                for key in ("message", "msg", "info", "error", "errors", "detail"):
                    val = data.get(key)
                    if isinstance(val, (dict, list)):
                        try:
                            import json as _json2
                            val = _json2.dumps(val, ensure_ascii=False)
                        except Exception:  # noqa: BLE001
                            val = str(val)
                    if not val:
                        continue
                    clean = re.sub(r"https?://\S+", "<链接>", str(val))
                    clean = re.sub(r"[A-Za-z0-9_\-]{24,}", "<串>", clean)
                    return f" 原因 {clean[:80]}"
            return ""
        if head == "<":
            return " 返回的是网页"
        return ""

    # 站点下载失败时常见的错误页字节串：命中即视为没拿到种子，别喂给解析器
    # （借鉴青蛙辅种助手对「你没有该权限」的识别，此处补充常见几种）。
    _DL_ERROR_MARKERS = (
        "你没有该权限".encode("utf-8"),
        "无权限下载".encode("utf-8"),
        "请登录".encode("utf-8"),
        "种子不存在".encode("utf-8"),
        "triggered the rate limit".encode("utf-8"),
    )

    @classmethod
    def __is_error_page(cls, body: bytes) -> bool:
        """200 但内容是错误页/无权限提示时，识别出来当作下载失败。"""
        if not body:
            return True
        # 真种子必是 bencode 字典（以 d 开头）
        if body[:1] == b"d":
            return False
        head = body[:4096]
        return any(marker in head for marker in cls._DL_ERROR_MARKERS)

    def __download_torrent(self, cand: Candidate) -> bytes:
        """下载候选种子文件。站点凭据只在内存中传递，不落盘、不进日志。

        顺序：站点流控检查 → 站点换票配方 → MP 自带下载服务 → 直连。
        """
        # 站点流控检查（借鉴 IYUU）：命中流控的站点跳过该候选，避免频繁访问
        # 触发封禁。check() 返回 (是否命中, 原因)，取不到站点时放行、不误伤。
        if self._check_site_flow and cand.page_url:
            try:
                from urllib.parse import urlparse
                domain = urlparse(cand.page_url).netloc
                if domain:
                    blocked, reason = SitesHelper().check(domain)
                    if blocked:
                        self._log(f"站点流控中，跳过候选 {cand.site}: {str(reason)[:60]}")
                        return b""
            except Exception as err:  # noqa: BLE001
                logger.debug(f"拆包辅种：站点流控检查不可用 {err}")
        url, recipe = self.__split_recipe(cand.enclosure or "")
        if not url:
            return b""
        if self._debug:
            head = re.sub(r"/(?:[A-Za-z0-9_\-]{16,})", "/<r>", url.split("?")[0])
            self._log(f"  链接 {cand.site}: {head[:80]}{' [配方]' if recipe else ''}")
        if url.startswith("magnet:"):
            self._log(f"{cand.site} 返回磁力链，无法做 piece 校验，跳过")
            return b""
        if recipe:
            blob = self.__fetch_with_recipe(url, recipe, cand)
            if blob:
                return blob
        site_id: Optional[int] = None
        try:
            site_id = int(cand.site_id) if cand.site_id is not None else None
        except (TypeError, ValueError):
            site_id = None
        try:
            from app.application.torrent.download import TorrentHelper
            _, content, _, _, err = TorrentHelper().download_torrent(
                url=url,
                cookie=cand.cookie or None,
                ua=cand.ua or None,
                referer=cand.referer or None,
                proxy=False,
                cache_invalid=False,
                site_id=site_id,
            )
            if content and not isinstance(content, str) and not self.__is_error_page(bytes(content)):
                return bytes(content)
            if content and isinstance(content, str) and content.startswith("magnet:"):
                self._log(f"{cand.site} 返回磁力链，无法做 piece 校验，跳过")
                return b""
            if err:
                self._log(f"站点下载服务未取到种子 {cand.site}: {str(err)[:80]}")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：站点下载服务不可用 {cand.site} {str(err)[:80]}")
        try:
            client = RequestUtils(
                ua=cand.ua or None,
                cookies=cand.cookie or None,
                referer=cand.referer or None,
                timeout=30,
            )
            res = client.get_res(url)
        except Exception as err:  # noqa: BLE001
            self._log(f"种子下载异常 {cand.site}: {str(err)[:80]}")
            return b""
        if not res or int(getattr(res, "status_code", 0) or 0) != 200 or not res.content:
            self._log(f"种子下载失败 {cand.site}: HTTP {getattr(res, 'status_code', '无响应')}")
            return b""
        if self.__is_error_page(bytes(res.content)):
            self._log(f"种子下载返回错误页 {cand.site}{self.__explain_body(bytes(res.content))}")
            return b""
        return bytes(res.content)

    def __pick_target_downloader(self) -> Any:
        services = DownloaderHelper().get_services() or {}
        want = self._target_downloader.strip().lower()
        for name, info in services.items():
            if want and str(name).lower() != want:
                continue
            instance = getattr(info, "instance", None)
            if instance and not getattr(instance, "is_inactive", lambda: False)():
                return info
        return None

    def __make_episode_resolver(self, base: str):
        """生成物化用的本地集定位器：按 (季,集)+体积容差在源目录找文件。

        供 materialize() 处理多集候选（如 {1,2}）：除当前单元这一集外，
        候选种子里的其它集也要在源目录找到本地对应文件才能正确硬链接。
        忽略传入的 rel 文件名（跨发布组必不同），只认解析出的集号 + 体积。
        """
        index = self.__local_episode_index(base)

        def resolve(_rel: str, episode: int, length: int) -> str:
            # rel 里带季号时用它，避免跨季集号撞车；解析不到则用 0（匹配任意季）
            rel_season, _rel_ep, _is_ep = episode_info(os.path.basename(_rel))
            candidates: List[Tuple[str, int]] = []
            for (season, ep), items in index.items():
                if ep != episode:
                    continue
                if rel_season and season and season != rel_season:
                    continue
                candidates.extend(items)
            if not candidates:
                return ""
            want = int(length or 0)
            if want:
                for path, size in candidates:
                    if abs(size - want) <= max(int(want * 0.15), 64 * 1024 * 1024):
                        return path
            return candidates[0][0]

        return resolve

    def __match_local_episode(self, base: str, item: dict) -> bool:
        """按季集号 + 体积在本地目录里找种子内同一集的文件。

        跨站辅种时两边常出自不同发布组（`[生逢其时].In.My.Prime...` 对
        `In My Prime S01E12-E13...`），文件名对不上，只按名字精确匹配会把
        整包全判成「本地没有」。体积用来排除「同一集但不同版本」，差太远不算命中。
        """
        rel = os.path.join(*item["path"]) if item.get("path") else ""
        season, episode, is_episode = episode_info(os.path.basename(rel))
        if not is_episode or not episode:
            return False
        index = self.__local_episode_index(base)
        want = int(item.get("length") or 0)
        for _path, size in index.get((season, episode), []):
            if not want:
                return True
            if abs(size - want) <= max(int(want * 0.15), 64 * 1024 * 1024):
                return True
        return False

    def __local_episode_index(self, base: str) -> Dict[Any, List[Tuple[str, int]]]:
        """把本地目录里的视频文件按 (季, 集) 建索引，按目录缓存。"""
        cached = self._ep_index.get(base)
        if cached is not None:
            return cached
        index: Dict[Any, List[Tuple[str, int]]] = {}
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                try:
                    if not os.path.isfile(path) or not _is_video(name):
                        continue
                    size = os.path.getsize(path)
                except OSError:
                    continue
                season, episode, is_episode = episode_info(name)
                if not is_episode or not episode:
                    continue
                index.setdefault((season, episode), []).append((path, size))
        except OSError:
            pass
        self._ep_index[base] = index
        return index

    # ---------------------------------------------------------------- 增量缓存
    # 三类缓存合起来解决「每轮全量重跑」：
    #   1) 单元结论缓存：记住每个单元上一轮的结论（已辅种 / 已存在 / 无候选 / 校验不过），
    #      下轮直接跳过，机制同 IYUU 的 success/error 缓存。
    #   2) 候选种子 blob 缓存：同一个候选种子文件不重复下载，省站点请求。
    # 失败类结论带 TTL（站点可能后来新发种），成功类结论也带 TTL 兜底（用户可能删了目标种子）。

    def __cache_ttl(self, verdict: str) -> float:
        """不同结论的有效期（秒）。失败类用配置天数，成功类给更长兜底。"""
        days = max(1, int(self._cache_days or 7))
        if verdict in ("seeded", "dup"):
            # 已辅种/已存在：给 3 倍兜底，避免用户删了目标种子后永远不再辅
            return days * 3 * 86400
        return days * 86400

    def __load_unit_cache(self) -> Dict[str, dict]:
        raw = self.get_data(_KEY_UNIT_CACHE) or {}
        return raw if isinstance(raw, dict) else {}

    def __unit_cache_get(self, cache: Dict[str, dict], unit: Unit) -> Optional[str]:
        """命中则返回 verdict；未命中/已过期/体积不符返回 None。"""
        if not self._use_cache:
            return None
        item = cache.get(unit.key)
        if not isinstance(item, dict):
            return None
        # 同路径换了内容（体积变了）视为不同单元，缓存失效
        try:
            if int(item.get("size") or 0) != int(unit.size or 0):
                return None
        except (TypeError, ValueError):
            return None
        verdict = str(item.get("verdict") or "")
        try:
            at = float(item.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        if not verdict or not at:
            return None
        if time.time() - at > self.__cache_ttl(verdict):
            return None
        return verdict

    def __unit_cache_put(self, cache: Dict[str, dict], unit: Unit, verdict: str) -> None:
        if not verdict:
            return
        cache[unit.key] = {"size": int(unit.size or 0), "verdict": verdict, "at": time.time()}

    def __prune_unit_cache(self, cache: Dict[str, dict]) -> Dict[str, dict]:
        """清理过期项并限制总量，防止数据无限增长。"""
        now = time.time()
        out: Dict[str, dict] = {}
        for k, v in cache.items():
            if not isinstance(v, dict):
                continue
            verdict = str(v.get("verdict") or "")
            try:
                at = float(v.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if not verdict or now - at > self.__cache_ttl(verdict):
                continue
            out[k] = v
        # 上限 20000 条，超了按时间淘汰最旧的
        if len(out) > 20000:
            items = sorted(out.items(), key=lambda kv: float(kv[1].get("at") or 0), reverse=True)
            out = dict(items[:20000])
        return out

    def __blob_key(self, cand: Candidate) -> str:
        return f"{cand.site}|{cand.title}|{int(cand.size or 0)}"

    def __blob_cache_get(self, cand: Candidate) -> bytes:
        """从内存缓存取候选种子。run_once 已把持久化内容加载到 self._blob_cache，
        这里不再每次读库（否则每个候选都要读一次 plugin_data，累积可观）。"""
        if not self._use_cache:
            return b""
        cache = self._blob_cache if isinstance(self._blob_cache, dict) else {}
        item = cache.get(self.__blob_key(cand))
        if not isinstance(item, dict):
            return b""
        try:
            at = float(item.get("at") or 0)
        except (TypeError, ValueError):
            return b""
        if time.time() - at > max(1, int(self._cache_days or 7)) * 86400:
            return b""
        import base64
        try:
            return base64.b64decode(item.get("b64") or "")
        except Exception:  # noqa: BLE001
            return b""

    def __blob_cache_put(self, cand: Candidate, blob: bytes, cache: Dict[str, dict]) -> None:
        """写 blob 缓存。只缓存真种子（bencode 字典），单条上限 512KB，总量上限 500 条。"""
        if not self._use_cache or not blob or blob[:1] != b"d" or len(blob) > 512 * 1024:
            return
        import base64
        cache[self.__blob_key(cand)] = {
            "b64": base64.b64encode(blob).decode("ascii"),
            "at": time.time(),
        }

    def __prune_blob_cache(self, cache: Dict[str, dict]) -> Dict[str, dict]:
        now = time.time()
        ttl = max(1, int(self._cache_days or 7)) * 86400
        out: Dict[str, dict] = {}
        for k, v in cache.items():
            if not isinstance(v, dict):
                continue
            try:
                at = float(v.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if now - at > ttl:
                continue
            out[k] = v
        if len(out) > 500:
            items = sorted(out.items(), key=lambda kv: float(kv[1].get("at") or 0), reverse=True)
            out = dict(items[:500])
        return out

    def __is_duplicate(self, target: Any, meta: dict) -> bool:
        """任一已配置下载器里是否已有这个种子。

        必须在物化之前问。除了源任务自身（op-TR 同时是源和目标），多台
        下载器通过 NFS 指向同一物理目录时，同一 hash 在两台里各加一遍会
        触发 "You cannot seed the same torrent in the same location" 错误。
        """
        hash_str = str((meta or {}).get("infohash") or "").lower()
        if not hash_str:
            return False
        instances = list(self._all_instances or [])
        if target:
            inst = getattr(target, "instance", None)
            if inst is not None and inst not in instances:
                instances.append(inst)
        for instance in instances:
            try:
                existing = instance.get_torrents(ids=[hash_str])
                items = existing[0] if isinstance(existing, tuple) else existing
                if items:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def __add_torrent(self, target, blob: bytes, meta: dict, ops: List[Operation],
                      site: str = "") -> bool:
        """把种子加到目标下载器，download_dir 指向副本根目录的父级。

        标签参数各家不同：Transmission 认 labels（列表），qBittorrent 认 tag
        （字符串）。按 ServiceInfo 的 type 分派，别把 TR 的参数名硬塞给 qB。

        打了两类标签：配置的通用标签（如「拆包辅种」）+ 候选来源站点名，
        方便在下载器里按站点筛选、与原始整包任务区分。
        """
        instance = getattr(target, "instance", None)
        if not instance:
            return False
        kind = str(getattr(target, "type", "") or "").lower()
        is_qb = kind.startswith("qb")
        # 按 infohash 判重，避免同一资源被重复添加
        hash_str = str(meta.get("infohash") or "").lower()
        if hash_str:
            try:
                existing = instance.get_torrents(ids=[hash_str])
                items = existing[0] if isinstance(existing, tuple) else existing
                if items:
                    # 已存在：若被暂停且已完整，恢复做种（猪猪/青蛙经验：加种前先看有没有被暂停的旧任务）
                    self.__resume_if_paused(instance, kind, items, hash_str)
                    self._log(f"目标下载器已有该种子，跳过: {str(meta.get('name') or '')[:40]}")
                    return False
            except Exception as err:  # noqa: BLE001
                logger.warning(f"拆包辅种：判重查询失败 {err}")
        # 副本根目录 = output_dir/<种子名>；种子内容从 output_dir 开始按种子内路径落盘
        download_dir = self._output_dir
        kwargs: Dict[str, Any] = {
            "content": blob,
            "is_paused": not self._auto_start,
            "download_dir": download_dir,
        }
        # 标签 = 配置的通用标签 + 候选来源站点名（去空、去重、保序）。
        # qB 额外加一枚随机临时 tag，便于加种后用 get_torrent_id_by_tag 精确定位本次任务
        # （借鉴 IYUU/青蛙：新任务索引有延迟，靠 hash 反查可能落空）。
        labels: List[str] = []
        for value in (self._label, str(site or "").strip()):
            if value and value not in labels:
                labels.append(value)
        trace_tag = ""
        if labels:
            if is_qb:
                trace_tag = "pcs_" + datetime.now().strftime("%H%M%S%f")[-8:]
                tags = labels + [trace_tag]
                kwargs["tag"] = ",".join(tags)
                # 强制关闭 qB 自动种子管理(ATM)：ATM 会用分类目录覆盖 download_dir，
                # 让辅种落到别处、原种文件不被复用而表现为“文件丢失”（IYUU 踩过的坑）。
                # ignore_category_check=False + category=None => 下载器侧 is_auto=False。
                kwargs["ignore_category_check"] = False
                kwargs["category"] = None
            else:
                kwargs["labels"] = labels
        try:
            ret = instance.add_torrent(**kwargs)
        except Exception as err:  # noqa: BLE001
            self._log(f"加种失败: {str(err)[:100]}")
            return False
        # add_torrent 现版本返回 (bool, [ids])，旧版本可能只返回 bool/真值对象。
        # 旧代码无条件判成功，会把失败也算成 seeded。这里兼容解析。
        added_ok, added_ids = self.__parse_add_result(ret)
        if not added_ok:
            self._log(f"加种失败: 下载器返回失败 - {str(meta.get('name') or '')[:40]}")
            return False
        if not self._verify_after_add:
            ops.append(Operation("add_torrent", download_dir, meta.get("name", ""), "在下载器里删除对应任务"))
            self._log(f"已加种: {meta.get('name','')[:50]}")
            return True

        # --- 加种后复核：确认新任务的真实 hash/状态，防 add_torrent 返回旧任务陷阱 ---
        live_id = self.__resolve_added_id(instance, kind, hash_str, added_ids, trace_tag)
        if not live_id:
            # 拿不到本次任务标识，无法确认，保守记为失败（宁可漏报也别误报做种）
            self._log(f"加种后无法定位新任务，已加入下载器但不计成功: {str(meta.get('name') or '')[:40]}")
            return False
        live = self.__get_one_torrent(instance, live_id)
        if live is None:
            self._log(f"加种后查询任务失败: {str(meta.get('name') or '')[:40]}")
            return False
        live_hash = str(self.__attr(live, "hash", "hash_string", "hashString", default="") or "").lower()
        # 若下载器回报的 hash 与目标不符，说明加错任务（极端情况下返回了旧任务）
        if live_hash and hash_str and live_hash != hash_str:
            self._log(f"加种复核 hash 不符（{live_hash[:8]} != {hash_str[:8]}），不计成功")
            return False
        # 复核保存路径：下载器实际落位必须等于我们给的 download_dir。
        # qB 的自动种子管理(ATM)或分类目录可能把它改到别处，那样副本文件不被
        # 复用，表现为「加进去了但一直 0%」。
        live_save = str(self.__attr(live, "save_path", "download_dir", "downloadDir",
                                    default="") or "")
        if live_save and download_dir:
            a = live_save.rstrip("/")
            b = str(download_dir).rstrip("/")
            if a != b:
                self._log(
                    f"加种复核 save_path 不一致（{a} != {b}），"
                    f"副本可能不被复用，请检查下载器自动管理/分类设置"
                )
        # 触发 recheck，让 qB/TR 按硬链接副本做校验
        try:
            instance.recheck_torrents(ids=[live_id])
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：触发 recheck 失败 {err}")
        # 清理 qB 临时追踪 tag
        if is_qb and trace_tag:
            try:
                instance.remove_torrents_tag(ids=[live_id], tag=[trace_tag])
            except Exception:  # noqa: BLE001
                pass
        ops.append(Operation("add_torrent", download_dir, meta.get("name", ""), "在下载器里删除对应任务"))
        self._log(f"已加种并触发校验: {meta.get('name','')[:50]}")
        return True

    @staticmethod
    def __parse_add_result(ret: Any) -> Tuple[bool, List[str]]:
        """兼容 add_torrent 的多种返回：新版本 (bool, [ids])、旧版本 bool/对象。"""
        if isinstance(ret, tuple) and len(ret) >= 2:
            ok = bool(ret[0])
            raw = ret[1] or []
            if isinstance(raw, str):
                raw = [raw]
            try:
                ids = [str(x) for x in raw if x]
            except TypeError:
                ids = []
            return ok, ids
        # 非 tuple：真值即成功，但没有可用 id
        return bool(ret), []

    @staticmethod
    def __get_one_torrent(instance: Any, torrent_id: str) -> Optional[Any]:
        """按 id 精确取单个任务，兼容 get_torrents 的 tuple 包装。"""
        try:
            r = instance.get_torrents(ids=[torrent_id])
            items = r[0] if isinstance(r, tuple) else r
            if isinstance(items, (list, tuple)):
                return items[0] if items else None
            return items
        except Exception:  # noqa: BLE001
            return None

    def __resolve_added_id(self, instance: Any, kind: str, hash_str: str,
                           added_ids: List[str], trace_tag: str) -> str:
        """确定本次新加任务的 id：优先下载器返回的 ids，其次 qB 临时 tag 反查。"""
        for tid in added_ids:
            if tid:
                return tid
        if trace_tag and str(kind).lower().startswith("qb"):
            try:
                tid = instance.get_torrent_id_by_tag(tags=trace_tag)
                if tid:
                    return str(tid)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"拆包辅种：临时 tag 反查失败 {err}")
        # 最后按目标 hash 反查（源任务也是这个 hash，但前面已判重排除，这里可接受）
        if hash_str:
            live = self.__get_one_torrent(instance, hash_str)
            if live is not None:
                return hash_str
        return ""

    def __resume_if_paused(self, instance: Any, kind: str, items: Any, hash_str: str) -> None:
        """目标已有该种子且被暂停、进度已满时恢复做种（防加种陷阱留下的僵尸暂停任务）。"""
        try:
            item = items[0] if isinstance(items, (list, tuple)) and items else items
            if not item:
                return
            done = self.__attr(item, "percent_done", "percentDone", "progress", default=0)
            try:
                done = float(done or 0)
            except (TypeError, ValueError):
                done = 0.0
            state = str(self.__attr(item, "state", "status", default="") or "").lower()
            is_paused = state in ("paused", "stopped") or bool(
                self.__attr(item, "paused", default=False)
            )
            if done >= 0.999 and is_paused:
                # 两个下载器模块都用 start_torrents（复数）
                instance.start_torrents(ids=[hash_str])
                self._log(f"已有任务被暂停且完整，已恢复做种: {hash_str[:12]}")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"拆包辅种：恢复暂停任务失败 {err}")

    def __align_single_file(self, meta: dict, unit: Unit, ops: List[Operation]) -> None:
        """单文件种子：确保按种子内文件名在 output_dir 下有一份副本。

        单文件种子的 info.name 就是文件名，下载器直接在 download_dir 下找它，
        不能再套一层同名目录。
        """
        files = meta.get("files") or []
        # 目录模式（有 files 字段）即便只有一个文件也由 materialize 建目录，
        # 这里只补真正的单文件种子（info.name 就是文件名）。
        if meta.get("multi") or len(files) != 1:
            return
        want = os.path.join(self._output_dir, meta.get("name", ""))
        src = unit.source_path
        if os.path.abspath(want) == os.path.abspath(src):
            return
        if os.path.exists(want):
            return
        parent = os.path.dirname(want)
        os.makedirs(parent, exist_ok=True)
        try:
            os.link(src, want)
            ops.append(Operation("hardlink", want, src, f"unlink {want}"))
        except OSError:
            try:
                os.symlink(src, want)
                ops.append(Operation("symlink", want, src, f"unlink {want}"))
            except OSError:
                pass

    # ================================================================ 小工具
    @staticmethod
    def __as_list(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return []

    @staticmethod
    def __as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __unit_brief(u: Unit) -> dict:
        return {
            "name": u.name,
            "size": u.size,
            "is_episode": u.is_episode,
            "season": u.season,
            "episode": u.episode,
            "source_task": u.source_task,
            "source_episodes": sorted(u.source_episodes),
            "candidates": len(u.candidates),
            "keyword": u.search_keyword,
        }

    def _log(self, message: str) -> None:
        logger.info(f"[拆包辅种] {message}")
