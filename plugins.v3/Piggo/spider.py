# -*- coding: utf-8 -*-
"""猪猪 (piggo.me) 专站 API 爬虫。

站点为自研 SPA，接口走 /v1/web/*，鉴权三层：
1. Bearer JWT（登录后获取，约 30 天）
2. visitorid 头（必须与签发该 token 的会话一致，否则一律 401）
3. 请求级 HMAC 签名（密钥由 /v1/web/auth/sign 下发）

登录使用 SRP-6a（PBKDF2-SHA256 派生，N/g 与站点一致），成功后 token 与
visitorid 配对生效，token 失效时自动重登。

CF 前置做 TLS 指纹 + UA 一致性检测：requests/httpx 的 Python TLS 指纹会被
reset，只有 curl 的指纹能通过，因此所有请求均通过 curl 子进程完成；下载另起
一个 127.0.0.1 本地代理，把 MP 下载器的 GET 转发成对站点的 curl POST。
"""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Optional, Tuple
from urllib.parse import quote, urlparse

from app.log import logger

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# visitorid 由客户端生成并与 token 绑定：一旦登录即固定，换值等于换会话。
_DEFAULT_VISITORID = ""

# 直连被 reset 时可配代理兜底，默认留空走直连。
_PROXY = os.getenv("PIGGO_PROXY", "")

# 签名密钥缓存 10 分钟，过期自动重取。
_SIGN_TTL = 600.0

# 两次自动登录之间的最小间隔，避免异常情况下反复登录触发风控。
_LOGIN_COOLDOWN = 60.0

# SRP-6a 参数（与站点前端一致）
_SRP_N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6"
    "AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
_SRP_N_BYTES = bytes.fromhex(_SRP_N_HEX)
_SRP_N = int(_SRP_N_HEX, 16)
_SRP_N_LEN = len(_SRP_N_BYTES)
_SRP_G = 2
_SRP_ITERS = 210000

# 本地下载代理端口，由 start_dl_server 动态分配后写入；未启动时为 0。
_DL_PORT = 0
_DL_SERVER: Optional[ThreadingHTTPServer] = None

# 站点同一账号多会话会互相踢下线，因此 token 在进程内全局共享，
# 所有 spider 实例复用同一份，避免并发搜索时反复登录。
_TOKEN_LOCK = threading.Lock()
_CACHED_TOKEN = ""
_LAST_LOGIN_TS = 0.0
# 强制重登的最小间隔，防止 401 风暴时反复登录把会话踢来踢去
_FORCE_LOGIN_GAP = 8.0


# --------------------------------------------------------------------- SRP 工具

def _srp_bytes(value: int) -> bytes:
    """整数转固定 256 字节大端（与站点 Cw/rn 一致，左补零）。"""
    return value.to_bytes(_SRP_N_LEN, "big")


def _sha(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _srp_client_proof(username: str, password: str, salt_hex: str, b_hex: str,
                      identity: str) -> Tuple[str, str, bytes]:
    """计算 SRP 的 A 与 M1，返回 (a_hex, m1_hex, expected_m2)。"""
    salt = bytes.fromhex(salt_hex)
    b_bytes = bytes.fromhex(b_hex)
    b_int = int(b_hex, 16)

    k = int.from_bytes(_sha(_SRP_N_BYTES, _srp_bytes(_SRP_G)), "big")
    pwd = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _SRP_ITERS, 32)
    x = int.from_bytes(_sha(salt, _sha((identity + ":").encode(), pwd)), "big")

    a_int = int.from_bytes(os.urandom(32), "big")
    a_bytes = _srp_bytes(pow(_SRP_G, a_int, _SRP_N))
    u = int.from_bytes(_sha(a_bytes, b_bytes), "big")

    base = (b_int - k * pow(_SRP_G, x, _SRP_N)) % _SRP_N
    s_int = pow(base, a_int + u * x, _SRP_N)
    k_bytes = _sha(_srp_bytes(s_int))

    m1 = _sha(_xor(_sha(_SRP_N_BYTES), _sha(_srp_bytes(_SRP_G))),
              _sha(identity.encode()), salt, a_bytes, b_bytes, k_bytes)
    m2 = _sha(a_bytes, m1, k_bytes)
    return a_bytes.hex(), m1.hex(), m2


# --------------------------------------------------------------------- HTTP 层

