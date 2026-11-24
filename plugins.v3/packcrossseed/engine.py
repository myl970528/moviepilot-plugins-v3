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
# 辅助文件：不属于正片内容的小文件（说明、字幕、样图等）。
# 站点种子常把它们打包进去，而本地拆出来的通常只有视频文件。
# 这类文件缺失不该判「内容不符」，也不该阻止加种——下载器可以补，或只补这几个文件。
AUXILIARY_EXT = {
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt",
    ".txt", ".md", ".url", ".lnk", ".sfv", ".md5",
}


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


def is_auxiliary(name: str) -> bool:
    """是否辅助文件（说明/字幕/样图等）。辅助文件缺失不算内容不符。"""
    return Path(str(name)).suffix.lower() in AUXILIARY_EXT


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


# ---------------------------------------------------------------- bencode / Merkle
# 自己实现 bencode 解析与 v2 Merkle，避免依赖宿主可能缺失的第三方库，
# 也让 padding / zero-length / file tree 这些细节可控。


def bdecode(data: bytes, pos: int = 0) -> Tuple[Any, int]:
    """最小 bencode 解析器，返回 (值, 新位置)。bytes 保持 bytes，不转字符串。"""
    if pos >= len(data):
        raise ValueError("bencode 越界")
    ch = data[pos:pos + 1]
    if ch == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if ch == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            value, pos = bdecode(data, pos)
            out.append(value)
        return out, pos + 1
    if ch == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = bdecode(data, pos)
            value, pos = bdecode(data, pos)
            out[key] = value
        return out, pos + 1
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start:start + length], start + length


def info_span(blob: bytes) -> Tuple[int, int]:
    """定位顶层 info 字典在原始字节里的起止偏移（算 infohash 必须用原始切片）。"""
    if not blob or blob[:1] != b"d":
        raise ValueError("不是 bencode 字典")
    pos = 1
    while pos < len(blob) and blob[pos:pos + 1] != b"e":
        key, pos = bdecode(blob, pos)
        if key == b"info":
            start = pos
            _, pos = bdecode(blob, pos)
            return start, pos
        _, pos = bdecode(blob, pos)
    raise ValueError("缺少 info 段")


V2_BLOCK_SIZE = 16 * 1024
_ZERO_LEAF = bytes(32)


def piece_layer_height(piece_length: int) -> int:
    """v2 piece 由若干 16KiB 块组成，返回该 piece 的 Merkle 高度。"""
    if piece_length < V2_BLOCK_SIZE or piece_length % V2_BLOCK_SIZE:
        raise ValueError("piece length 必须是 16KiB 的整数倍")
    blocks = piece_length // V2_BLOCK_SIZE
    if blocks & (blocks - 1):
        raise ValueError("piece length 的块数必须是 2 的幂")
    return blocks.bit_length() - 1


def zero_hash(height: int) -> bytes:
    value = _ZERO_LEAF
    for _ in range(height):
        value = hashlib.sha256(value + value).digest()
    return value


def merkle_root(hashes: Sequence[bytes], pad_hash: bytes = _ZERO_LEAF) -> bytes:
    """把若干 32 字节 hash 归并成 Merkle 根，不足补 pad_hash 到 2 的幂。"""
    if not hashes:
        raise ValueError("Merkle 树至少需要一个 hash")
    count = 1 << (len(hashes) - 1).bit_length()
    level = list(hashes) + [pad_hash] * (count - len(hashes))
    while len(level) > 1:
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


