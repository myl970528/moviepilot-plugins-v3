# -*- coding: utf-8 -*-
"""猪猪站客户端与种子匹配核心逻辑。

不依赖 MoviePilot，可脱离容器单独跑（见文件末尾 __main__），
方便上线前验证 SRP 登录、索引、bencode 解析是否正常。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_BASE = "https://piggo.me"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 猪猪站 SRP-6a 参数：512 字节模数、g=2、PBKDF2-SHA256 210000 轮。
# 站点改版时这三个值必须同步重新扒 JS，否则登录会 422。
_N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6"
    "AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
_N = int(_N_HEX, 16)
_N_BYTES = bytes.fromhex(_N_HEX)
_N_LEN = len(_N_BYTES)
_G = 2
_PBKDF2_ROUNDS = 210000

# 签名密钥缓存 10 分钟
_SIGN_TTL = 600


def _sha(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _rn(value: int) -> bytes:
    """大数转定长字节。长度不足必须左补零，否则 SRP 校验必然对不上。"""
    return value.to_bytes(_N_LEN, "big")


class PiggoError(Exception):
    """站点交互失败。"""


class PiggoClient:
    """猪猪站最小客户端：SRP 登录 + 签名请求 + 种子下载。"""

    def __init__(
        self,
        base: str = DEFAULT_BASE,
        username: str = "",
        password: str = "",
        visitorid: str = "",
        proxy: str = "",
        timeout: int = 20,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.visitorid = visitorid or ""
        self.proxy = (proxy or "").strip()
        self.timeout = int(timeout or 20)
        self._log = logger or (lambda msg: None)
        self._token = ""
        self._sign = ""
        self._sign_ts = 0.0
        self.last_code = ""
        self.last_body = ""
        self._jar = os.path.join(
            tempfile.gettempdir(), f"piggo_{os.getpid()}_{id(self)}.jar"
        )

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        try:
            if os.path.exists(self._jar):
                os.remove(self._jar)
        except OSError:
            pass

    def _headers(self, auth: bool = True) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN",
            "x-requested-with": "XMLHttpRequest",
            "version": "1.0.0",
            "webversion": "1",
            "visitorid": self.visitorid,
            "ts": str(int(time.time())),
            "origin": self.base,
            "referer": self.base + "/",
        }
        if auth and self._token:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    def _curl(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: Optional[dict] = None,
        binary: bool = False,
        timeout: Optional[int] = None,
    ) -> Tuple[str, Any]:
        """直连优先，返回 000 时用代理兜底重试一次。"""
        limit = timeout or self.timeout
        handle, temp = tempfile.mkstemp()
        os.close(handle)

        def build(use_proxy: bool) -> List[str]:
            cmd = [
                "curl", "-4", "-sS", "-m", str(limit),
                "--retry", "2", "--retry-delay", "1", "--retry-all-errors",
                "-X", method, "-o", temp, "-w", "%{http_code}",
                "-c", self._jar, "-b", self._jar,
            ]
            if use_proxy and self.proxy:
                cmd += ["-x", self.proxy]
            for key, value in headers.items():
                cmd += ["-H", f"{key}: {value}"]
            if body is not None:
                cmd += ["-H", "Content-Type: application/json",
                        "--data-binary", json.dumps(body)]
            cmd.append(self.base + path)
            return cmd

        def run(cmd: List[str]) -> str:
            try:
                return subprocess.run(
                    cmd, capture_output=True, text=True, timeout=limit + 35
                ).stdout.strip()
            except Exception:  # noqa: BLE001
                return ""

        # 配置了代理就只走代理：直连会被 Cloudflare 挡成 403，
        # 而 403 响应写进 cookie jar 后会让代理侧的 SRP 会话失效
        # （表现是「登录会话已过期，请重试」）。
        code = run(build(bool(self.proxy)))

        try:
            with open(temp, "rb") as handle:
                raw = handle.read()
        except OSError:
            raw = b""
        try:
            os.remove(temp)
        except OSError:
            pass
        return code, raw if binary else raw.decode("utf-8", "replace")

    # ------------------------------------------------------------------ 登录
    def login(self, retry: int = 3) -> bool:
        last = ""
        for attempt in range(retry):
            try:
                return self._login_once()
            except PiggoError as err:
                last = str(err)
                if attempt < retry - 1:
                    self._log(f"登录失败，{4 * (attempt + 1)} 秒后重试：{err}")
                    time.sleep(4 * (attempt + 1))
        raise PiggoError(last or "登录失败")

    def _login_once(self) -> bool:
        code, raw = self._curl(
            "POST", "/v1/web/auth/srp/challenge",
            self._headers(False), {"username": self.username},
        )
        if code != "200":
            raise PiggoError(f"SRP 挑战失败 {code} {str(raw)[:120]}")
        data = json.loads(raw)["data"]
        ticket = data["ticket"]
        identity = data.get("i") or self.username
        salt = bytes.fromhex(data["s"])
        b_bytes = bytes.fromhex(data["B"])
        b_int = int(data["B"], 16)

        k = int.from_bytes(_sha(_N_BYTES, _rn(_G)), "big")
        bits = hashlib.pbkdf2_hmac(
            "sha256", self.password.encode(), salt, _PBKDF2_ROUNDS, 32
        )
        x = int.from_bytes(
            _sha(salt, _sha((identity + ":").encode(), bits)), "big"
        )
        a_int = int.from_bytes(os.urandom(32), "big")
        a_pub = _rn(pow(_G, a_int, _N))
        u = int.from_bytes(_sha(a_pub, b_bytes), "big")
        shared = pow((b_int - k * pow(_G, x, _N)) % _N, a_int + u * x, _N)
        key = _sha(_rn(shared))
        proof = _sha(
            _xor(_sha(_N_BYTES), _sha(_rn(_G))),
            _sha(identity.encode()), salt, a_pub, b_bytes, key,
        )

        code, raw = self._curl(
            "POST", "/v1/web/auth/srp/login", self._headers(False),
            {"ticket": ticket, "a": a_pub.hex(), "m1": proof.hex()},
        )
        if code != "200":
            raise PiggoError(f"SRP 登录失败 {code} {str(raw)[:120]}")
        self._token = json.loads(raw)["data"]["token"]
        self._sign = ""
        self._sign_ts = 0.0
        return True

    def _ensure_sign(self) -> str:
        if self._sign and time.time() - self._sign_ts < _SIGN_TTL:
            return self._sign
        for attempt in range(3):
            code, raw = self._curl("GET", "/v1/web/auth/sign", self._headers())
            if code == "200":
                self._sign = json.loads(raw)["data"]["sign"]
                self._sign_ts = time.time()
                return self._sign
            if code == "401":
                self.login()
            else:
                time.sleep(2)
            self._log(f"取签名失败 {code}，第 {attempt + 1} 次")
        return ""

    def _signed(self, method: str, path: str) -> Optional[Dict[str, str]]:
        sign = self._ensure_sign()
        if not sign:
            return None
        headers = self._headers()
        nonce = secrets.token_hex(16)
        plain = path.split("?")[0].lstrip("/").lower()
        message = f"{method}\n{plain}\n{headers['ts']}\n{nonce}"
        headers["x-spa-nonce"] = nonce
        headers["x-spa-hmac"] = hmac.new(
            sign.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return headers

    # ------------------------------------------------------------------ 接口
    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        retry: int = 2,
    ) -> Optional[str]:
        for attempt in range(retry + 1):
            headers = self._signed(method, path)
            if not headers:
                time.sleep(2)
                continue
            code, raw = self._curl(method, path, headers, body=body)
            self.last_code = code
            self.last_body = raw if isinstance(raw, str) else ""
            if code == "200":
                return raw
            if code == "401" and attempt < retry:
                self._log("请求 401，重新登录")
                self.login()
                continue
            if code == "403" and attempt < retry and "notice_unread" in (self.last_body or ""):
                # 站点有未读公告时会拿 403 拦掉业务接口，标记已读才放行
                self._ack_notice()
                time.sleep(2)
                continue
            if code in ("", "000") and attempt < retry:
                time.sleep(3)
                continue
            self._log(f"请求失败 {code} {path} {str(raw)[:100]}")
            return None
        return None

    def api(self, path: str, retry: int = 2) -> Optional[str]:
        return self.request("GET", path, retry=retry)

    def _ack_notice(self) -> None:
        """把拦住接口的公告标记已读。

        站点公告未读时会用 403 notice_unread 挡掉列表、日历等业务接口，
        只有 POST /v1/web/notices/{id}/read 之后才放行。公告正文与我们
        无关，纯粹是道门。
        """
        notice_id = 0
        try:
            payload = json.loads(self.last_body or "{}")
            data = payload.get("data") or payload
            notice_id = int((data.get("notice_gate") or {}).get("id") or 0)
        except (ValueError, TypeError, AttributeError):
            notice_id = 0
        if notice_id:
            path, method, body = f"/v1/web/notices/{notice_id}/read", "POST", {}
        else:
            path, method, body = "/v1/web/notices", "GET", None
        headers = self._signed(method, path)
        if not headers:
            return
        code, _ = self._curl(method, path, headers, body=body)
        self._log(f"猪猪：公告 {notice_id or '-'} 标记已读 -> {code}")

    # ------------------------------------------------------------------ 签到
    def calendar(self) -> Optional[dict]:
        """拉签到日历。站点把签到做成万年历，按 cell 标记已签 / 可补签。"""
        raw = self.api("/v1/web/calendar")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if payload.get("code") not in (0, None):
            return None
        return payload.get("data") or {}

    def attendance(self) -> Optional[dict]:
        """取今日签到状态。"""
        data = self.calendar()
        if not data:
            return None
        today = str(data.get("today") or "")
        cell = next(
            (x for x in data.get("cells") or [] if x.get("ymd") == today), {}
        )
        info = data.get("attendance")
        if not isinstance(info, dict):
            info = {}
        return {
            "today": today,
            "checked": bool(cell.get("checked")),
            "status": str(info.get("status") or ""),
            "points": info.get("points"),
            "total": info.get("total"),
            "streak": info.get("streak"),
            "detail": info,
        }

    def fetch_captcha(self) -> Optional[dict]:
        """取一张图片验证码，返回 {key, image(bytes)}。"""
        raw = self.api("/v1/web/captcha")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        data = payload.get("data") or {}
        image = str(data.get("image") or "")
        if not image:
            return None
        if image.startswith("data:"):
            image = image.split(",", 1)[1]
        try:
            blob = base64.b64decode(image)
        except Exception:  # noqa: BLE001
            return None
        return {"key": str(data.get("key") or ""), "image": blob}

    def sign_in(
        self,
        solver: Optional[Callable[[bytes], str]] = None,
        max_try: int = 6,
    ) -> Tuple[bool, str]:
        """每日签到。已签到直接返回成功，不重复提交。

        站点风控命中时要求图片验证码（422 need_captcha）。实测 ddddocr 对
        这种 120x40 小图的全对率只有四成（容易漏字、Q/O、5/S 混淆），
        所以靠「取图 - 识别 - 重提」多轮硬试，15 次累计成功率 99.9%。
        """
        data = self.calendar()
        if not data:
            return False, "获取签到日历失败"
        today = str(data.get("today") or "")
        cells = data.get("cells") or []
        cell = next((x for x in cells if x.get("ymd") == today), {})
        info = data.get("attendance") if isinstance(data.get("attendance"), dict) else {}
        if cell.get("checked") or info.get("status") == "checked":
            bits = []
            if info.get("streak"):
                bits.append(f"连签 {info['streak']} 天")
            if info.get("total"):
                bits.append(f"累计 {info['total']} 天")
            return True, "今日已签到" + (f"（{'，'.join(bits)}）" if bits else "")

        path = "/v1/web/calendar/attendance"
        raw = self.request("POST", path, {"ymd": today}, retry=1)
        parsed = self._sign_result(raw, today) if raw else None
        if parsed is not None:
            return parsed
        if not self._captcha_required():
            return False, f"签到失败 {self.last_code} {self.last_body[:80]}"
        if solver is None:
            return False, "站点要求人机验证，请在网页日历页手动签到"

        tried = 0
        for _ in range(max_try):
            captcha = self.fetch_captcha()
            if not captcha:
                time.sleep(1)
                continue
            try:
                raw_text = str(solver(captcha["image"]) or "").upper()
            except Exception as err:  # noqa: BLE001
                self._log(f"验证码识别异常 {err}")
                continue
            # 站点只收 A-Z0-9 且统一大写，中文和标点必须滤掉
            text = "".join(ch for ch in raw_text if ch.isascii() and ch.isalnum())
            if len(text) < 3:
                continue
            tried += 1
            raw = self.request(
                "POST", path,
                {"ymd": today, "captcha_key": captcha["key"], "captcha": text},
                retry=0,
            )
            parsed = self._sign_result(raw, today) if raw else None
            if parsed is not None:
                self._log(f"猪猪 验证码 {text} 通过（第 {tried} 次）")
                return parsed
            if not self._captcha_required():
                return False, f"签到失败 {self.last_code} {self.last_body[:80]}"
            self._log(f"猪猪 验证码 {text} 未通过（第 {tried} 次），换一张")
        return False, f"验证码 {tried} 次未通过，请人工签到"

    def _captcha_required(self) -> bool:
        body = self.last_body or ""
        return "need_captcha" in body or "人机验证" in body or "验证码" in body

    def _sign_result(self, raw: str, today: str) -> Optional[Tuple[bool, str]]:
        try:
            payload = json.loads(raw)
        except ValueError:
            return False, f"签到返回异常 {raw[:80]}"
        if payload.get("code") not in (0, None):
            # 站点对重复签到不一定给 code 0，可能用提示语表达，统一认成功
            message = str(payload.get("msg") or payload.get("message") or "")
            if "已签到" in message or "重复签到" in message:
                return True, message
            return None
        result = payload.get("data") or {}
        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
        hit = next(
            (x for x in result.get("cells") or [] if x.get("ymd") == today), {}
        )
        if hit.get("checked") or result.get("cells"):
            gain = detail.get("points")
            extra = f"，+{gain}" if gain else ""
            return True, f"签到成功{extra}"
        return True, "签到已提交"

    def download(self, torrent_id: int, retry: int = 2) -> Optional[bytes]:
        path = f"/v1/web/torrents/{torrent_id}/download"
        for attempt in range(retry + 1):
            headers = self._signed("POST", path)
            if not headers:
                time.sleep(2)
                continue
            code, data = self._curl("POST", path, headers, binary=True)
            if code == "200" and data[:1] == b"d":
                return data
            if code in ("401", "", "000") and attempt < retry:
                if code == "401":
                    self.login()
                else:
                    time.sleep(2)
                continue
            self._log(f"下载种子失败 {code} tid={torrent_id} {data[:80]}")
            return None
        return None

    def list_page(self, page: int) -> Optional[dict]:
        raw = self.api(f"/v1/web/torrents?page={page}")
        if not raw:
            return None
        try:
            return json.loads(raw).get("data") or {}
        except (ValueError, AttributeError):
            return None

    def build_index(
        self,
        pages: int = 0,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> Dict[int, dict]:
        """拉全站列表建索引，pages=0 表示按站点总页数全量。"""
        first = self.list_page(1)
        if not first:
            raise PiggoError("首页列表获取失败")
        total_pages = int(first.get("pages") or 0)
        if pages and pages > 0:
            total_pages = min(total_pages, int(pages))
        if total_pages <= 0:
            raise PiggoError("站点返回的总页数为 0")

        items: Dict[int, dict] = {}
        for page in range(1, total_pages + 1):
            payload = first if page == 1 else self.list_page(page)
            if not payload:
                self._log(f"第 {page} 页跳过")
                continue
            for item in payload.get("items") or []:
                try:
                    tid = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                items[tid] = {
                    "id": tid,
                    "name": item.get("name") or "",
                    "size": int(item.get("size") or 0),
                    "seeders": int(item.get("seeders") or 0),
                    "leechers": int(item.get("leechers") or 0),
                    "can_download": bool(item.get("can_download")),
                }
            if on_progress and (page % 10 == 0 or page == total_pages):
                on_progress(page, total_pages, len(items))
            time.sleep(0.2)
        return items


# ---------------------------------------------------------------------- bencode
def bdecode(data: bytes, pos: int = 0):
    char = data[pos:pos + 1]
    if char == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if char == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            value, pos = bdecode(data, pos)
            out.append(value)
        return out, pos + 1
    if char == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = bdecode(data, pos)
            value, pos = bdecode(data, pos)
            out[key] = value
        return out, pos + 1
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    return data[colon + 1:colon + 1 + length], colon + 1 + length


def torrent_infohash(blob: bytes) -> str:
    """取 .torrent 顶层 info 字典的 sha1，即种子 infohash（小写十六进制）。

    重新 bdecode + 编码会破坏键顺序，算出来的 hash 就不是真值了，所以
    这里只在原始字节上定位 info 段的起止偏移再切片做 sha1。
    """
    if not blob or blob[:1] != b"d":
        return ""
    pos = 1
    try:
        while pos < len(blob) and blob[pos:pos + 1] != b"e":
            key, pos = bdecode(blob, pos)
            if key == b"info":
                start = pos
                _, pos = bdecode(blob, pos)
                return hashlib.sha1(blob[start:pos]).hexdigest()
            _, pos = bdecode(blob, pos)
    except (ValueError, IndexError, KeyError):
        return ""
    return ""


def norm_path(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def torrent_files(blob: bytes) -> dict:
    """解析 .torrent，返回 {name, files[(相对路径, 长度)], total}。"""
    meta, _ = bdecode(blob)
    info = meta[b"info"]
    root = info[b"name"].decode("utf-8", "replace")
    files: List[Tuple[str, int]] = []
    if b"files" in info:
        for entry in info[b"files"]:
            rel = "/".join(x.decode("utf-8", "replace") for x in entry[b"path"])
            files.append((root + "/" + rel, int(entry[b"length"])))
    else:
        files.append((root, int(info[b"length"])))
    return {
        "name": root,
        "files": files,
        "total": sum(length for _, length in files),
    }


def file_signature(files: Any) -> Tuple[Tuple[str, int], ...]:
    """把文件清单归一成可比较的签名，兼容 tuple 和 transmission 的文件对象。"""
    out: List[Tuple[str, int]] = []
    for entry in files or []:
        if isinstance(entry, dict):
            size = entry.get("length") or entry.get("size") or 0
            out.append((norm_path(entry.get("name")), int(size)))
        elif hasattr(entry, "name"):
            size = getattr(entry, "size", None)
            if size is None:
                size = getattr(entry, "length", 0)
            out.append((norm_path(entry.name), int(size or 0)))
        else:
            out.append((norm_path(entry[0]), int(entry[1] or 0)))
    return tuple(sorted(out))


def match_by_size(
    done_torrents: List[dict],
    index: Dict[int, dict],
) -> List[Tuple[dict, List[dict]]]:
    """按总字节数精确相等反查候选，返回 [(本地种子, 候选列表)]。

    跨站 infohash 必然不同（announce 里带用户 token），只能按体积粗筛，
    再用文件清单精筛，不能按 hash 匹配。
    """
    by_size: Dict[int, List[dict]] = {}
    for item in index.values():
        by_size.setdefault(int(item.get("size") or 0), []).append(item)
    hits: List[Tuple[dict, List[dict]]] = []
    for torrent in done_torrents:
        candidates = by_size.get(int(torrent.get("size") or 0))
        if candidates:
            hits.append((torrent, sorted(candidates, key=lambda x: -x["seeders"])))
    return hits


if __name__ == "__main__":  # pragma: no cover
    user = os.getenv("PIGGO_USER", "")
    pwd = os.getenv("PIGGO_PASS", "")
    visitor = os.getenv("PIGGO_VISITOR", "")
    proxy = os.getenv("PIGGO_PROXY", "")
    client = PiggoClient(
        username=user, password=pwd, visitorid=visitor, proxy=proxy,
        logger=lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True),
    )
    try:
        client.login()
        print("登录成功，token 长度", len(client._token))
        index = client.build_index(
            pages=int(sys.argv[1]) if len(sys.argv) > 1 else 3,
            on_progress=lambda page, total, count: print(
                f"  {page}/{total} 累计 {count}", flush=True
            ),
        )
        print("索引条数", len(index))
        sample = next(iter(index.values()))
        print("样例", json.dumps(sample, ensure_ascii=False))
    finally:
        client.close()