def _run_curl(method: str, path: str, headers: dict, base: str, timeout: int,
              body: Optional[str] = None) -> Tuple[str, str]:
    """执行 curl 请求，返回 (http_code, text_body)。

    先直连；连不上（000）再走旁路由代理重试一次。猪猪的 CF 前置偶发
    直接 reset 直连，代理兜底能把可用率拉回正常。
    """
    token_tag = secrets.token_hex(4)
    out_file = f"/tmp/piggo_{os.getpid()}_{token_tag}.txt"
    body_file = f"/tmp/piggo_{os.getpid()}_{token_tag}.json"
    if body is not None:
        with open(body_file, "w", encoding="utf-8") as handle:
            handle.write(body)

    def _build(use_proxy: bool) -> list:
        cmd = ["curl", "-4", "-sS", "--retry", "2", "--retry-delay", "1",
               "--retry-all-errors", "-m", str(timeout), "-X", method]
        if use_proxy and _PROXY:
            cmd += ["-x", _PROXY]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        if body is not None:
            cmd += ["--data-binary", f"@{body_file}"]
        cmd += ["-o", out_file, "-w", "%{http_code}", f"{base}{path}"]
        return cmd

    def _exec(cmd: list) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout * 4 + 30).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    # 配了代理就只走代理：直连会被 Cloudflare 拦成 403，403 响应会写进
    # cookie jar，导致代理侧会话失效，表现为取签名 401 反复重登。
    out = _exec(_build(bool(_PROXY)))

    text = ""
    for temp in (out_file, body_file):
        if os.path.exists(temp):
            if temp == out_file:
                try:
                    text = open(temp, encoding="utf-8", errors="replace").read()
                except OSError:
                    text = ""
            try:
                os.remove(temp)
            except OSError:
                pass
    return out, text


def _run_curl_bytes(method: str, path: str, headers: dict, base: str,
                    timeout: int) -> Tuple[str, bytes]:
    """执行 curl 请求，二进制响应落到临时文件，返回 (http_code, bytes)。"""
    temp = f"/tmp/piggo_{os.getpid()}_{secrets.token_hex(4)}.torrent"

    def _build(use_proxy: bool) -> list:
        cmd = ["curl", "-4", "-sS", "--retry", "2", "--retry-delay", "1",
               "--retry-all-errors", "-m", str(timeout), "-X", method]
        if use_proxy and _PROXY:
            cmd += ["-x", _PROXY]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        cmd += ["-o", temp, "-w", "%{http_code}", f"{base}{path}"]
        return cmd

    def _exec(cmd: list) -> str:
        try:
            return subprocess.run(cmd, capture_output=True,
                                  timeout=timeout * 4 + 30).stdout.decode(
                                      errors="replace").strip()
        except Exception:  # noqa: BLE001
            return ""

    out = _exec(_build(bool(_PROXY)))
    content = b""
    if os.path.exists(temp):
        try:
            content = open(temp, "rb").read()
        except OSError:
            content = b""
        try:
            os.remove(temp)
        except OSError:
            pass
    return out.strip(), content


