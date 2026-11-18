# -*- coding: utf-8 -*-
"""栖影 HDHaven 每日签到。

站点是 Next.js 自研站，登录接口 POST /api/auth/login 要求携带 Cloudflare
Turnstile 令牌，且会话与出口 IP、设备指纹绑定。机房 IP 环境下 Turnstile 一律
卡在「正在验证」，从别的 IP 复用 Cookie 会被判定异地并直接吊销会话。

唯一稳定的做法是让浏览器、登录、签到同处一个会话：用 Browserless 的 BrowserQL
在它自己的浏览器里解 Turnstile、填表登录，再在同页面内调用签到接口。整条链路
不落地 Cookie，也就不存在会话绑定问题。

BrowserQL 一次 mutation 完成的动作：
  goto /login → solve(type: cloudflare) → 填账号密码 → 点登录 → 等跳转
  → fetch /api/auth/me → fetch POST /api/account/checkin
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.log import logger
from app.plugins import _PluginBase

import requests

_DEFAULT_BROWSERLESS_URL = "https://production-sfo.browserless.io"
_DEFAULT_CRON = "20 8 * * *"

# 站点固定的 Turnstile sitekey，登录页由 JS 动态注入，不能从 HTML 里取
_SITEKEY = "0x4AAAAAAE6M3XfI3JzwDd99"
_LOGIN_URL = "https://hdhaven.com/login"
_HOME_URL = "https://hdhaven.com/"


class HdhSignIn(_PluginBase):
    # 插件名称
    plugin_name = "栖影签到"
    # 插件描述
    plugin_desc = "栖影 HDHaven 每日自动签到，用 Browserless 解 Turnstile 后在同一会话内签到。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "hermes"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "hdhsignin_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 2

    # ---- 配置
    _enabled: bool = False
    _notify: bool = True
    _username: str = ""
    _password: str = ""
    _browserless_url: str = _DEFAULT_BROWSERLESS_URL
    _browserless_token: str = ""
    _cron: str = _DEFAULT_CRON
    _onlyonce: bool = False

    _HISTORY_KEY = "history"
    _HISTORY_LIMIT = 30

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[dict] = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._username = str(config.get("username") or "").strip()
            self._password = str(config.get("password") or "")
            url = str(config.get("browserless_url") or "").strip().rstrip("/")
            self._browserless_url = url or _DEFAULT_BROWSERLESS_URL
            self._browserless_token = str(config.get("browserless_token") or "").strip()
            self._cron = str(config.get("cron") or _DEFAULT_CRON).strip()
            self._onlyonce = bool(config.get("onlyonce"))

        if self._onlyonce:
            self._onlyonce = False
            self._save_config()
            logger.info("栖影签到：收到立即执行指令，15 秒后开始")
            import threading
            threading.Timer(15, self.run_task).start()

    def get_state(self) -> bool:
        return bool(self._enabled)

    def stop_service(self) -> None:
        pass

    # ------------------------------------------------------------------ 调度
    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            if self._cron.count(" ") != 4:
                logger.error(f"栖影签到：cron 表达式不合法：{self._cron}")
                return []
            return [{
                "id": "HdhSignIn",
                "name": "栖影每日签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_task,
                "kwargs": {},
            }]
        except Exception as err:  # noqa: BLE001
            logger.error(f"栖影签到：注册定时任务失败：{err}")
            return []

    # ------------------------------------------------------------------ 接口
    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET"],
            "summary": "立即签到",
            "description": "手动执行一次签到，不影响定时任务",
        }]

    def api_run(self) -> schemas.Response:
        ok, msg = self.run_task()
        return schemas.Response(success=ok, message=msg,
                                data={"success": ok, "message": msg})

    # ------------------------------------------------------------------ 业务
    def run_task(self) -> Tuple[bool, str]:
        try:
            ok, msg, extra = self._signin()
        except Exception as err:  # noqa: BLE001
            logger.error(f"栖影签到：执行异常 {err}\n{traceback.format_exc()}")
            ok, msg, extra = False, f"执行异常：{err}", {}

        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(ok),
            "msg": msg,
        }
        record.update({k: v for k, v in (extra or {}).items() if v not in (None, "")})
        self._append_history(record)

        if ok:
            logger.info(f"栖影签到：{msg}")
        else:
            logger.error(f"栖影签到：{msg}")

        if self._notify and (not ok or extra.get("changed")):
            try:
                self.post_message(title="【栖影签到】",
                                  text=msg if ok else f"签到失败：{msg}")
            except Exception as err:  # noqa: BLE001
                logger.error(f"栖影签到：发送通知失败 {err}")
        return ok, msg

    def _signin(self) -> Tuple[bool, str, Dict[str, Any]]:
        if not self._username or not self._password:
            return False, "未配置账号或密码", {}
        if not self._browserless_token:
            return False, "未配置 Browserless 令牌", {}

        result = self._bql(self._build_query())
        data = result.get("data") or {}
        if result.get("errors"):
            err = "; ".join(str(e.get("message") or "") for e in result["errors"])
            return False, f"BrowserQL 返回错误：{err[:200]}", {}

        solve = data.get("solve") or {}
        if not solve.get("solved"):
            return False, ("人机验证未通过（Browserless 解 Turnstile 失败），"
                           "稍后重试或更换 Browserless 区域端点"), {}

        home = str((data.get("u2") or {}).get("value") or "")
        me_raw = str((data.get("m2") or {}).get("value") or "")
        ci_raw = str((data.get("k2") or {}).get("value") or "")

        me_status, me = _split_response(me_raw)
        if me_status != 200:
            hint = "登录失败"
            if me_status == 401:
                hint = "登录失败（账号或密码错误，或站点新增了校验）"
            return False, f"{hint}：{str(me)[:160]}", {"url": home}

        points = me.get("points")
        level = str((me.get("level") or {}).get("name") or "")
        nickname = str(me.get("nickname") or me.get("id") or "")

        ci_status, ci = _split_response(ci_raw)
        if ci_status == 409:
            msg = _already_text(ci, points, level)
            return True, msg, {"points": points, "level": level, "changed": False}
        if ci_status == 200:
            gained = None
            if isinstance(ci, dict):
                inner = ci.get("data") if isinstance(ci.get("data"), dict) else ci
                for key in ("points", "gain", "reward", "bonus"):
                    if isinstance(inner.get(key), (int, float)):
                        gained = inner.get(key)
                        break
            if gained:
                msg = f"签到成功，获得 {gained} 积分（当前 {points}）"
            else:
                msg = f"签到成功（当前 {points} 积分）"
            if nickname:
                msg = f"{nickname} {msg}"
            return True, msg, {"points": points, "level": level, "changed": True}
        return False, f"签到接口返回 {ci_status}：{str(ci)[:160]}", {"points": points}

    # ------------------------------------------------------------------ BrowserQL
    def _build_query(self) -> str:
        fill = (
            "(()=>{const u=document.querySelector('#username');"
            "const p=document.querySelector('#password');"
            "if(!u||!p)return 'no-form';"
            "const set=(el,v)=>{const d=Object.getOwnPropertyDescriptor("
            "el.constructor.prototype,'value');d.set.call(el,v);"
            "el.dispatchEvent(new Event('input',{bubbles:true}));};"
            "set(u,%s);set(p,%s);return 'filled';})()"
        ) % (json.dumps(self._username), json.dumps(self._password))
        submit = ("(()=>{const b=document.querySelector('button[type=submit]');"
                  "if(!b)return 'no-btn';b.click();return 'clicked';})()")
        start_me = (
            "(()=>{window.__hdme='pending';"
            "fetch('/api/auth/me',{credentials:'include',headers:{Accept:'application/json'}})"
            ".then(r=>r.text().then(t=>window.__hdme=r.status+' '+t))"
            ".catch(e=>{window.__hdme='0 '+e});return 'ok';})()"
        )
        start_ci = (
            "(()=>{window.__hdci='pending';"
            "fetch('/api/account/checkin',{method:'POST',credentials:'include',"
            "headers:{'Content-Type':'application/json',Accept:'application/json'},body:'{}'})"
            ".then(r=>r.text().then(t=>window.__hdci=r.status+' '+t))"
            ".catch(e=>{window.__hdci='0 '+e});return 'ok';})()"
        )
        return (
            "mutation Sign {\n"
            '  goto(url: "%s", waitUntil: networkIdle) { status }\n'
            '  solve(type: cloudflare, timeout: 90000) { found solved time }\n'
            "  f: evaluate(content: %s) { value }\n"
            "  c: evaluate(content: %s) { value }\n"
            "  w1: waitForTimeout(time: 9000) { time }\n"
            "  m1: evaluate(content: %s) { value }\n"
            "  w2: waitForTimeout(time: 3000) { time }\n"
            '  m2: evaluate(content: "window.__hdme||\\"\\"") { value }\n'
            "  k1: evaluate(content: %s) { value }\n"
            "  w3: waitForTimeout(time: 5000) { time }\n"
            '  k2: evaluate(content: "window.__hdci||\\"\\"") { value }\n'
            '  u2: evaluate(content: "location.href") { value }\n'
            "}"
        ) % (_LOGIN_URL, json.dumps(fill), json.dumps(submit),
             json.dumps(start_me), json.dumps(start_ci))

    def _bql(self, query: str) -> Dict[str, Any]:
        endpoint = _bql_endpoint(self._browserless_url)
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}token={self._browserless_token}"
        try:
            resp = requests.post(url, json={"query": query},
                                 headers={"Content-Type": "application/json"},
                                 timeout=240)
        except Exception as err:  # noqa: BLE001
            return {"errors": [{"message": f"请求 Browserless 失败：{err}"}]}
        if resp.status_code != 200:
            return {"errors": [{"message":
                                f"Browserless 返回 {resp.status_code}：{resp.text[:200]}"}]}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"errors": [{"message": "Browserless 返回内容无法解析"}]}

    # ------------------------------------------------------------------ 记录
    def _append_history(self, record: Dict[str, Any]) -> None:
        history = self.get_data(self._HISTORY_KEY) or []
        if not isinstance(history, list):
            history = []
        history.insert(0, record)
        self.save_data(self._HISTORY_KEY, history[: self._HISTORY_LIMIT])

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "username": self._username,
            "password": self._password,
            "browserless_url": self._browserless_url,
            "browserless_token": self._browserless_token,
            "cron": self._cron,
            "onlyonce": False,
        })

    # ------------------------------------------------------------------ 页面
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._col(4, {"component": "VSwitch",
                                          "props": {"model": "enabled", "label": "启用插件"}}),
                            self._col(4, {"component": "VSwitch",
                                          "props": {"model": "notify", "label": "发送通知"}}),
                            self._col(4, {"component": "VSwitch",
                                          "props": {"model": "onlyonce", "label": "立即签到一次"}}),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, {"component": "VTextField",
                                          "props": {"model": "username", "label": "站点账号"}}),
                            self._col(6, {"component": "VTextField",
                                          "props": {"model": "password", "label": "站点密码",
                                                    "type": "password"}}),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, {"component": "VTextField",
                                          "props": {"model": "browserless_url",
                                                    "label": "Browserless 地址",
                                                    "placeholder": _DEFAULT_BROWSERLESS_URL}}),
                            self._col(6, {"component": "VCronField",
                                          "props": {"model": "cron", "label": "执行周期",
                                                    "placeholder": "5位cron表达式，如 20 8 * * *"}}),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, {"component": "VTextField",
                                           "props": {"model": "browserless_token",
                                                     "label": "Browserless 令牌",
                                                     "type": "password"}}),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "栖影登录强制 Cloudflare Turnstile，且会话绑定出口 IP 与设备指纹，"
                                            "机房 IP 直连一律卡在「正在验证」。本插件用 Browserless 的 "
                                            "BrowserQL 在其浏览器内解验证、登录并直接调用签到接口，"
                                            "不保存 Cookie，每次运行都是独立会话。令牌与「PT站点签到」"
                                            "插件共用同一份即可。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "username": "",
            "password": "",
            "browserless_url": _DEFAULT_BROWSERLESS_URL,
            "browserless_token": "",
            "cron": _DEFAULT_CRON,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data(self._HISTORY_KEY) or []
        if not isinstance(history, list):
            history = []
        rows = []
        for record in history[:15]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-sm"},
                     "text": str(record.get("time", ""))},
                    {"component": "td", "props": {"class": "text-sm"},
                     "text": "成功" if record.get("ok") else "失败"},
                    {"component": "td", "props": {"class": "text-sm"},
                     "text": str(record.get("msg", ""))},
                ],
            })
        if not rows:
            rows.append({
                "component": "tr",
                "content": [{
                    "component": "td",
                    "props": {"colspan": 3, "class": "text-sm text-medium-emphasis"},
                    "text": "还没有执行过，可以点插件页面上的「立即签到一次」或调用 /run 接口",
                }],
            })

        last = history[0] if history else {}
        summary = "暂无记录"
        if last:
            summary = (f"{last.get('time', '')}　{'成功' if last.get('ok') else '失败'}　"
                       f"{last.get('msg', '')}")
        return [{
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {"component": "VAlert",
                     "props": {"type": "success" if last.get("ok") else "info",
                               "variant": "tonal", "text": summary}},
                    {"component": "VTable",
                     "props": {"density": "compact", "class": "mt-3"},
                     "content": [
                         {"component": "thead", "content": [{
                             "component": "tr",
                             "content": [
                                 {"component": "th", "props": {"class": "text-start text-sm"},
                                  "text": "时间"},
                                 {"component": "th", "props": {"class": "text-start text-sm"},
                                  "text": "结果"},
                                 {"component": "th", "props": {"class": "text-start text-sm"},
                                  "text": "说明"},
                             ],
                         }]},
                         {"component": "tbody", "content": rows},
                     ]},
                ],
            }],
        }]

    @staticmethod
    def _col(cols: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": cols},
                "content": [component]}


def _bql_endpoint(address: str) -> str:
    """把 Browserless 地址补成 BrowserQL 端点。"""
    base = address.rstrip("/")
    if base.endswith("/bql"):
        return base
    if base.endswith("/stealth"):
        return base + "/bql"
    return base + "/stealth/bql"


def _split_response(raw: str) -> Tuple[int, Any]:
    """把 "200 {...}" 拆成状态码与解析后的 JSON。"""
    text = str(raw or "").strip()
    if not text or text == "pending":
        return 0, {}
    parts = text.split(" ", 1)
    try:
        status = int(parts[0])
    except (TypeError, ValueError):
        return 0, text[:200]
    body = parts[1] if len(parts) > 1 else ""
    try:
        return status, json.loads(body)
    except Exception:  # noqa: BLE001
        return status, body[:200]


def _already_text(payload: Any, points: Any, level: str) -> str:
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("msg") or "")
    head = "今日已签到"
    if points is not None:
        head += f"，当前 {points} 积分"
    if level:
        head += f"（{level}）"
    if message and "签到" not in message:
        head += f"：{message}"
    return head