def infohash_of(blob: bytes) -> str:
    """算 v1 infohash：对原始 info 段字节做 sha1。

    必须用原始切片，不能用「解析后再编码」——重新编码会改变键顺序，
    算出来的 hash 就不是真值了（下载器按 infohash 认种子，错一个字节都不认）。
    """
    try:
        start, end = info_span(blob)
        return hashlib.sha1(blob[start:end]).hexdigest()
    except Exception:
        pass
    # 兜底：老路径
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
    """解析 .torrent，支持 v1 / v2 / hybrid。返回统一结构。

    返回 {
      name, piece_length, pieces(bytes, 仅 v1/hybrid),
      files:[{path:[str], length:int, padding:bool, zero_length:bool}],
      multi, total, infohash, kind(v1/v2/hybrid), v2_roots: [[bytes...]]
    }
    v2 时 pieces 为空，改为按文件在 v2_roots 里保存每个文件的 piece-layer 哈希；
    padding 标记用于校验时喂零字节，zero_length 表示该文件长度为 0。
    """
    try:
        meta, _ = bdecode(blob)
    except Exception:
        return None
    if not isinstance(meta, dict) or b"info" not in meta:
        return None
    info = meta[b"info"]
    if not isinstance(info, dict):
        return None

    def dec(x: Any) -> str:
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x or "")

    name = dec(info.get(b"name") or b"")
    piece_length = int(info.get(b"piece length") or 0)
    pieces = info.get(b"pieces") or b""
    if isinstance(pieces, str):
        pieces = pieces.encode("latin-1")
    meta_version = info.get(b"meta version")
    has_v2 = (meta_version == 2) or (b"file tree" in info)
    has_v1 = bool(pieces)

    files: List[Dict[str, Any]] = []
    v2_roots: List[List[bytes]] = []
    if has_v2 and b"file tree" in info:
        # v2：walk file tree，叶节点是 {length, pieces root?, attr?}
        def walk(node: Any, prefix: List[str]) -> None:
            if not isinstance(node, dict):
                return
            for key, child in node.items():
                if not isinstance(child, dict):
                    continue
                seg = dec(key) if isinstance(key, bytes) else str(key)
                if b"" in child:
                    leaf = child[b""]
                    if isinstance(leaf, dict):
                        length = int(leaf.get(b"length") or 0)
                        attr = leaf.get(b"attr") or b""
                        padding = isinstance(attr, bytes) and b"p" in attr
                        files.append({
                            "path": prefix + [seg],
                            "length": length,
                            "padding": padding,
                            "zero_length": length == 0,
                        })
                        layer = leaf.get(b"pieces root")
                        if isinstance(layer, bytes) and len(layer) == 32:
                            v2_roots.append([layer])
                        else:
                            v2_roots.append([])
                    continue
                walk(child, prefix + [seg])

        walk(info[b"file tree"], [])
        # 顶层 pieces root 若存在，是整棵树的根（单文件或多文件共享）；
        # 这里按文件粒度保存，逐文件比对 piece-layer。
        kind = "hybrid" if has_v1 else "v2"
        if kind == "hybrid":
            # hybrid 同时带 v1 的 files/单文件布局，优先用 v1 结构做校验
            files = _parse_v1_files(info, dec)
    else:
        files = _parse_v1_files(info, dec)
        kind = "v1"

    total = sum(int(f["length"]) for f in files)
    return {
        "name": name,
        "piece_length": piece_length,
        "pieces": pieces if kind != "v2" else b"",
        "files": files,
        "multi": bool(info.get(b"files")),
        "total": total,
        "infohash": infohash_of(blob),
        "kind": kind,
        "v2_roots": v2_roots,
    }


def _parse_v1_files(info: dict, dec: Callable[[Any], str]) -> List[Dict[str, Any]]:
    """按 v1 结构解析文件列表（单文件/多文件），带 padding/zero-length 标记。"""
    files: List[Dict[str, Any]] = []
    files_raw = info.get(b"files")
    if files_raw:
        for f in files_raw:
            if not isinstance(f, dict):
                continue
            path = [dec(x) for x in (f.get(b"path") or [])]
            length = int(f.get(b"length") or 0)
            attr = f.get(b"attr") or b""
            padding = isinstance(attr, bytes) and b"p" in attr
            files.append({
                "path": path,
                "length": length,
                "padding": padding,
                "zero_length": length == 0,
            })
    else:
        length = int(info.get(b"length") or 0)
        name = dec(info.get(b"name") or b"")
        files = [{"path": [name], "length": length, "padding": False,
                  "zero_length": length == 0}]
    return files


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
    # 排序：单集候选优先（拆包场景最可能命中）→ 加权评分从高到低。
    # 评分维度不可比较时不计入分母，避免信息少的候选被莫名压低。
    out.sort(key=lambda c: (
        0 if episode_info(c.title)[2] else 1,
        -score_candidate(unit, c.title, c.size),
    ))
    return out[:max_candidates]


