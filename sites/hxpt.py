"""好学（hxpt.org）签到模块 —— FluxTorrent JSON API。

站点 2026-11 起整站改为 Next.js / FluxTorrent，老 NexusPHP 的
/attendance.php、/signin.php、/plugin_sign-in.php 全部 404，插件自带规则必然失败。

新版签到接口（2026-11-08 实测）：
  GET  /api/v1/attendance          查状态 -> {code:0, data:{checked_today, streak, makeup_cards, recent}}
  POST /api/v1/attendance/checkin  执行签到，必须带 x-csrf-token（值与 Cookie 里的 flux_csrf 相同）
未带 CSRF 时返回 {code:2003, message:"无权限"}，带错路径返回 {code:1004, message:"接口不存在"}。
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple

BASE = "https://www.hxpt.org"


def _csrf_from_cookie(cookie: str) -> str:
    for chunk in str(cookie or "").split(";"):
        chunk = chunk.strip()
        if chunk.startswith("flux_csrf="):
            return chunk.split("=", 1)[1].strip()
    return ""


def _json_of(response: Any) -> Mapping[str, Any]:
    if response is None:
        return {}
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, Mapping) else {}


class HxptSignin:
    site_url = "hxpt.org"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        cookie = str(site.get("cookie") or "").strip()
        if not cookie:
            return False, "站点没有 Cookie，无法签到"
        if "flux_token" not in cookie:
            return False, "Cookie 缺少 flux_token，登录态不完整，请重新获取站点 Cookie"

        headers = {
            "Origin": BASE,
            "Referer": BASE + "/",
            "Accept": "application/json",
        }

        # 1. 先查今天是否已经签到
        response = ctx.get(f"{BASE}/api/v1/attendance", headers=headers)
        if response is None:
            return False, "读取签到状态失败（网络错误）"
        if response.status_code != 200:
            return False, f"读取签到状态失败（HTTP {response.status_code}）"
        body = _json_of(response)
        code = body.get("code")
        if code == 2001:
            return False, "登录态已失效（Cookie 过期），请更新站点 Cookie"
        if code not in (0, None):
            return False, f"读取签到状态被拒（code={code}：{body.get('message') or ''}）"
        data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
        if data.get("checked_today"):
            streak = data.get("streak")
            return True, f"今日已签到（连签 {streak} 天）" if streak else "今日已签到"

        # 2. 提交签到
        token = _csrf_from_cookie(cookie)
        if not token:
            return False, "Cookie 里没有 flux_csrf，无法通过 CSRF 校验"
        posted = ctx.post(
            f"{BASE}/api/v1/attendance/checkin",
            json_body={},
            headers={**headers, "x-csrf-token": token, "Content-Type": "application/json"},
        )
        if posted is None:
            return False, "提交签到失败（网络错误）"
        result = _json_of(posted)
        rcode = result.get("code")
        message = str(result.get("message") or "")
        if "已经签到" in message:
            return True, "今日已签到"
        if rcode == 0:
            payload = result.get("data") if isinstance(result.get("data"), Mapping) else {}
            reward = payload.get("reward") or payload.get("magic")
            streak = payload.get("streak")
            if reward:
                return True, f"签到成功，获得 {reward} 魔力值" + (f"，连签 {streak} 天" if streak else "")
            return True, "签到成功"
        if rcode == 2001:
            return False, "登录态已失效（Cookie 过期），请更新站点 Cookie"
        if rcode == 2003:
            return False, "CSRF 校验未通过（flux_csrf 可能已刷新，请更新站点 Cookie）"
        return False, f"签到失败（HTTP {posted.status_code}，code={rcode}：{message}）"
