# -*- coding: utf-8 -*-
"""拆包辅种核心引擎。

职责（五块）：
  1. 发现：从 MP 已配置的下载器里读「已完成任务」，解析其 .torrent，
     按「电影整文件 / 剧集单集」拆成待匹配单元。
  2. 匹配：用单集文件名走 MP 的站内搜索，按体积 + 名称相似度粗筛候选，
     拿到候选的种子下载地址 / 站点 cookie / UA / 站点 ID。
  3. 校验：用 MP 的种子下载服务把候选 .torrent 拉下来，torrentool 解出
     pieces，对本地单集文件逐片做 SHA1 对比（支持跨文件 piece 字节流）。
  4. 物化：校验通过后，在目标目录按候选种子的文件布局，优先硬链接、
     再软链接、最后复制，生成独立副本。源文件全程只读。
  5. 加种：把候选种子交给目标下载器（默认 op-TR），指定新目录，
     is_paused 添加后由下载器 recheck 开始做种。

安全边界：
  - 默认 dry_run：只做发现 + 匹配 + 校验，不物化、不加种。
  - 源文件只读，object journal 记录每一步，可回滚（删新目录 + 删新任务）。
  - 站内搜索 / 种子下载复用 MP 自带链路，插件不自己访问任何站点，
    也不在代码里保存任何站点凭据。

本模块不 import app.*，保证可独立语法检查；与 MP 的耦合全部收敛到
__init__.py，通过依赖注入把「搜索器 / 下载器服务 / 日志」传进来。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------- 常量

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".rmvb", ".rm", ".m4v", ".mpg", ".mpeg", ".vob", ".webm",
}
# 原盘结构与镜像：没有集号语义，站点侧也不存在对应的独立种子。
# 这类文件拆出来只会拿 00302.m2ts 这样的名字去搜，白烧一次全站搜索。
SEGMENT_EXT = {".iso", ".bdmv"}
SEGMENT_PIECE_EXT = {".m2ts", ".mts", ".vob"}
SEGMENT_DIRS = {"bdmv", "video_ts", "certificate", "bdmv_stream", "stream"}
# 搜索关键词里要剔除的压制/分辨率噪声词
NOISE_WORDS = {
    "web", "webdl", "webrip", "dl", "bluray", "bdrip", "brrip", "hdtv",
    "remux", "h264", "h265", "x264", "x265", "hevc", "avc", "aac", "ac3",
    "eac3", "flac", "dts", "ddp", "truehd", "atmos", "10bit", "8bit",
    "hdr", "hdr10", "dv", "dolby", "vision", "60fps", "1080p", "2160p",
    "720p", "4k", "2k", "576p", "480p", "uc", "uncut", "extended",
}
# 集数识别：S01E02 / 1x02 / EP02 / 第02集 / .02. / E02
EPISODE_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})"),
    re.compile(r"(?<!\d)(\d{1,2})x(\d{1,4})(?!\d)"),
    re.compile(r"\bEP?\.?\s?(\d{1,4})\b", re.I),
    re.compile(r"第\s*(\d{1,4})\s*集"),
]


@dataclass
class Unit:
    """一个待匹配单元（一部电影或一个单集）。"""
    key: str                      # 稳定标识
    name: str                     # 展示名
    source_path: str              # 本地源文件绝对路径
    size: int                     # 源文件字节数
    search_keyword: str           # 站内搜索关键词
    source_task: str = ""         # 来源下载器任务名
    source_downloader: str = ""   # 来源下载器名
    source_torrent: str = ""      # 来源种子文件名
    season: int = 0               # 识别到的季（0=电影/未知）
    episode: int = 0              # 识别到的集
    is_episode: bool = False      # 是否剧集单集
    # 该单元所属「源种子」包含的全部集号。拆包辅种只加种源种子的真子集
    # （单集 {1} / 两集 {1,2}），完整整包交给 iyuu 之类的通用辅种插件。
    source_episodes: Set[int] = field(default_factory=set)
    candidates: List["Candidate"] = field(default_factory=list)


@dataclass
class Candidate:
    """一个站内搜索候选种子。"""
    title: str
    size: int
    site: str
    site_id: Optional[int]
    enclosure: str                # 种子下载链接
    page_url: str = ""
    cookie: str = ""
    ua: str = ""
    referer: str = ""
    # 校验结果
    torrent_blob: bytes = b""
    infohash: str = ""
    piece_total: int = 0
    piece_matched: int = 0
    verify_ok: bool = False
    verify_reason: str = ""


@dataclass
class Operation:
    """一步写操作（journal）。"""
    action: str                   # link / symlink / copy / add_torrent / mkdir
    target: str
    detail: str = ""
    undo: str = ""                # 回滚说明


# ---------------------------------------------------------------- 工具函数

def normalize(text: str) -> str:
    """统一大小写、分隔符，便于名称比对。"""
    text = str(text or "").lower()
    text = re.sub(r"[\[\]()（）【】{}]", " ", text)
    text = re.sub(r"[._\-+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_video(name: str) -> bool:
    return Path(str(name)).suffix.lower() in VIDEO_EXT


def _is_segment(name: str) -> bool:
    """原盘分段/镜像文件（BDMV 的 .m2ts、VIDEO_TS 的 .vob、整盘 .iso）。

    判据分三层：整盘镜像直接排除；落在 BDMV / VIDEO_TS / STREAM 目录里的排除；
    纯数字命名的 .m2ts（00302.m2ts）也是分段。带集号的 `S01E02.m2ts` 仍按单集处理。
    """
    low = str(name).replace("\\", "/").lower()
    path = Path(low)
    if path.suffix in SEGMENT_EXT:
        return True
    if any(part in SEGMENT_DIRS for part in low.split("/")[:-1]):
        return True
    return path.suffix in SEGMENT_PIECE_EXT and path.stem.isdigit()


def episode_info(name: str) -> Tuple[int, int, bool]:
    """从文件名识别季/集。返回 (season, episode, is_episode)。"""
    base = Path(str(name)).stem
    for pat in EPISODE_PATTERNS[:2]:
        m = pat.search(base)
        if m:
            return int(m.group(1)), int(m.group(2)), True
    for pat in EPISODE_PATTERNS[2:]:
        m = pat.search(base)
        if m:
            ep = int(m.group(1))
            return 0, ep, True
    return 0, 0, False


# 英文虚词，不参与关键词，避免占用 token 名额
STOP_WORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "to", "with"}


def build_keyword(name: str, words: int = 4) -> str:
    """从单集文件名提取站内搜索关键词：保留主标题，剔除压制噪声。"""
    base = Path(str(name)).stem
    tokens: List[str] = []
    for piece in re.split(r"[\[\]()（）【】.\s_]+", base):
        token = piece.strip("-_+")
        if not token:
            continue
        low = token.lower()
        if low in NOISE_WORDS or low in STOP_WORDS or re.fullmatch(r"\d{3,4}[pi]", low):
            continue
        # 季集标记不进搜索词：按剧名搜全剧，再在本地按集号筛，召回更全
        if any(pat.search(token) for pat in EPISODE_PATTERNS[:2]):
            continue
        tokens.append(token)
        if len(tokens) >= words:
            break
    return " ".join(tokens) or base[:40]


def similarity(keyword: str, title: str) -> float:
    """关键词命中率：搜索词里的 token 有多少出现在标题中。

    用命中率而不是 Jaccard：站点标题通常比搜索词长得多（带年份、编码、
    压制组），Jaccard 会把「中文剧名 + 英文译名」这类正确结果压到门槛以下。
    """
    kt = set(normalize(keyword).split())
    tt = set(normalize(title).split())
    if not kt or not tt:
        return 0.0
    return len(kt & tt) / len(kt)


def size_close(a: int, b: int, tolerance: float) -> bool:
    """体积是否接近。tolerance 为比例，如 0.05 表示 5%。"""
    if not a or not b:
        return False
    return abs(a - b) / max(a, b) <= tolerance


# 版本特征词：用来判断候选与本地是不是同一压制版本。
# 同一版本的文件字节才可能一致，是拆包辅种能否成立的前提。
RELEASE_TAGS = (
    "2160p", "1080p", "1080i", "720p", "480p", "4k", "uhd",
    "hdr", "hdr10", "dolby", "dv", "sdr",
    "h264", "h265", "x264", "x265", "hevc", "avc", "av1",
    "web-dl", "webdl", "webrip", "bluray", "blu-ray", "remux", "hdtv", "dvdrip",
    "ddp", "dd+", "aac", "ac3", "eac3", "truehd", "atmos", "flac",
    "viu", "iq", "wetv", "dsnp", "nf", "amzn", "hmax", "atvp", "iqiyi", "baha",
)


def release_tags(name: str) -> set:
    """提取名称里的版本特征（分辨率 / 编码 / 来源 / 平台 / 音轨）。"""
    low = str(name).lower()
    return {t for t in RELEASE_TAGS if t in low}


def season_of(title: str) -> int:
    """只取季号，用于排除「别的季」的整包候选。0 表示没写季号。"""
    m = re.search(r"[Ss](\d{1,2})(?![\dEe])", str(title))
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*(\d{1,2})\s*季", str(title))
    if m:
        return int(m.group(1))
    return 0


def episodes_in_title(title: str) -> Set[int]:
    """从种子标题里提取出现的全部集号（含 E01-E24 这类区间展开）。

    用于「拆分作种」模式下提前识别整包候选、避免白下载种子。
    区间展开设有上限，防止 E01-E999 这类误写撑爆内存。
    """
    s = str(title or "")
    out: Set[int] = set()
    # 区间：E01-E24 / E01~E24 / S01E01-E24 / 01-24集
    for m in re.finditer(r"[Ee](\d{1,4})\s*[-~–至到]\s*[Ee]?(\d{1,4})", s):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b and b - a <= 200:
            out.update(range(a, b + 1))
    for m in re.finditer(r"第\s*(\d{1,4})\s*集\s*[-~–至到]\s*第?\s*(\d{1,4})", s):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b and b - a <= 200:
            out.update(range(a, b + 1))
    # 单集标记：SxxEyy / Exy / 第N集
    for m in re.finditer(r"[Ss]\d{1,2}[Ee](\d{1,4})", s):
        out.add(int(m.group(1)))
    for m in re.finditer(r"(?<![\dEe])(?<!\d)[Ee](\d{1,4})(?!\d)", s):
        out.add(int(m.group(1)))
    for m in re.finditer(r"第\s*(\d{1,4})\s*集", s):
        out.add(int(m.group(1)))
    return out


_FULL_PACK_WORDS = ("complete", "season", "全集", "全季", "全集合集", "全24集", "全集全季")


def is_full_pack_title(title: str, source_episode_count: int) -> bool:
    """标题是否明显是「整包」（拆分模式下要剔除、交由 iyuu 辅种）。

    判据任一命中即为整包：
      - 标题含 Complete / Season / 全集 等整包词；
      - 有季号但完全没有集号（形如 S01，通常是整季）；
      - 标题里出现的集号数量 >= 源种子集数（覆盖了源的全部或更多集）。
    """
    low = str(title or "").lower()
    if any(w in low for w in _FULL_PACK_WORDS):
        return True
    eps = episodes_in_title(title)
    if season_of(title) and not eps:
        return True
    if source_episode_count > 0 and len(eps) >= source_episode_count:
        return True
    return False


def infohash_of(blob: bytes) -> str:
    """从 .torrent 内容算 infohash。优先 torrentool（宿主自带），bencodepy 兜底。"""
    try:
        from torrentool.api import Torrent
        return str(Torrent.from_string(blob).info_hash or "")
    except Exception:
        pass
    try:
        import bencodepy
        meta = bencodepy.decode(blob)
        return hashlib.sha1(bencodepy.encode(meta[b"info"])).hexdigest()
    except Exception:
        return ""


def parse_torrent(blob: bytes) -> Optional[Dict[str, Any]]:
    """解析 .torrent，返回统一结构。优先 torrentool，bencodepy 兜底。

    返回 {
      name, piece_length, pieces(bytes), files:[{path:[str], length:int}],
      total, infohash
    }
    """
    info: Optional[dict] = None
    name = ""
    # 优先 torrentool（MP 容器内自带，能正确读 byte_keys={'pieces'}）
    try:
        from torrentool.api import Torrent
        t = Torrent.from_string(blob)
        raw = t._struct.get("info", {})
        info = raw
        name = getattr(t, "name", "") or ""
    except Exception:
        info = None
    # 兜底 bencodepy
    if info is None:
        try:
            import bencodepy
            meta = bencodepy.decode(blob)
            info_raw = meta[b"info"]
            info = {
                "name": info_raw.get(b"name", b""),
                "piece length": info_raw.get(b"piece length", 0),
                "pieces": info_raw.get(b"pieces", b""),
                "files": info_raw.get(b"files"),
                "length": info_raw.get(b"length", 0),
            }
            nb = info.get("name")
            name = nb.decode("utf-8", "replace") if isinstance(nb, bytes) else str(nb or "")
        except Exception:
            return None

    def dec(x: Any) -> str:
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x or "")

    plRaw = info.get("piece length") or info.get(b"piece length") or 0
    piece_length = int(plRaw)
    pieces = info.get("pieces") or info.get(b"pieces") or b""
    if isinstance(pieces, str):
        pieces = pieces.encode("latin-1")

    files: List[Dict[str, Any]] = []
    filesRaw = info.get("files") or info.get(b"files")
    # multi=True 表示「目录模式」：即便只有一个文件，下载器也会在 download_dir
    # 下先建 info.name 目录，文件再落进去。只有真正没有 files 字段的种子才是
    # 单文件（info.name 就是文件名本身）。
    multi = bool(filesRaw)
    if filesRaw:
        for f in filesRaw:
            if isinstance(f, dict):
                path_raw = f.get(b"path") or f.get("path") or []
                length_raw = f.get(b"length") or f.get("length") or 0
            else:
                continue
            path = [dec(x) for x in (path_raw or [])]
            length = int(length_raw)
            files.append({"path": path, "length": length})
    else:
        length_raw = info.get("length") or info.get(b"length") or 0
        nb = info.get("name") or info.get(b"name") or ""
        files = [{"path": [dec(nb)], "length": int(length_raw)}]

    total = sum(f["length"] for f in files)
    return {
        "name": dec(name) if isinstance(name, bytes) else name,
        "piece_length": piece_length,
        "pieces": pieces,
        "files": files,
        "multi": multi,
        "total": total,
        "infohash": infohash_of(blob),
    }


# ---------------------------------------------------------------- 发现

def discover_units(
    tasks: Sequence[dict],
    min_size: int,
    max_per_task: int,
    log: Callable[[str], None] = lambda m: None,
) -> List[Unit]:
    """从已完成任务列表拆出待匹配单元。

    tasks 每项由 __init__.py 注入，字段：
      downloader, task_name, torrent_path, save_path
    其中 torrent_path 是本地 .torrent 路径，save_path 是任务落地目录。
    """
    units: List[Unit] = []
    for task in tasks:
        dl = str(task.get("downloader") or "")
        task_name = str(task.get("task_name") or "")
        tp = str(task.get("torrent_path") or "")
        save_path = str(task.get("save_path") or "")
        if not tp or not os.path.exists(tp):
            continue
        try:
            with open(tp, "rb") as fh:
                meta = parse_torrent(fh.read())
        except Exception as err:  # noqa: BLE001
            log(f"解析种子失败 {os.path.basename(tp)}: {err}")
            continue
        if not meta or not meta["files"]:
            continue

        # 汇总这个源种子的全部集号：拆分作种只加种它的真子集，
        # 完整整包（== 源）交给 iyuu 等通用辅种插件处理。
        source_eps: Set[int] = set()
        for f in meta["files"]:
            rel_all = os.path.join(*f["path"]) if f["path"] else meta["name"]
            if not _is_video(rel_all) or _is_segment(rel_all):
                continue
            _s, _e, _is_ep = episode_info(rel_all)
            if _is_ep and _e:
                # 用 episodes_in_title 展开区间命名（S01E01-E02 -> {1,2}），
                # 只取 episode_info 的第一个集会漏集、导致真子集判定误杀。
                expanded = episodes_in_title(os.path.basename(rel_all))
                source_eps.update(expanded or {_e})

        current: List[Unit] = []
        for f in meta["files"]:
            rel = os.path.join(*f["path"]) if f["path"] else meta["name"]
            if not _is_video(rel) or _is_segment(rel):
                continue
            size = int(f["length"])
            if size < min_size:
                continue
            # 任务落地路径的三种常见布局：downloadDir 即包目录、downloadDir/包名、
            # downloadDir 的上一级；逐个探测，命中即用。
            probes = [
                os.path.join(save_path, rel),
                os.path.join(save_path, meta["name"], rel),
                os.path.join(os.path.dirname(save_path.rstrip("/")), rel),
                os.path.join(os.path.dirname(save_path.rstrip("/")), meta["name"], rel),
            ]
            full = next((p for p in probes if os.path.exists(p)), "")
            if not full:
                continue
            season, episode, is_ep = episode_info(rel)
            fname = os.path.basename(rel)
            key = hashlib.md5(full.encode("utf-8")).hexdigest()[:16]
            current.append(Unit(
                key=key,
                name=fname,
                source_path=full,
                size=size,
                search_keyword=build_keyword(fname),
                source_task=task_name,
                source_downloader=dl,
                source_torrent=os.path.basename(tp),
                season=season,
                episode=episode,
                is_episode=is_ep,
                source_episodes=set(source_eps),
            ))
            if 0 < max_per_task <= len(current):
                break
        units.extend(current)
        if current:
            log(f"发现 {task_name[:40]}: {len(current)} 个可拆单元")
    # 去重（同一文件可能被多站种子的任务引用）
    seen: set = set()
    unique: List[Unit] = []
    for u in units:
        if u.source_path in seen:
            continue
        seen.add(u.source_path)
        unique.append(u)
    return unique


# ---------------------------------------------------------------- 匹配

def screen_candidates(
    unit: Unit,
    raw: Sequence[dict],
    size_tolerance: float,
    min_similarity: float,
    max_candidates: int,
    split_only: bool = False,
) -> List[Candidate]:
    """对站内搜索原始结果做集号 + 体积 + 名称粗筛。

    raw 每项由 __init__.py 从 MP 搜索结果归一化而来，字段：
      title, size, site, site_id, enclosure, page_url, cookie, ua, referer

    剧集单元的筛法：
      - 候选标题里带集号的，集号必须与本地一致（不同集的文件必然不同）；
      - 不带集号的整包（全集 / Complete），总大小不小于本地单集即可，
        具体的文件定位与内容校验交给 piece 校验；
      - 带集号的单集种子，体积需落在容差内。
    电影单元走纯体积容差。
    """
    out: List[Candidate] = []
    want_season, want_episode = unit.season, unit.episode
    src_eps: Set[int] = set(unit.source_episodes)
    # 拆分作种：源不是多集整包（电影/单集）就没有「更小的块」可加，
    # 完整资源交给 iyuu 等通用辅种插件，这里直接不产生候选。
    if split_only and len(src_eps) < 2:
        return []
    for item in raw:
        enclosure = str(item.get("enclosure") or "")
        if not enclosure:
            continue
        title = str(item.get("title") or "")
        size = int(item.get("size") or 0)
        if size <= 0:
            continue

        # 拆分作种：标题级先剔除整包（Complete/S01 无集号/集号覆盖源全集），
        # 既符合「只加真子集」的语义，也省下整包种子的下载请求（馒头配额有限）。
        if split_only and is_full_pack_title(title, len(src_eps)):
            continue

        c_season, c_episode, c_is_episode = episode_info(title)
        if unit.is_episode and want_episode:
            if c_is_episode:
                if c_episode != want_episode:
                    continue
                if want_season and c_season and c_season != want_season:
                    continue
                # 同一集：不卡体积，压制版本不同也纳入候选，
                # 能否真正复用由 piece 校验判断（这是硬事实，不是猜测）
            else:
                # 整包候选：必须能容纳本地单集，且不能是别的季
                if size < unit.size:
                    continue
                t_season = season_of(title)
                if want_season and t_season and t_season != want_season:
                    continue
        elif not size_close(unit.size, size, size_tolerance):
            continue

        sim = similarity(unit.search_keyword, title)
        if sim < min_similarity:
            continue
        out.append(Candidate(
            title=title,
            size=size,
            site=str(item.get("site") or ""),
            site_id=item.get("site_id"),
            enclosure=enclosure,
            page_url=str(item.get("page_url") or ""),
            cookie=str(item.get("cookie") or ""),
            ua=str(item.get("ua") or ""),
            referer=str(item.get("referer") or ""),
        ))
    # 排序：单集优先 → 版本特征（分辨率/编码/来源）重合多优先 → 体积接近 → 名称相似
    u_tags = release_tags(unit.name)
    out.sort(key=lambda c: (
        0 if episode_info(c.title)[2] else 1,
        -len(u_tags & release_tags(c.title)),
        abs(c.size - unit.size),
        -similarity(unit.search_keyword, c.title),
    ))
    return out[:max_candidates]


def same_episode(title: str, season: int, episode: int) -> bool:
    """标题里的季集是否与目标一致（任一为 0 时只比集）。"""
    t_season, t_episode, is_ep = episode_info(title)
    if not is_ep or not episode:
        return False
    if t_episode != episode:
        return False
    if season and t_season and t_season != season:
        return False
    return True


# ---------------------------------------------------------------- 校验

def _iter_piece_bytes(meta: Dict[str, Any], base_dir: str) -> Any:
    """按种子文件顺序产出连续字节流（piece 是跨文件连续切片的）。"""
    for f in meta["files"]:
        rel = os.path.join(*f["path"]) if f["path"] else meta["name"]
        full = os.path.join(base_dir, rel)
        if not os.path.exists(full):
            continue
        with open(full, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk


def verify_pieces(
    meta: Dict[str, Any],
    base_dir: str,
    pieces_to_check: Optional[int] = None,
    log: Callable[[str], None] = lambda m: None,
) -> Tuple[int, int, str]:
    """逐片校验字节流与种子 pieces 是否一致。

    返回 (matched, checked, reason)。pieces_to_check=None 表示全量校验；
    指定 N 则只校验前 N 片（快速预检，试点用）。
    """
    pieces = meta.get("pieces") or b""
    plen = int(meta.get("piece_length") or 0)
    if not pieces or not plen:
        return 0, 0, "种子无 pieces 信息"
    total = len(pieces) // 20
    if total <= 0:
        return 0, 0, "pieces 长度为 0"
    check = total if pieces_to_check is None else min(total, pieces_to_check)

    buf = bytearray()
    idx = 0
    matched = 0
    for chunk in _iter_piece_bytes(meta, base_dir):
        buf.extend(chunk)
        while len(buf) >= plen and idx < check:
            piece = bytes(buf[:plen])
            del buf[:plen]
            expect = pieces[idx * 20:(idx + 1) * 20]
            if hashlib.sha1(piece).digest() == expect:
                matched += 1
            else:
                return matched, idx + 1, f"第 {idx + 1} 片校验失败"
            idx += 1
        if idx >= check:
            break
    # 最后不足一片
    if idx < check and buf:
        expect = pieces[idx * 20:(idx + 1) * 20]
        if hashlib.sha1(bytes(buf)).digest() == expect:
            matched += 1
            idx += 1
    if idx < check:
        return matched, idx, f"字节流不足，仅校验 {idx}/{check} 片"
    return matched, idx, "OK"


def verify_local_file_in_torrent(
    meta: Dict[str, Any],
    local_file: str,
    pieces_to_check: Optional[int] = None,
    prefer_season: int = 0,
    prefer_episode: int = 0,
) -> Tuple[int, int, str]:
    """校验本地单集文件的内容是否与候选种子描述的 piece 一致。

    与 verify_pieces 的区别：不依赖「种子内文件名 == 本地文件名」，而是
    用候选种子的文件清单，按体积定位本地单集对应的是种子里的哪一个文件，
    再计算该文件在种子连续字节流中的 piece 偏移，只校验完全落在该文件
    内部的 piece（跨文件边界的 piece 因缺少种子里的其它文件而跳过）。

    这是拆包辅种的正确校验口径：内容一致即可，不要求整包其它文件都在本地。

    返回 (matched, checked, reason)。
    """
    pieces = meta.get("pieces") or b""
    plen = int(meta.get("piece_length") or 0)
    files = meta.get("files") or []
    if not pieces or not plen or not files:
        return 0, 0, "种子缺少 pieces / 文件清单"
    local_size = os.path.getsize(local_file)
    if local_size <= 0:
        return 0, 0, "本地文件为空"

    # 定位本地单集在种子文件序列中的位置：
    #   ① 先按季集号定位（同集即可，体积不必相等）——整包与本地压制版本不同时
    #      体积必然不同，但仍是「同一个文件」，是否真一致交给 piece 判断；
    #   ② 集号定位不到，再退回等大体积匹配（无集号的整包或单文件种子）。
    match_index = -1
    if prefer_episode:
        for i, f in enumerate(files):
            rel = "/".join(f["path"]) if f["path"] else str(meta.get("name") or "")
            if same_episode(rel, prefer_season, prefer_episode):
                match_index = i
                break
        if match_index >= 0:
            seed_len = int(files[match_index]["length"])
            if seed_len != local_size:
                return 0, 0, (
                    f"集号命中但体积不同（本地 {local_size // 1048576}MB / "
                    f"种子 {seed_len // 1048576}MB），非同一版本"
                )
    if match_index < 0:
        for i, f in enumerate(files):
            if int(f["length"]) == local_size:
                match_index = i
                break
    if match_index < 0:
        return 0, 0, "种子内未找到对应集的文件"

    # 该文件在种子字节流里的起始偏移
    start = sum(int(f["length"]) for f in files[:match_index])
    file_len = local_size
    total_pieces = len(pieces) // 20
    first_piece = start // plen
    # 最后一个 piece 的结束偏移
    last_piece_end = start + file_len
    last_piece = (last_piece_end - 1) // plen

    # 只校验完全落在该文件内部的 piece：
    #   piece 起点 >= start 且 piece 终点 <= start + file_len
    checkable: List[int] = []
    for pno in range(first_piece, min(last_piece, total_pieces - 1) + 1):
        p_start = pno * plen
        p_end = p_start + plen
        if p_start >= start and p_end <= start + file_len:
            checkable.append(pno)
            if pieces_to_check and len(checkable) >= pieces_to_check:
                break
    if not checkable:
        return 0, 0, "单集太小，无完整可校验 piece"

    matched = 0
    with open(local_file, "rb") as fh:
        for pno in checkable:
            offset_in_file = pno * plen - start
            fh.seek(offset_in_file)
            data = fh.read(plen)
            if len(data) < plen:
                break
            expect = pieces[pno * 20:(pno + 1) * 20]
            if hashlib.sha1(data).digest() == expect:
                matched += 1
            else:
                return matched, matched + 1, f"片号 {pno} 校验失败"
    checked = matched
    if matched != len(checkable):
        return matched, checked, "读取中断"
    return matched, checked, "OK"


def episodes_of_meta(meta: Dict[str, Any]) -> Set[int]:
    """从种子真实文件列表提取集号集合（比标题解析精确）。"""
    out: Set[int] = set()
    for f in meta.get("files") or []:
        rel = os.path.join(*f["path"]) if f.get("path") else str(meta.get("name") or "")
        if not _is_video(rel) or _is_segment(rel):
            continue
        _s, _e, _is_ep = episode_info(rel)
        if _is_ep and _e:
            expanded = episodes_in_title(os.path.basename(rel))
            out.update(expanded or {_e})
    return out


def proper_subset(candidate: Set[int], source: Set[int]) -> bool:
    """candidate 是否为 source 的非空真子集（拆分作种的判定核心）。

    单集 {1} ⊂ {1,2,3}、两集 {1,2} ⊂ {1,2,3} 成立；
    整包 {1,2,3}（相等）、超集、含源外集号、空集都不成立。
    """
    return bool(candidate) and candidate < source


def size_of_meta(meta: Dict[str, Any]) -> int:
    return int(meta.get("total") or 0)


# ---------------------------------------------------------------- 物化

def _ensure_dir(path: str) -> None:
    """建目录并把权限显式设成 755。

    飞牛上的 TrimACL 会让新建目录落到 000，下载器（路由侧以 root 走 NFS）
    会因此进不去目录、校验全部失败，所以每次建完都显式 chmod。
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


