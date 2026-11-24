# -*- coding: utf-8 -*-
"""做种健康巡查（只做本地检查）。

范围与边界（重要）：
  - 只读本地下载器状态与本地文件系统，**不访问任何站点**。
    站点侧「是否被删种」这类探测需要逐种子请求详情页，成本高且容易触发站点
    限制，本模块不做，也不应该由插件自动去做。
  - 检查三类问题：
      1) 文件缺失：任务声明的文件在磁盘上不存在
      2) 盘未挂载：路径所在的挂载点不在位（此时不能判为缺失，只能报未知）
      3) tracker 报错：下载器回报该任务的 tracker 状态为错误
  - 挂载点判定用「以挂载根为锚」：挂载根存在而路径不存在 = 真缺失；
    挂载根本身不在 = 盘没挂上，记为未知而不是缺失，避免误报一片。

本模块不 import app.*，保持可独立语法检查。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 巡查结论
STATE_OK = "ok"
STATE_MISSING = "missing"       # 挂载根在、但文件/目录不在
STATE_UNMOUNTED = "unmounted"   # 挂载根不在，盘可能没挂上
STATE_TRACKER = "tracker"       # tracker 报错
STATE_UNKNOWN = "unknown"       # 无法判定


@dataclass
class HealthItem:
    task: str                 # 任务名
    downloader: str           # 下载器
    path: str                 # 相关路径（内容路径或任务路径）
    state: str                # 上面五种之一
    detail: str = ""          # 说明
    hit_count: int = 1        # 该问题影响的种子数（按路径聚合时 > 1）


@dataclass
class HealthReport:
    at: str = ""
    total: int = 0
    ok: int = 0
    missing: int = 0
    unmounted: int = 0
    tracker: int = 0
    unknown: int = 0
    items: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    # 检查覆盖范围：MoviePilot 的下载器查询默认只取 trackerStats + downloadDir，
    # 不返回逐文件清单，所以这里只能做「目录级」检查。真机验证过，写死在报告里，
    # 避免使用者误以为查了每个文件。
    scope: str = "directory"
    scope_detail: str = "按任务保存目录与 tracker 状态检查，不含逐文件校验"


def _mount_root(path: str) -> str:
    """向上找到 path 所属的挂载点（一定存在，最差是 /）。"""
    p = os.path.abspath(path)
    while True:
        try:
            if os.path.ismount(p):
                return p
        except OSError:
            pass
        parent = os.path.dirname(p)
        if parent == p:
            return p
        p = parent


def classify_path(path: str) -> Tuple[str, str]:
    """判定一个本地路径的可见性，返回 (state, detail)。

    以挂载根为锚：
      - 路径存在            -> ok
      - 挂载根不在（盘没挂） -> unmounted
      - 挂载根在、路径不在   -> missing（含中间目录被删）
    """
    if not path:
        return STATE_UNKNOWN, "路径为空"
    if os.path.exists(path):
        return STATE_OK, ""
    root = _mount_root(path)
    try:
        root_ok = os.path.exists(root)
    except OSError:
        root_ok = False
    if not root_ok:
        return STATE_UNMOUNTED, f"挂载点 {root} 不在，盘可能未挂载"
    return STATE_MISSING, f"{path} 不存在（挂载点 {root} 正常）"


def _torrent_files(torrent: Any, attr: Callable[..., Any]) -> List[Tuple[str, int]]:
    """取任务文件清单，兼容 qB / TR 两种字段命名。"""
    files = attr(torrent, "files", "files_list", default=None)
    out: List[Tuple[str, int]] = []
    if not files:
        return out
    for f in files:
        if isinstance(f, dict):
            name = f.get("name") or f.get("path") or ""
            size = int(f.get("size") or f.get("length") or 0)
        else:
            name = str(getattr(f, "name", "") or "")
            size = int(getattr(f, "size", getattr(f, "length", 0)) or 0)
        if name:
            out.append((str(name), size))
    return out


def _tracker_has_error(torrent: Any, attr: Callable[..., Any]) -> str:
    """检查 tracker 是否报错，返回错误说明（空串表示正常）。"""
    stats = attr(torrent, "tracker_stats", "trackerStats", default=None)
    if isinstance(stats, (list, tuple)):
        for st in stats:
            if not isinstance(st, dict):
                continue
            # TR: lastAnnounceResult / qB 用 tracker 的 status
            res = str(st.get("lastAnnounceResult") or st.get("last_announce_result") or "")
            msg = str(st.get("lastAnnounceSucceeded", "") or "")
            if res and res.lower() not in ("success", "success ", ""):
                return res[:120]
            if msg is False:
                return "announce 未成功"
    return ""


def check_task(
    torrent: Any,
    downloader: str,
    attr: Callable[..., Any],
) -> List[HealthItem]:
    """检查单个做种任务，返回发现的问题（没有问题时返回空列表）。"""
    name = str(attr(torrent, "name", "title", default="") or "")
    save_path = str(attr(torrent, "save_path", "download_dir", "downloadDir", default="") or "")
    content_path = str(attr(torrent, "content_path", "contentPath", default="") or "")

    items: List[HealthItem] = []

    # 1) tracker 报错
    err = _tracker_has_error(torrent, attr)
    if err:
        items.append(HealthItem(name, downloader, save_path or content_path,
                                STATE_TRACKER, err))

    # 2) 文件可见性：优先看内容路径，再逐个文件
    main_path = content_path or save_path
    if main_path:
        state, detail = classify_path(main_path)
        if state in (STATE_MISSING, STATE_UNMOUNTED):
            items.append(HealthItem(name, downloader, main_path, state, detail))
            return items

    files = _torrent_files(torrent, attr)
    if files and save_path:
        base = save_path
        # 任务内容可能落在 save_path/<包名>/ 下，也可能直接落在 save_path
        for rel, _size in files:
            cand = os.path.join(base, rel)
            if os.path.exists(cand):
                continue
            state, detail = classify_path(cand)
            if state in (STATE_MISSING, STATE_UNMOUNTED):
                items.append(HealthItem(name, downloader, cand, state, detail))
                break  # 每任务只报首个缺失文件，避免刷屏
    return items


def build_report(
    tasks: Sequence[Tuple[Any, str]],
    attr: Callable[..., Any],
    at: str = "",
    limit: int = 200,
) -> HealthReport:
    """扫描全部做种任务，汇总成报告。

    tasks: [(torrent 对象, 下载器名), ...]
    """
    report = HealthReport(at=at)
    report.total = len(tasks)
    buckets: Dict[Tuple[str, str], HealthItem] = {}

    for torrent, downloader in tasks:
        problems = check_task(torrent, downloader, attr)
        if not problems:
            report.ok += 1
            continue
        for p in problems:
            if p.state == STATE_MISSING:
                report.missing += 1
            elif p.state == STATE_UNMOUNTED:
                report.unmounted += 1
            elif p.state == STATE_TRACKER:
                report.tracker += 1
            else:
                report.unknown += 1
            # 同一路径的缺失合并成一条（一个目录缺失常影响同片多个种子）
            key = (p.state, p.path)
            if key in buckets:
                buckets[key].hit_count += 1
            else:
                buckets[key] = p

    merged = sorted(buckets.values(), key=lambda x: (-x.hit_count, x.path))
    report.items = [
        {
            "task": it.task,
            "downloader": it.downloader,
            "path": it.path,
            "state": it.state,
            "detail": it.detail,
            "hit_count": it.hit_count,
        }
        for it in merged[:limit]
    ]
    if report.unmounted:
        report.note = "存在未挂载的存储，相关任务已跳过判定（未计为缺失）"
    return report
