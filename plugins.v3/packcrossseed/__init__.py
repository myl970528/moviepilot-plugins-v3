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
    discover_units,
    materialize,
    parse_torrent,
    screen_candidates,
    verify_local_file_in_torrent,
)
from . import config_form

_PLUGIN_ID = "packcrossseed"
_KEY_PLAN = "pcs_plan"           # 当前试跑计划
_KEY_RESULT = "pcs_result"       # 上轮结果
_KEY_HISTORY = "pcs_history"     # 执行历史
_KEY_OPS = "pcs_operations"      # 写操作 journal


class PackCrossSeed(_PluginBase):
    plugin_name = "拆包辅种"
    plugin_desc = "整包按电影/单集拆分，跨站匹配独立种子并做 piece 校验，硬链接生成副本后辅种。原包不动。"
    plugin_icon = "package.png"
    plugin_version = "1.0.7"
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
    _link_mode: str = "auto"
    _label: str = ""
    _auto_start: bool = True
    _notify: bool = True
    _cron: str = ""
    _auto_mode: bool = False
    _onlyonce: bool = False
    _debug: bool = False

    _running: bool = False
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
            self._link_mode = str(config.get("link_mode") or "auto").strip()
            self._label = str(config.get("label") or "").strip()
            self._auto_start = bool(config.get("auto_start", True))
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
            "link_mode": self._link_mode,
            "label": self._label,
            "auto_start": self._auto_start,
            "notify": self._notify,
            "cron": self._cron,
            "auto_mode": self._auto_mode,
            "onlyonce": self._onlyonce,
            "debug": self._debug,
        }

    def __downloader_choices(self) -> List[dict]:
        """运行时从 MP 拉取下载器列表（只回名字，不含地址/账号）。"""
        try:
            services = DownloaderHelper().get_services(type_filter="transmission") or {}
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
        """详情页：KPI + 当前计划 + 执行历史 + 说明。全部读本地缓存。"""
        result = self.get_data(_KEY_RESULT) or {}
        plan = self.get_data(_KEY_PLAN) or {}
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
             f"候选 {result.get('candidates') or 0} 个"),
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

        # 当前计划（试跑预览）
        units = plan.get("units") or []
        if units:
            rows: List[List[Tuple[str, str]]] = []
            for i, u in enumerate(units[:25], 1):
                rows.append([
                    (str(i), "text-body-2 text-medium-emphasis"),
                    (str(u.get("name") or "")[:44], "text-body-2"),
                    (f"{int(u.get('size') or 0)/1024/1024/1024:.2f} GB", "text-body-2 text-no-wrap"),
                    ("单集" if u.get("is_episode") else "电影", "text-body-2 text-center"),
                    (str(u.get("candidates") or 0), "text-body-2 text-center"),
                ])
            heads = ["#", "单集 / 影片", "体积", "类型", "候选"]
            table = [{
                "component": "VTable", "props": {"density": "comfortable"},
                "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [
                        {"component": "th", "props": {"class": "text-caption text-medium-emphasis"}, "text": t} for t in heads
                    ]}]},
                    {"component": "tbody", "content": [{"component": "tr", "content": [
                        {"component": "td", "props": {"class": c}, "text": t} for t, c in row
                    ]} for row in rows]},
                ],
            }]
            page.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VIcon", "props": {"color": "primary", "class": "mr-2"}, "text": "mdi-format-list-bulleted"},
                        {"component": "span", "text": f"当前试跑计划（最多 25 条，共 {len(units)} 条）"},
                    ]},
                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": table},
                ],
            }]}]})

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
                    "使用顺序：① 保持试跑模式，点「扫描生成计划」看拆出哪些单元；"
                    "② 点「执行一轮」看校验结果（不写盘）；③ 确认无误后关掉试跑模式再执行。"
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
        started = time.time()
        stats: Dict[str, Any] = {
            "units": 0, "tasks": 0, "candidates": 0, "verified": 0,
            "materialized": 0, "seeded": 0, "failed": 0,
            "mode": "试跑" if self._dry_run else "执行",
            "message": "",
        }
        ops_new: List[dict] = []
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

            for i, unit in enumerate(units, 1):
                if self._stop_flag:
                    stats["message"] = f"已手动停止，剩余 {len(units) - i + 1} 个单元未处理"
                    self._log(stats["message"])
                    break
                self._progress["done"] = i
                try:
                    raw = self.__search(unit)
                    cands = screen_candidates(
                        unit, raw,
                        size_tolerance=self._size_tolerance / 100.0,
                        min_similarity=self._min_similarity,
                        max_candidates=self._max_candidates,
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
                        continue
                    stats["verified"] += 1

                    if self._dry_run:
                        unit.candidates = [best]
                        continue

                    # --- 4. 物化
                    seed_blob = best.torrent_blob
                    meta = parse_torrent(seed_blob)
                    if not meta:
                        stats["failed"] += 1
                        continue
                    ops, missing = materialize(
                        meta,
                        src_base=os.path.dirname(unit.source_path),
                        dst_base=self._output_dir,
                        mode=self._link_mode,
                        log=self._log,
                        target_source=unit.source_path,
                        season=unit.season,
                        episode=unit.episode,
                    )
                    if missing:
                        self._log(
                            f"  {unit.name[:30]}: 这个种子需要 {len(missing)} 个本地没有的文件，"
                            f"加进去会一直校验失败，跳过"
                        )
                        stats["failed"] += 1
                        continue
                    # unit 源文件名可能与种子内文件名不同，修正硬链接
                    self.__align_single_file(meta, unit, ops)
                    ops_new.extend([o.__dict__ for o in ops])
                    stats["materialized"] += sum(
                        1 for o in ops if o.action in ("hardlink", "symlink", "copy")
                    )
                    # --- 5. 加种
                    if self.__add_torrent(target, seed_blob, meta, ops):
                        stats["seeded"] += 1
                except Exception as err:  # noqa: BLE001
                    stats["failed"] += 1
                    logger.warning(f"拆包辅种：处理 {unit.name[:40]} 失败 {err}")

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
                          f"物化 {stats['materialized']} · 加种 {stats['seeded']} · "
                          f"耗时 {stats['elapsed']}s" + (f"\n{stats['message']}" if stats["message"] else "")),
                )

    # ================================================================ 内部方法
    def __collect_tasks(self) -> List[dict]:
        """从已配置下载器收集已完成任务的种子路径与保存目录。"""
        services = DownloaderHelper().get_services(type_filter="transmission") or {}
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
                    pct = float(self.__attr(t, "percent_done", "percentDone", default=0))
                except (TypeError, ValueError):
                    continue
                if self._only_finished and pct < 0.999:
                    continue
                save_path = str(self.__attr(t, "download_dir", "downloadDir", default="") or "")
                hash_str = str(self.__attr(t, "hash_string", "hashString", default="") or "").lower()
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
        try:
            results = SearchChain().search_by_title(
                title=unit.search_keyword,
                page=0,
                sites=sites,
                cache_local=False,
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
        return out

    def __verify_candidate(self, unit: Unit, cand: Candidate) -> Tuple[bool, str]:
        """下载候选种子 → 解析 → piece 校验本地单集文件。"""
        blob = self.__download_torrent(cand)
        if not blob:
            cand.verify_reason = "种子下载失败"
            return False, cand.verify_reason
        meta = parse_torrent(blob)
        if not meta:
            cand.verify_reason = "种子解析失败"
            return False, cand.verify_reason
        cand.torrent_blob = blob
        cand.infohash = meta.get("infohash") or ""
        # 整包/合集候选：本地缺文件就没必要再做 piece 校验和物化。
        # 否则每集都要下种子、逐条比对、最后才发现缺 43 集，白烧 IO 和日志。
        base = os.path.dirname(unit.source_path)
        need = len(meta.get("files") or [])
        if need > 1:
            have = 0
            for f in meta["files"]:
                rel = os.path.join(*f["path"]) if f["path"] else ""
                paths = [os.path.join(base, rel), os.path.join(base, os.path.basename(rel))]
                if any(p and os.path.exists(p) for p in paths):
                    have += 1
            if have < need:
                cand.verify_reason = (
                    f"站点是 {need} 个文件的{'整包' if need > 4 else '合集'}，"
                    f"本地只有 {have} 个，跳过"
                )
                return False, cand.verify_reason
        # 不再用「种子里必须有等大文件」当门槛：压制版本不同时体积必然不同，
        # 但仍是同一个片子，能否复用由 piece 校验按集号定位后判定。
        # 用本地单集文件的字节，按候选种子的 piece 边界做内容校验
        matched, checked, reason = verify_local_file_in_torrent(
            meta,
            unit.source_path,
            pieces_to_check=self._pieces_to_check or None,
            prefer_season=unit.season,
            prefer_episode=unit.episode,
        )
        cand.piece_total, cand.piece_matched = checked, matched
        cand.verify_ok = (checked > 0 and matched == checked and reason == "OK")
        cand.verify_reason = reason if not cand.verify_ok else f"通过 {matched}/{checked} 片"
        return cand.verify_ok, cand.verify_reason

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
        # 有些站点先回一段 JSON / 页面，里面才是临时下载地址
        text = body.decode("utf-8", "ignore") if body else ""
        link = re.search(r"https?://[^\s\"'<>]+\.torrent[^\s\"'<>]*", text) \
            or re.search(r"https?://[^\s\"'<>]{20,}", text)
        if code == 200 and link:
            real = link.group(0).replace("\\/", "/")
            try:
                res2 = RequestUtils(
                    ua=cand.ua or None, cookies=cand.cookie or None, timeout=30
                ).get_res(real, allow_redirects=True)
                body2 = getattr(res2, "content", b"") or b"" if res2 else b""
                if int(getattr(res2, "status_code", 0) or 0) == 200 and body2[:1] == b"d":
                    return bytes(body2)
            except Exception:  # noqa: BLE001
                pass
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

    def __download_torrent(self, cand: Candidate) -> bytes:
        """下载候选种子文件。站点凭据只在内存中传递，不落盘、不进日志。

        顺序：站点换票配方 → MP 自带下载服务 → 直连。
        """
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
            if content and not isinstance(content, str):
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
        return bytes(res.content)

    def __pick_target_downloader(self) -> Any:
        services = DownloaderHelper().get_services(type_filter="transmission") or {}
        want = self._target_downloader.strip().lower()
        for name, info in services.items():
            if want and str(name).lower() != want:
                continue
            instance = getattr(info, "instance", None)
            if instance and not getattr(instance, "is_inactive", lambda: False)():
                return info
        return None

    def __add_torrent(self, target, blob: bytes, meta: dict, ops: List[Operation]) -> bool:
        """把种子加到目标下载器，download_dir 指向副本根目录的父级。"""
        instance = getattr(target, "instance", None)
        if not instance:
            return False
        # 按 infohash 判重，避免同一资源被重复添加
        hash_str = str(meta.get("infohash") or "").lower()
        if hash_str:
            try:
                existing = instance.get_torrents(ids=[hash_str])
                items = existing[0] if isinstance(existing, tuple) else existing
                if items:
                    self._log(f"目标下载器已有该种子，跳过: {str(meta.get('name') or '')[:40]}")
                    return False
            except Exception as err:  # noqa: BLE001
                logger.warning(f"拆包辅种：判重查询失败 {err}")
        # 副本根目录 = output_dir/<种子名>；TR 的种子内容从 output_dir 开始按种子内路径落盘
        download_dir = self._output_dir
        labels = [self._label] if self._label else None
        try:
            instance.add_torrent(
                content=blob,
                is_paused=not self._auto_start,
                download_dir=download_dir,
                labels=labels,
            )
            ops.append(Operation("add_torrent", download_dir, meta.get("name", ""), "在下载器里删除对应任务"))
            self._log(f"已加种: {meta.get('name','')[:50]}")
            return True
        except Exception as err:  # noqa: BLE001
            self._log(f"加种失败: {str(err)[:100]}")
            return False

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
            "candidates": len(u.candidates),
            "keyword": u.search_keyword,
        }

    def _log(self, message: str) -> None:
        logger.info(f"[拆包辅种] {message}")