def materialize(
    meta: Dict[str, Any],
    src_base: str,
    dst_base: str,
    mode: str,
    log: Callable[[str], None] = lambda m: None,
    target_source: str = "",
    season: int = 0,
    episode: int = 0,
    episode_resolver: Optional[Callable[[str, int, int], str]] = None,
) -> Tuple[List[Operation], List[str]]:
    """按候选种子文件布局，在 dst_base 下生成独立副本。

    mode: hardlink / symlink / copy / auto
    源文件只读。

    target_source 是本地这一集的真实文件：候选种子里的文件名可能与本地不同
    （不同站点重命名、重打包），但它与本地文件字节相同，所以按候选种子要求的
    相对路径落盘、内容直接硬链接本地文件，TR 才能按种子布局校验并做种。

    episode_resolver 用于多集候选（如 {1,2}）：候选种子含多个集，除当前单元这一集
    外，其余集需要按 (季,集)+体积在源目录找本地文件。签名 (rel, length, ...) 由
    调用方注入；这里按 (rel, episode, length) 调用，返回本地绝对路径或空串。

    返回 (写操作列表, 缺失文件列表)。缺失列表非空说明本地凑不齐这个种子，
    加进去 TR 会一直校验失败或去补下载，调用方应放弃加种。
    """
    ops: List[Operation] = []
    created: List[str] = []
    missing: List[str] = []
    # 目录模式（有 files 字段，哪怕只有 1 个文件）：info.name 是目录，文件按 path 落下；
    # 单文件种子：info.name 就是文件名，下载器要求它直接躺在 download_dir 下。
    multi = bool(meta.get("multi"))
    root = os.path.join(dst_base, meta["name"] or "seeding") if multi else dst_base
    _ensure_dir(dst_base)
    if multi and os.path.isfile(root):
        # 上一版把「单文件目录种子」误认成单文件，留下的同名文件会挡住建目录
        try:
            os.unlink(root)
            log(f"清理布局冲突的同名文件: {os.path.basename(root)}")
        except OSError:
            pass
    _ensure_dir(root)
    ops.append(Operation("mkdir", root, undo=f"rmdir {root}"))

    local_size = 0
    if target_source and os.path.exists(target_source):
        local_size = os.path.getsize(target_source)

    for f in meta["files"]:
        if multi:
            rel = os.path.join(*f["path"]) if f["path"] else (meta["name"] or "file")
        else:
            rel = meta["name"] or (f["path"][0] if f["path"] else "file")
        # 目标集：体积与本地单集一致，直接用本地文件（校验已证明字节相同）
        if target_source and local_size and int(f["length"]) == local_size:
            src = target_source
        else:
            src = os.path.join(src_base, rel)
            if not os.path.exists(src):
                alt = os.path.join(src_base, os.path.basename(rel))
                if os.path.exists(alt):
                    src = alt
        # 多集候选兜底：相对路径和文件名都没命中时，按候选文件的集号+体积
        # 在源目录找本地对应集（跨发布组文件名不同也能命中）。
        if (not src or not os.path.exists(src)) and episode_resolver:
            _es, _ee, _eep = episode_info(os.path.basename(rel))
            if _eep and _ee:
                resolved = episode_resolver(rel, _ee, int(f["length"]))
                if resolved and os.path.exists(resolved):
                    src = resolved
        if not src or not os.path.exists(src):
            log(f"物化跳过（源文件不存在）: {rel}")
            missing.append(rel)
            continue
        dst = os.path.join(root, rel)
        _ensure_dir(os.path.dirname(dst))
        if os.path.exists(dst):
            # 同一个候选种子会被同包的多个单元用到，第二次进来副本已存在
            try:
                if os.path.samefile(src, dst):
                    continue
            except OSError:
                pass
            try:
                os.unlink(dst)
            except OSError:
                missing.append(rel)
                continue
        done = False
        if mode in ("hardlink", "auto"):
            try:
                os.link(src, dst)
                ops.append(Operation("hardlink", dst, src, f"unlink {dst}"))
                done = True
            except OSError:
                pass
        if not done and mode in ("symlink", "auto"):
            try:
                os.symlink(src, dst)
                ops.append(Operation("symlink", dst, src, f"unlink {dst}"))
                done = True
            except OSError:
                pass
        if not done:
            shutil.copy2(src, dst)
            try:
                os.chmod(dst, 0o644)
            except OSError:
                pass
            ops.append(Operation("copy", dst, src, f"unlink {dst}"))
        created.append(dst)

    if missing:
        # 本地凑不齐这个种子：清理已建的副本，别留半截目录
        for path in reversed(created):
            try:
                os.unlink(path)
            except OSError:
                pass
        for dirpath, _dirs, _files in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(dst_base):
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        return [], missing
    return ops, missing