class PiggoSpider:
    """猪猪 SPA API 搜索与下载适配器。"""

    def __init__(self, indexer: dict[str, Any]):
        global _CACHED_TOKEN
        self._indexer = indexer or {}
        self._domain = str(self._indexer.get("domain") or "https://piggo.me/").rstrip("/")
        self._name = self._indexer.get("name") or "猪猪"
        self._token = str(self._indexer.get("token") or self._indexer.get("apikey") or "").strip()
        # 进程内已登录过则直接复用，避免多实例互相踢会话
        if _CACHED_TOKEN:
            self._token = _CACHED_TOKEN
        self._username = str(self._indexer.get("username") or "").strip()
        self._password = str(self._indexer.get("password") or "").strip()
        self._ua = str(self._indexer.get("ua") or "").strip() or _DEFAULT_UA
        self._visitorid = (str(self._indexer.get("visitorid") or "").strip()
                           or _DEFAULT_VISITORID)
        self._cookie = str(self._indexer.get("cookie") or "").strip()
        self._timeout = self._indexer.get("timeout") or 30
        self.error_detail: Optional[str] = None
        self._sign: Optional[str] = None
        self._sign_ts: float = 0.0
        logger.info(
            f"猪猪 spider 初始化：token_len={len(self._token)} "
            f"user={self._username or '-'} visitorid={self._visitorid[:8]}..."
        )

    # ------------------------------------------------------------------ 工具

    def _persist_token(self, token: str) -> None:
        """把新 token 写回 site 表，减少重启后首次请求的 401。"""
        try:
            import psycopg2
            host = os.getenv("DB_POSTGRESQL_HOST", "127.0.0.1")
            port = os.getenv("DB_POSTGRESQL_PORT", "5432")
            dbname = os.getenv("DB_POSTGRESQL_DATABASE", "")
            user = os.getenv("DB_POSTGRESQL_USERNAME", "")
            password = os.getenv("DB_POSTGRESQL_PASSWORD", "")
            if not dbname:
                return
            domain = self._domain.replace("https://", "").replace("http://", "").strip("/")
            conn = psycopg2.connect(host=host, port=port, dbname=dbname,
                                    user=user, password=password,
                                    connect_timeout=5)
            cur = conn.cursor()
            cur.execute("update site set token=%s where domain=%s", (token, domain))
            conn.commit()
            conn.close()
            logger.info(f"{self._name} token 已写回站点表")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"{self._name} token 写回失败：{err}")

    def _current_token(self) -> str:
        """进程内最新的 token 优先，避免多实例拿到被踢掉的旧值。"""
        return _CACHED_TOKEN or self._token

    def _base_headers(self) -> dict:
        headers = {
            "User-Agent": self._ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-requested-with": "XMLHttpRequest",
            "version": "1.0.0",
            "webversion": "1",
            "visitorid": self._visitorid,
            "ts": str(int(time.time())),
            "Origin": self._domain,
            "Referer": f"{self._domain}/",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    # ------------------------------------------------------------------ 登录

    def login(self, force: bool = False) -> bool:
        """SRP-6a 登录，成功后刷新内存 token（进程内全局共享）。

        force=True 表示刚刚收到 401，需要真的重新登录而不是复用缓存。
        """
        global _CACHED_TOKEN, _LAST_LOGIN_TS
        if not self._username or not self._password:
            logger.warn(f"{self._name} 未配置账号密码，无法自动登录")
            return False
        with _TOKEN_LOCK:
            gap = _FORCE_LOGIN_GAP if force else _LOGIN_COOLDOWN
            # 其他实例刚刚登录成功，直接复用同一会话
            if not force and _CACHED_TOKEN and _CACHED_TOKEN != self._token:
                self._token = _CACHED_TOKEN
                self._sign = None
                self._sign_ts = 0.0
                logger.info(f"{self._name} 复用已有会话，token_len={len(self._token)}")
                return True
            if time.time() - _LAST_LOGIN_TS < gap:
                if _CACHED_TOKEN:
                    self._token = _CACHED_TOKEN
                    self._sign = None
                    self._sign_ts = 0.0
                logger.warn(f"{self._name} 登录过于频繁，跳过本次")
                return False
            _LAST_LOGIN_TS = time.time()

            headers = self._base_headers()
            headers["Content-Type"] = "application/json"
            headers.pop("Authorization", None)

            code, text = _run_curl(
                "POST", "/v1/web/auth/srp/challenge", headers, self._domain,
                self._timeout, body=json.dumps({"username": self._username})
            )
            if code != "200":
                logger.warn(f"{self._name} SRP 挑战失败：HTTP {code}")
                return False
            try:
                data = json.loads(text).get("data") or {}
                ticket = data["ticket"]
                identity = str(data.get("i") or self._username)
                salt_hex = data["s"]
                b_hex = data["B"]
            except Exception as err:  # noqa: BLE001
                logger.warn(f"{self._name} SRP 挑战响应异常：{err}")
                return False

            a_hex, m1_hex, expect_m2 = _srp_client_proof(
                self._username, self._password, salt_hex, b_hex, identity
            )
            code, text = _run_curl(
                "POST", "/v1/web/auth/srp/login", headers, self._domain,
                self._timeout,
                body=json.dumps({"ticket": ticket, "a": a_hex, "m1": m1_hex})
            )
            if code != "200":
                logger.warn(f"{self._name} SRP 登录失败：HTTP {code} {text[:120]}")
                return False
            try:
                payload = json.loads(text).get("data") or {}
                token = payload.get("token") or ""
                server_m2 = str(payload.get("m2") or "")
            except Exception as err:  # noqa: BLE001
                logger.warn(f"{self._name} SRP 登录响应异常：{err}")
                return False
            if not token:
                logger.warn(f"{self._name} SRP 登录未返回 token")
                return False
            if server_m2 and server_m2 != expect_m2.hex():
                logger.warn(f"{self._name} SRP 服务端证据校验不一致")
            self._token = token
            _CACHED_TOKEN = token
            self._sign = None
            self._sign_ts = 0.0
            logger.info(f"{self._name} 登录成功，token_len={len(token)}")
        self._persist_token(token)
        return True

    # ------------------------------------------------------------------ 签名

    def _fetch_sign(self, force: bool = False) -> Optional[str]:
        if not force and self._sign and time.time() - self._sign_ts < _SIGN_TTL:
            return self._sign
        headers = self._base_headers()
        token = self._current_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        code, text = _run_curl("GET", "/v1/web/auth/sign", headers,
                               self._domain, self._timeout)
        if code == "200":
            try:
                sign = (json.loads(text).get("data") or {}).get("sign")
                if sign:
                    self._sign = sign
                    self._sign_ts = time.time()
                    return sign
            except ValueError:
                pass
        logger.warn(f"{self._name} 获取签名密钥失败：HTTP {code}")
        return None

    def _signed_headers(self, method: str, path: str) -> dict:
        headers = self._base_headers()
        token = self._current_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        sign = self._fetch_sign()
        if sign:
            nonce = secrets.token_hex(16)
            plain = urlparse(path).path.lstrip("/").lower()
            message = f"{method}\n{plain}\n{headers['ts']}\n{nonce}"
            mac = hmac.new(sign.encode(), message.encode(), hashlib.sha256).hexdigest()
            headers["x-spa-nonce"] = nonce
            headers["x-spa-hmac"] = mac
        return headers

    def _call(self, method: str, path: str) -> Tuple[str, str]:
        """带自动重登的请求。401 时重新登录并重试一次。"""
        code, text = _run_curl(method, path, self._signed_headers(method, path),
                               self._domain, self._timeout)
        if code == "401" and self._username and self._password:
            logger.info(f"{self._name} 返回 401，尝试重新登录")
            if self.login(force=True):
                code, text = _run_curl(method, path,
                                       self._signed_headers(method, path),
                                       self._domain, self._timeout)
        if code == "403" and "notice_unread" in text:
            # 站点有未读公告时会 403 拦住业务接口，标记已读再试一次
            if self._ack_notice(text):
                code, text = _run_curl(method, path,
                                       self._signed_headers(method, path),
                                       self._domain, self._timeout)
        return code, text

    def _ack_notice(self, text: str) -> bool:
        try:
            payload = json.loads(text)
        except ValueError:
            return False
        data = payload.get("data") or payload
        try:
            notice_id = int((data.get("notice_gate") or {}).get("id") or 0)
        except (TypeError, ValueError, AttributeError):
            notice_id = 0
        if notice_id:
            path, method, body = f"/v1/web/notices/{notice_id}/read", "POST", "{}"
        else:
            path, method, body = "/v1/web/notices", "GET", None
        code, _ = _run_curl(method, path, self._signed_headers(method, path),
                            self._domain, self._timeout, body=body)
        logger.info(f"{self._name} 公告 {notice_id or '-'} 标记已读 -> {code}")
        return True

    # ------------------------------------------------------------------ 搜索

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """猪猪固定每页 100 条，支持可靠分页。"""
        return 100

    async def async_search(
        self,
        keyword: str,
        mtype: Optional[Any] = None,
        page: Optional[int] = 0,
        search_type: Optional[str] = None,
        cat: Optional[str] = None,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """异步搜索：把同步 curl 搜索放到线程池执行。"""
        import asyncio
        return await asyncio.to_thread(self.search, keyword, mtype, page, cat)

    def search(
        self,
        keyword: str,
        mtype: Optional[Any] = None,
        page: Optional[int] = 0,
        cat: Optional[str] = None,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        self.error_detail = None
        if not self._current_token() and not self.login():
            self.error_detail = "未登录且自动登录失败"
            return True, []
        page_num = int(page or 0) + 1
        query = f"page={page_num}"
        if keyword:
            query += f"&q={quote(keyword)}"
        path = f"/v1/web/torrents?{query}"
        code, text = self._call("GET", path)
        logger.info(f"猪猪 搜索响应：http={code} body_len={len(text)}")
        if code != "200":
            self.error_detail = f"HTTP {code or '无响应'}"
            return True, []
        try:
            payload = json.loads(text)
        except ValueError:
            self.error_detail = "响应不是有效 JSON"
            return True, []
        if int(payload.get("code", -1)) != 0:
            self.error_detail = str(payload.get("message") or "接口返回失败")
            logger.warn(f"猪猪 接口返回失败：{self.error_detail}")
            return True, []
        items = (payload.get("data") or {}).get("items") or []
        logger.info(f"猪猪 搜索命中 {len(items)} 条")
        return False, self._parse(items)

    def _parse(self, items: List[dict[str, Any]]) -> List[dict[str, Any]]:
        torrents: List[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            tid = it.get("id")
            if tid is None:
                continue
            try:
                size = int(it.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                seeders = int(it.get("seeders") or 0)
            except (TypeError, ValueError):
                seeders = 0
            try:
                leechers = int(it.get("leechers") or 0)
            except (TypeError, ValueError):
                leechers = 0
            dvol = 1.0
            uvol = 1.0
            promo = it.get("promotion")
            if isinstance(promo, dict):
                if promo.get("down") == 0:
                    dvol = 0.0
                if promo.get("up") == 0:
                    uvol = 0.0
            labels = [
                t.get("name")
                for t in (it.get("tags") or [])
                if isinstance(t, dict) and t.get("name")
            ]
            resolution = ""
            for tax in (it.get("taxonomies") or []):
                if isinstance(tax, dict) and tax.get("field") == "standard":
                    resolution = tax.get("name") or ""
                    break
            title = str(it.get("name") or "").strip()
            if resolution and resolution.lower() not in title.lower():
                title = f"{title} {resolution}".strip()
            torrents.append({
                "title": title,
                "description": str(it.get("small_descr") or ""),
                "enclosure": self.get_download_url(tid),
                "pubdate": it.get("added_at") or "",
                "size": size,
                "seeders": seeders,
                "peers": leechers,
                "grabs": int(it.get("times_completed") or 0),
                "downloadvolumefactor": dvol,
                "uploadvolumefactor": uvol,
                "page_url": f"{self._domain}/torrents/{tid}",
                "category": str(it.get("category_name") or ""),
                "labels": labels,
            })
        return torrents

    # ------------------------------------------------------------------ 下载

    def download(self, torrent_id: Any) -> bytes:
        """用 curl POST 下载种子，返回种子字节。"""
        path = f"/v1/web/torrents/{torrent_id}/download"
        code, content = _run_curl_bytes("POST", path,
                                        self._signed_headers("POST", path),
                                        self._domain, self._timeout)
        if code == "401" and self._username and self._password and self.login(force=True):
            code, content = _run_curl_bytes("POST", path,
                                            self._signed_headers("POST", path),
                                            self._domain, self._timeout)
        if code == "200" and content:
            return content
        logger.warn(f"{self._name} 下载种子 {torrent_id} 失败：HTTP {code}")
        return b""

    def get_download_url(self, torrent_id: Any) -> str:
        """返回本地下载代理地址，MP 下载器 GET 它即可拿到种子。"""
        global _DL_PORT
        if _DL_PORT:
            return f"http://127.0.0.1:{_DL_PORT}/dl/{torrent_id}"
        return f"{self._domain}/torrents/{torrent_id}"


class _DLHandler(BaseHTTPRequestHandler):
    spider: Optional[PiggoSpider] = None

    def do_GET(self):
        if not self.spider or not self.path.startswith("/dl/"):
            self.send_error(404)
            return
        torrent_id = self.path[len("/dl/"):]
        content = self.spider.download(torrent_id)
        if not content:
            self.send_error(502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-bittorrent")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


def start_dl_server(spider: PiggoSpider) -> int:
    """启动本地下载代理，返回监听端口。"""
    global _DL_PORT, _DL_SERVER
    if _DL_SERVER is not None:
        _DLHandler.spider = spider
        return _DL_PORT
    _DLHandler.spider = spider
    _DL_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _DLHandler)
    _DL_PORT = _DL_SERVER.server_address[1]
    threading.Thread(target=_DL_SERVER.serve_forever, daemon=True).start()
    logger.info(f"猪猪下载代理已启动，端口 {_DL_PORT}")
    return _DL_PORT