def score_candidate(unit: "Unit", title: str, size: int) -> float:
    """给一个候选打分（0~100）。只用于排序，通过与否仍由 piece 校验决定。

    维度与权重：
      集号匹配 30 / 体积接近 30 / 发布属性重合 25 / 名称相似 15
    每维只有在「可比较」时才计入分母，避免信息缺失把总分压低。
    """
    earned = 0.0
    maximum = 0.0

    # 集号匹配
    c_season, c_episode, c_is_episode = episode_info(title)
    want_ep = unit.episode if unit.is_episode else 0
    if want_ep:
        maximum += 30
        if c_is_episode and c_episode == want_ep:
            same_season = not (unit.season and c_season and c_season != unit.season)
            earned += 30 if same_season else 20
        elif not c_is_episode:
            # 整包：体积能容纳这一集才给部分分
            if size >= unit.size:
                earned += 15

    # 体积接近（相对差异越小分越高）
    if unit.size > 0 and size > 0:
        maximum += 30
        denom = max(unit.size, size)
        diff = abs(unit.size - size) / denom if denom else 1.0
        # 差异 0 -> 满分；差异 >= 20% -> 0 分
        ratio = max(0.0, 1.0 - diff / 0.20)
        earned += 30 * ratio

    # 发布属性重合（分辨率/编码/来源等标签）
    u_tags = release_tags(unit.name)
    c_tags = release_tags(title)
    if u_tags and c_tags:
        maximum += 25
        union = u_tags | c_tags
        if union:
            earned += 25 * (len(u_tags & c_tags) / len(union))

    # 名称相似
    sim = similarity(unit.search_keyword, title)
    if sim > 0:
        maximum += 15
        earned += 15 * sim

    if maximum <= 0:
        return 0.0
    return round(earned * 100 / maximum, 3)


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
    """校验本地单集文件的内容是否与候选种子一致（兼容包装，返回 (matched, checked, reason)）。

    实际走 span 流校验内核：把候选种子的全部文件拼成逻辑字节流，
    本地能提供的文件映射为可读 span，缺失文件占位标 unavailable，
    这样一个「跨越本地文件与缺失文件」的 piece 会被判为未知，
    而「跨越两个都在本地的文件」的 piece 则能被正确校验（旧版直接跳过）。
    """
    local_map = _map_local_targets(meta, local_file, prefer_season, prefer_episode)
    verdict = verify_torrent_content(meta, local_map, pieces_to_check=pieces_to_check)
    if verdict.verified == 0 and verdict.mismatch == 0:
        return 0, 0, verdict.reason
    checked = verdict.verified + verdict.mismatch
    if verdict.mismatch:
        return verdict.verified, verdict.verified + 1, verdict.reason
    return verdict.verified, checked, verdict.reason if verdict.level != "full" else "OK"


# ---------------------------------------------------------------- 内容校验内核
# 把 torrent 的文件列表建成一条逻辑字节流（span 序列），
# 按 piece 边界顺序消费。padding 喂零字节、缺失文件标 unavailable 但不破坏偏移，
# 因此跨文件 piece（含跨 padding）都能被正确判定。读取前后各拍一次文件快照
# （dev/inode/size/mtime），中途被改写即判失败，避免「校验时通过、做种时错位」。


@dataclass
class _FileSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass
class FileSpan:
    """种子内一个文件在逻辑字节流中的一段。"""
    torrent_path: str
    length: int
    local_path: Optional[str] = None
    padding: bool = False
    zero_length: bool = False
    snapshot: Optional[_FileSnapshot] = None


@dataclass
class ContentVerdict:
    """内容校验结论：带证据等级，区分「不符」「缺失」「通过」。"""
    level: str = "blocked"      # full / sampled / client_check / blocked
    verified: int = 0           # 校验通过的 piece 数
    mismatch: int = 0           # 内容不符的 piece 数
    unavailable: int = 0        # 因缺文件无法判定的 piece 数
    checked: int = 0            # 实际比对过的 piece 数（verified+mismatch）
    reason: str = ""
    covered_files: List[str] = field(default_factory=list)   # 本地能覆盖的种子文件
    missing_files: List[str] = field(default_factory=list)   # 本地缺的种子文件


