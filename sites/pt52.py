"""52PT 签到模块（Turnstile + Referer 校验）。

站点 2026-11 起把签到页换成 /bakatestfinal.php：
- 直接访问会被「安全拦截」页挡掉，必须带本站 Referer；
- 页面是 Cloudflare Turnstile（sitekey 0x4AAAAAAFl5BKvpPSvgq3jb，action=checkin），
  通过回调后才点亮 #my-submit-btn，提交表单里带 sign_token + sign_submit=1。

插件自带的 52pt 规则还停在老的 /bakatest.php 答题签到（现在 404），所以这里用
Browserless BrowserQL 走真实浏览器：带 Referer 打开页面 → solve Turnstile → 点提交。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

CONFIG_PATH = Path("/config/ptsignin_52pt.json")
DEFAULT_ADDRESS = "https://production-sfo.browserless.io"
# 发布版不带默认 token：请在 /config/ptsignin_52pt.json 里配 {"address": "...", "token": "..."}
DEFAULT_TOKEN = ""

STATE_JS = """(() => {
  const btn = document.querySelector('#my-submit-btn');
  const form = document.querySelector('form');
  const token = document.querySelector("input[name='cf-turnstile-response']");
  const text = (document.body && document.body.innerText) || '';
  return JSON.stringify({
    title: document.title || '',
    url: location.href,
    hasForm: !!form,
    hasButton: !!btn,
    disabled: btn ? !!btn.disabled : null,
    tokenLen: token ? String(token.value || '').length : 0,
    blocked: text.indexOf('安全拦截') >= 0,
    text: text.slice(0, 1200)
  });
})()"""

CLICK_JS = """(() => {
  const btn = document.querySelector('#my-submit-btn');
  if (!btn) return JSON.stringify({clicked: false, reason: 'no-button'});
  if (btn.disabled) return JSON.stringify({clicked: false, reason: 'disabled'});
  btn.click();
  return JSON.stringify({clicked: true, reason: 'clicked'});
})()"""

CHECK_QUERY = """mutation Pt52Check($cookies:[CookieInput!]! $state:String! $referer:String!) {
  cookies(cookies:$cookies){cookies{name}}
  page:goto(url:"https://52pt.site/bakatestfinal.php",waitUntil:domContentLoaded,referer:$referer){status}
  settle:waitForTimeout(time:2000){time}
  state:evaluate(content:$state){value}
}"""

SIGN_QUERY = """mutation Pt52Sign($cookies:[CookieInput!]! $state:String! $click:String! $referer:String!) {
  cookies(cookies:$cookies){cookies{name}}
  page:goto(url:"https://52pt.site/bakatestfinal.php",waitUntil:domContentLoaded,referer:$referer){status}
  settle:waitForTimeout(time:2000){time}
  solve(timeout:60000){found solved time token}
  afterSolve:waitForTimeout(time:2500){time}
  click:evaluate(content:$click){value}
  afterClick:waitForTimeout(time:6000){time}
  state:evaluate(content:$state){value}
}"""


def _load_config() -> Tuple[str, str]:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_ADDRESS, DEFAULT_TOKEN
    if not isinstance(raw, Mapping):
        return DEFAULT_ADDRESS, DEFAULT_TOKEN
    return (str(raw.get("address") or DEFAULT_ADDRESS).strip(),
            str(raw.get("token") or DEFAULT_TOKEN).strip())


def _endpoint(address: str, token: str) -> str:
    address = address.strip().rstrip("/")
    if address.endswith("/bql"):
        return address + (f"?token={token}" if token else "")
    if address.endswith("/stealth"):
        return address + "/bql" + (f"?token={token}" if token else "")
    return address + "/stealth/bql" + (f"?token={token}" if token else "")


def _cookies(cookie: str, base: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for chunk in str(cookie or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            items.append({"name": name, "value": value.strip(), "domain": "52pt.site",
                          "path": "/", "secure": True,
                          "url": "https://52pt.site/bakatestfinal.php"})
    return items


def _bql(address: str, token: str, query: str, variables: Mapping[str, Any],
         timeout: int = 150) -> Dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(_endpoint(address, token), data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return {"error": f"Browserless 鉴权失败（HTTP {error.code}）"}
        if error.code == 429:
            return {"error": "Browserless 额度不足或请求过于频繁（HTTP 429）"}
        return {"error": f"Browserless HTTP {error.code}"}
    except Exception as error:
        return {"error": f"Browserless 请求失败：{type(error).__name__}"}
    data = body.get("data")
    return data if isinstance(data, Mapping) else {"error": "Browserless 未返回数据"}


def _state(data: Mapping[str, Any], key: str) -> Dict[str, Any]:
    item = data.get(key) if isinstance(data, Mapping) else None
    value = item.get("value") if isinstance(item, Mapping) else None
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


class Pt52Signin:
    site_url = "52pt.site"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        base = str(site.get("url") or "https://52pt.site/").strip()
        referer = base if base.endswith("/") else base + "/"
        cookie = str(site.get("cookie") or "").strip()
        if not cookie:
            return False, "站点没有 Cookie，无法签到"
        address, token = _load_config()
        cookies = _cookies(cookie, base)
        if not cookies:
            return False, "站点 Cookie 格式无效"

        checked = _bql(address, token, CHECK_QUERY,
                       {"cookies": cookies, "state": STATE_JS, "referer": referer}, timeout=90)
        if checked.get("error"):
            return False, str(checked["error"])
        pre = _state(checked, "state")
        if pre.get("blocked"):
            return False, "被站点安全拦截（Referer 校验未通过）"
        if not pre.get("title"):
            return False, "打开 52PT 签到页失败"
        if not pre.get("hasForm") and not pre.get("hasButton"):
            if "签到处" not in str(pre.get("title") or ""):
                return False, f"52PT 签到页状态异常（{pre.get('title') or '无标题'}）"
            if "签到成功" in str(pre.get("text") or ""):
                return True, self._bonus(pre.get("text") or "") or "今日已签到"
            return True, "今日已签到"
        if not pre.get("hasButton"):
            return False, "签到页没有提交按钮，页面结构可能又有变化"

        signed = _bql(address, token, SIGN_QUERY,
                      {"cookies": cookies, "state": STATE_JS, "click": CLICK_JS,
                       "referer": referer}, timeout=180)
        if signed.get("error"):
            return False, str(signed["error"])
        solve = signed.get("solve") if isinstance(signed.get("solve"), Mapping) else {}
        click = _state(signed, "click")
        post = _state(signed, "state")
        text = str(post.get("text") or "")
        if "签到成功" in text:
            return True, self._bonus(text) or "签到成功"
        if not solve.get("solved"):
            return False, "Turnstile 未通过（Browserless solve 未拿到 token）"
        if not click.get("clicked"):
            reason = str(click.get("reason") or "")
            if reason == "disabled":
                return False, "Turnstile 已解，但签到按钮仍不可用"
            return False, "签到页没有可点击的提交按钮"
        if not post.get("hasForm") and not post.get("hasButton"):
            return True, "已提交签到，页面表单已消失"
        return False, "提交后页面没有出现签到成功提示"

    @staticmethod
    def _bonus(text: str) -> str:
        match = re.search(r"签到成功[，,]?\s*获得\s*([\d,]+)\s*魔力值", str(text or ""))
        return f"签到成功，获得 {match.group(1)} 魔力值" if match else ""