def _snapshot_file(path: str) -> _FileSnapshot:
    st = os.stat(path, follow_symlinks=False)
    return _FileSnapshot(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _snapshot_same(a: Optional[_FileSnapshot], b: Optional[_FileSnapshot]) -> bool:
    if a is None or b is None:
        return a is b
    return (a.device, a.inode, a.size) == (b.device, b.inode, b.size)


def _hash_file_range(hasher: Any, fd: int, offset: int, length: int,
                     chunk: int = 4 * 1024 * 1024) -> None:
    """把文件 [offset, offset+length) 的字节喂给 hasher（不返回 digest）。"""
    os.lseek(fd, offset, os.SEEK_SET)
    remaining = length
    while remaining > 0:
        data = os.read(fd, min(remaining, chunk))
        if not data:
            raise OSError("读取期间文件提前结束")
        hasher.update(data)
        remaining -= len(data)


def _hash_zeros(hasher: Any, length: int, chunk: int = 4 * 1024 * 1024) -> None:
    """把 length 个零字节喂给 hasher。"""
    zero = bytes(min(length, chunk))
    remaining = length
    while remaining > 0:
        take = min(remaining, len(zero))
        hasher.update(zero[:take])
        remaining -= take


def _map_local_targets(
    meta: Dict[str, Any],
    local_file: str,
    prefer_season: int = 0,
    prefer_episode: int = 0,
) -> Dict[str, str]:
    """把种子里的文件映射到本地可用文件：先按季集号，再按等大体积。

    只映射「本地确实存在且体积与种子声明一致」的文件；体积不同说明是不同版本，
    不做映射（校验时该 span 记为不可用，不会误判成内容不符）。
    """
    files = meta.get("files") or []
    out: Dict[str, str] = {}
    if not files or not local_file or not os.path.exists(local_file):
        return out
    try:
        local_size = os.path.getsize(local_file)
    except OSError:
        return out
    target_idx = -1
    if prefer_episode:
        for i, f in enumerate(files):
            rel = "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
            if same_episode(rel, prefer_season, prefer_episode):
                target_idx = i
                break
    if target_idx < 0:
        for i, f in enumerate(files):
            if int(f.get("length") or 0) == local_size:
                target_idx = i
                break
    if target_idx < 0:
        return out
    if int(files[target_idx].get("length") or 0) != local_size:
        # 集号命中了但体积不同：不同版本，不能映射
        return out
    rel_key = "/".join(files[target_idx]["path"]) if files[target_idx].get("path") else str(meta.get("name") or "")
    out[rel_key] = local_file
    return out


def _build_spans(meta: Dict[str, Any], local_map: Dict[str, str]) -> List[FileSpan]:
    """按种子文件顺序建 span 序列，缺失文件也占位（保证偏移不错位）。"""
    spans: List[FileSpan] = []
    for f in meta.get("files") or []:
        rel = "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
        length = int(f.get("length") or 0)
        if f.get("padding"):
            spans.append(FileSpan(rel, length, None, padding=True))
            continue
        if length == 0 or f.get("zero_length"):
            spans.append(FileSpan(rel, 0, None, zero_length=True))
            continue
        local = local_map.get(rel)
        snap = None
        if local and os.path.exists(local):
            try:
                snap = _snapshot_file(local)
                if snap.size != length:
                    # 体积不符视为不可用，避免把不同版本算成 mismatch
                    local, snap = None, None
            except OSError:
                local, snap = None, None
        spans.append(FileSpan(rel, length, local, snapshot=snap))
    return spans


def verify_torrent_content(
    meta: Dict[str, Any],
    local_map: Dict[str, str],
    pieces_to_check: Optional[int] = None,
    log: Callable[[str], None] = lambda m: None,
) -> ContentVerdict:
    """按 span 流校验候选种子与本地文件的逐片一致性。

    local_map: {种子内相对路径: 本地绝对路径}，只放确实存在的文件。
    返回带证据等级的 ContentVerdict。
    """
    kind = str(meta.get("kind") or "v1")
    if kind == "v2":
        return _verify_v2(meta, local_map)
    return _verify_v1_stream(meta, local_map, pieces_to_check, log)


def _verify_v1_stream(
    meta: Dict[str, Any],
    local_map: Dict[str, str],
    pieces_to_check: Optional[int],
    log: Callable[[str], None],
) -> ContentVerdict:
    pieces = meta.get("pieces") or b""
    plen = int(meta.get("piece_length") or 0)
    files = meta.get("files") or []
    if not pieces or not plen or not files:
        return ContentVerdict(level="blocked", reason="种子缺少 pieces / 文件清单")
    total_pieces = len(pieces) // 20
    if total_pieces <= 0:
        return ContentVerdict(level="blocked", reason="pieces 为空")

    spans = _build_spans(meta, local_map)
    total_length = sum(s.length for s in spans)
    if total_length <= 0:
        return ContentVerdict(level="blocked", reason="文件总长度为 0")
    # 每个 span 在逻辑流中的起始偏移
    offsets: List[int] = []
    acc = 0
    for s in spans:
        offsets.append(acc)
        acc += s.length

    covered = [s.torrent_path for s in spans if s.local_path]
    missing = [s.torrent_path for s in spans
               if not s.local_path and not s.padding and not s.zero_length]
    if not covered:
        return ContentVerdict(
            level="blocked", reason="本地没有能对应上的文件",
            missing_files=missing,
        )

    # 把与本地 span 有交集的 piece 分成「全覆盖(可验)」与「跨到缺失文件(未知)」两类。
    # 抽样只在可验集合里取，否则首末片常因邻居缺失而白占名额。
    verifiable: List[int] = []
    span_for_piece: Dict[int, bool] = {}
    for idx, span in enumerate(spans):
        if not span.local_path:
            continue
        s_start = offsets[idx]
        s_end = s_start + span.length
        first = s_start // plen
        last = min((s_end - 1) // plen, total_pieces - 1)
        for pno in range(first, last + 1):
            span_for_piece[pno] = True
    # 判断每个候选 piece 是否全部字节来自本地可读 span 或 padding
    def piece_fully_local(pno: int) -> bool:
        p_start = pno * plen
        p_end = min(p_start + plen, total_length)
        pos = p_start
        sidx = 0
        for i, off in enumerate(offsets):
            if off + spans[i].length > p_start:
                sidx = i
                break
        while pos < p_end and sidx < len(spans):
            span = spans[sidx]
            s_off = offsets[sidx]
            in_span = pos - s_off
            take = min(p_end - pos, span.length - in_span)
            if take <= 0:
                sidx += 1
                continue
            if not span.padding and span.local_path is None:
                return False
            pos += take
            sidx += 1
        return pos >= p_end

    for pno in sorted(span_for_piece):
        if piece_fully_local(pno):
            verifiable.append(pno)
    if not verifiable:
        return ContentVerdict(
            level="client_check",
            reason="本地文件对应的 piece 都跨越了缺失文件，无法独立判定",
            missing_files=missing, covered_files=covered,
        )

    # 抽样：取可验集合的首片、末片与均匀间隔
    if pieces_to_check and 0 < pieces_to_check < len(verifiable):
        n = len(verifiable)
        if pieces_to_check == 1:
            chosen = [verifiable[0]]
        else:
            step = (n - 1) / (pieces_to_check - 1)
            chosen = sorted({verifiable[round(i * step)] for i in range(pieces_to_check)})
    else:
        chosen = verifiable

    verified = mismatch = unavailable = 0
    first_bad = ""
    bad_piece = -1
    # 快照：只对真正会被读到的文件拍
    read_spans = {s.local_path for s in spans if s.local_path}
    pre_snap = {}
    for p in read_spans:
        try:
            pre_snap[p] = _snapshot_file(p)
        except OSError:
            pre_snap[p] = None

    open_fds: Dict[str, int] = {}
    try:
        for pno in chosen:
            p_start = pno * plen
            p_end = min(p_start + plen, total_length)
            need = p_end - p_start
            hasher = hashlib.sha1()
            span_idx = 0
            # 定位首个可能与 piece 相交的 span（线性扫描偏移表，span 数通常很小）
            for i, off in enumerate(offsets):
                if off + spans[i].length > p_start:
                    span_idx = i
                    break
            pos = p_start
            ok = True
            avail = True
            while pos < p_end and span_idx < len(spans):
                span = spans[span_idx]
                s_off = offsets[span_idx]
                in_span = pos - s_off
                take = min(p_end - pos, span.length - in_span)
                if take <= 0:
                    span_idx += 1
                    continue
                if span.padding:
                    _hash_zeros(hasher, take)
                elif span.local_path is None:
                    avail = False
                    break
                else:
                    fd = open_fds.get(span.local_path)
                    try:
                        if fd is None:
                            fd = os.open(span.local_path, os.O_RDONLY)
                            open_fds[span.local_path] = fd
                        _hash_file_range(hasher, fd, in_span, take)
                    except OSError:
                        avail = False
                        break
                pos += take
                span_idx += 1
            if not avail or pos < p_end:
                unavailable += 1
                continue
            expect = pieces[pno * 20:(pno + 1) * 20]
            if hasher.digest() == expect:
                verified += 1
            else:
                mismatch += 1
                if not first_bad:
                    first_bad = f"片号 {pno} 内容不符"
                    bad_piece = pno
    finally:
        for fd in open_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass

    # 读完复核快照，防止校验期间源文件被改写
    for p, snap in pre_snap.items():
        try:
            now = _snapshot_file(p)
        except OSError:
            now = None
        if not _snapshot_same(snap, now):
            return ContentVerdict(
                level="blocked", reason="校验期间源文件发生变化，结果不可信",
                verified=verified, mismatch=mismatch, unavailable=unavailable,
                covered_files=covered, missing_files=missing,
            )

    checked = verified + mismatch
    if mismatch:
        return ContentVerdict(
            level="blocked", reason=first_bad or "内容不符",
            verified=verified, mismatch=mismatch, unavailable=unavailable,
            checked=checked, covered_files=covered, missing_files=missing,
        )
    if verified == 0:
        return ContentVerdict(
            level="client_check",
            reason="本地与候选的交集 piece 全部无法判定（相邻文件缺失）",
            verified=0, mismatch=0, unavailable=unavailable, checked=0,
            covered_files=covered, missing_files=missing,
        )
    # 抽样 vs 全量：只有「本地能覆盖的所有 piece 都验过」才算 full
    sampled = bool(pieces_to_check and pieces_to_check > 0
                   and pieces_to_check < len(verifiable))
    if unavailable and not sampled:
        # 有产物片段无法判定（相邻文件不在本地），但已验部分全部一致
        return ContentVerdict(
            level="client_check",
            reason=f"通过 {verified} 片，另有 {unavailable} 片因相邻文件缺失无法判定",
            verified=verified, mismatch=0, unavailable=unavailable, checked=checked,
            covered_files=covered, missing_files=missing,
        )
    level = "sampled" if sampled else "full"
    return ContentVerdict(
        level=level, reason="OK",
        verified=verified, mismatch=0, unavailable=unavailable, checked=checked,
        covered_files=covered, missing_files=missing,
    )


def _verify_v2(
    meta: Dict[str, Any],
    local_map: Dict[str, str],
) -> ContentVerdict:
    """v2：按文件粒度算 piece-layer 的 Merkle 根并与种子声明比对。

    v2 的校验单位是「文件」，没有跨文件 piece，比 v1 简单。
    只校验本地有映射的文件；缺失文件记为 missing，不影响已映射文件的判定。
    """
    plen = int(meta.get("piece_length") or 0)
    files = meta.get("files") or []
    roots = meta.get("v2_roots") or []
    if not plen or not files:
        return ContentVerdict(level="blocked", reason="v2 种子缺少 piece length / 文件清单")
    try:
        height = piece_layer_height(plen)
    except ValueError:
        return ContentVerdict(level="blocked", reason="v2 piece length 非法")

    covered, missing = [], []
    verified = mismatch = 0
    first_bad = ""
    for i, f in enumerate(files):
        rel = "/".join(f["path"]) if f.get("path") else str(meta.get("name") or "")
        if f.get("padding") or int(f.get("length") or 0) == 0:
            continue
        local = local_map.get(rel)
        if not local or not os.path.exists(local):
            missing.append(rel)
            continue
        covered.append(rel)
        if i >= len(roots) or not roots[i]:
            missing.append(rel)
            continue
        expect = roots[i][0]
        try:
            actual = _v2_file_root(local, plen, height)
        except OSError:
            missing.append(rel)
            continue
        if actual == expect:
            verified += 1
        else:
            mismatch += 1
            if not first_bad:
                first_bad = f"{os.path.basename(rel)} 内容不符"

    if mismatch:
        return ContentVerdict(level="blocked", reason=first_bad,
                              verified=verified, mismatch=mismatch,
                              checked=verified + mismatch,
                              covered_files=covered, missing_files=missing)
    if verified == 0:
        return ContentVerdict(level="blocked", reason="没有可校验的 v2 文件",
                              covered_files=covered, missing_files=missing)
    return ContentVerdict(level="full", reason="OK",
                          verified=verified, mismatch=0, checked=verified,
                          covered_files=covered, missing_files=missing)


def _v2_file_root(path: str, piece_length: int, height: int) -> bytes:
    """计算单个文件在 v2 下的 piece-layer Merkle 根。"""
    st = _snapshot_file(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        piece_hashes: List[bytes] = []
        offset = 0
        while offset < st.size:
            take = min(piece_length, st.size - offset)
            piece_hashes.append(_merkle_of_blocks(fd, offset, take, height))
            offset += take
        if not piece_hashes:
            return _ZERO_LEAF
        if len(piece_hashes) == 1:
            return piece_hashes[0]
        return merkle_root(piece_hashes, pad_hash=zero_hash(height))
    finally:
        os.close(fd)


def _merkle_of_blocks(fd: int, offset: int, length: int, height: int) -> bytes:
    """把一个 piece 内的数据按 16KiB 切块做 Merkle，得到该 piece 的 hash。"""
    block_hashes: List[bytes] = []
    os.lseek(fd, offset, os.SEEK_SET)
    remaining = length
    while remaining > 0:
        take = min(V2_BLOCK_SIZE, remaining)
        data = os.read(fd, take)
        if not data:
            raise OSError("读取期间文件提前结束")
        block_hashes.append(hashlib.sha256(data).digest())
        remaining -= len(data)
    if not block_hashes:
        return _ZERO_LEAF
    if len(block_hashes) == 1 and height == 0:
        return block_hashes[0]
    total = 1 << height
    level = list(block_hashes) + [_ZERO_LEAF] * (total - len(block_hashes))
    while len(level) > 1:
        level = [hashlib.sha256(level[i] + level[i + 1]).digest()
                 for i in range(0, len(level), 2)]
    return level[0]


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

def _verify_placement_identity(action: str, src: str, dst: str) -> str:
    """校验落位副本与源文件的身份关系，返回空串表示通过。

    - hardlink：必须与源共用同一 inode（同 inode 才是真硬链，否则占额外空间）
    - copy：必须与源不同 inode（共用 inode 意味着其实是硬链，下载器写入会破坏源）
    - symlink：目标应是指向源的符号链接
    """
    try:
        s_stat = os.stat(src, follow_symlinks=False)
        d_stat = os.lstat(dst)
    except OSError as err:
        return f"无法读取状态 {err}"
    if action == "symlink":
        if not os.path.islink(dst):
            return "期望符号链接但不是"
        return ""
    same = (s_stat.st_dev == d_stat.st_dev) and (s_stat.st_ino == d_stat.st_ino)
    if action == "hardlink" and not same:
        return "硬链落位后 inode 与源不同（可能被降级成复制）"
    if action == "copy" and same:
        return "复制落位后与源共用 inode，下载器写入会破坏源文件"
    return ""


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
            if is_auxiliary(rel):
                # 辅助文件（nfo/字幕/样图）本地没有很正常：不阻止加种，
                # 下载器会按需补齐，或用户可只补这几个文件。只记一条日志。
                log(f"辅助文件本地缺失，不阻止加种: {os.path.basename(rel)}")
                continue
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
        action = ""
        if mode in ("hardlink", "auto"):
            try:
                os.link(src, dst)
                action = "hardlink"
                done = True
            except OSError:
                pass
        if not done and mode in ("symlink", "auto"):
            try:
                os.symlink(src, dst)
                action = "symlink"
                done = True
            except OSError:
                pass
        if not done:
            shutil.copy2(src, dst)
            try:
                os.chmod(dst, 0o644)
            except OSError:
                pass
            action = "copy"
            done = True
        # 落位后验证身份：硬链必须与源共用 inode；复制必须是独立 inode。
        # 若复制模式却共用 inode，说明实际做成了硬链——下载器一旦写入会连带
        # 破坏源文件（源文件本应只读），必须拦下。
        verify_err = _verify_placement_identity(action, src, dst)
        if verify_err:
            log(f"落位身份校验失败 {rel}: {verify_err}")
            try:
                os.unlink(dst)
            except OSError:
                pass
            missing.append(rel)
            continue
        ops.append(Operation(action, dst, src, f"unlink {dst}"))
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
