# -*- coding: utf-8 -*-
"""栖影 HDHaven 每日签到 - 普通签 / 赌狗签

站点是 Next.js 自研站，登录接口 POST /api/auth/login 要求携带 Cloudflare
Turnstile 令牌，且会话与出口 IP、设备指纹绑定。机房 IP 环境下 Turnstile 一律
卡在「正在验证」，从别的 IP 复用 Cookie 会被判定异地并直接吊销会话。

唯一稳定的做法是让浏览器、登录、签到同处一个会话：用 Browserless 的 BrowserQL
在它自己的浏览器里解 Turnstile、填表登录，再在同页面内调用签到接口。整条链路
不落地 Cookie，也就不存在会话绑定问题。

BrowserQL 一次 mutation 完成的动作：
  goto /login → solve(type: cloudflare) → 填账号密码 → 点登录 → 等跳转
  → fetch /api/auth/me → fetch POST /api/account/checkin（或 /checkin/gamble）

两种签到每天只能二选一（站点前端按钮按 checkin_today 互斥）：
  普通签  POST /api/account/checkin        固定积分 + 经验
  赌狗签  POST /api/account/checkin/gamble 按档位随机倍数，可能为负，扣到 0 为止，不加经验
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.log import logger
from app.plugins import _PluginBase

_DEFAULT_BROWSERLESS_URL = "https://production-sfo.browserless.io"
_DEFAULT_CRON = "20 8 * * *"

_LOGIN_URL = "https://hdhaven.com/login"
_API_ME = "/api/auth/me"
_API_CHECKIN = "/api/account/checkin"
_API_GAMBLE = "/api/account/checkin/gamble"

_RECORDS_KEY = "hdh_records"
_RECORDS_LIMIT = 500
_TOKEN_SLOT_KEY = "hdh_token_slot"   # 上次成功的令牌槽位 {idx, fp}


class HdhSignIn(_PluginBase):
    # 插件名称
    plugin_name = "栖影签到"
    # 插件描述
    plugin_desc = "栖影 HDHaven 每日自动签到 · 普通签/赌狗签模式 · Browserless 解 Turnstile · 积分与签到记录 · 结果通知 · 失败重试"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "1.4"
    # 插件作者
    plugin_author = "sdcsmyl"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "hdhsignin_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 2

    # ======================== 常量 ========================

    SIGN_MODE_NORMAL = "normal"
    SIGN_MODE_GAMBLE = "gamble"

    # 站点 /api/auth/me 返回的档位表（后台可调，这里只做兜底展示）
    GAMBLE_TIERS = {
        "暴富": 4.0,
        "小赚": 1.5,
        "不亏": 1.0,
        "白签": 0.0,
        "血亏": -1.0,
    }

    # ---- 配置
    _enabled: bool = False
    _notify: bool = True
    _username: str = ""
    _password: str = ""
    _browserless_url: str = _DEFAULT_BROWSERLESS_URL
    _browserless_token: str = ""
    _browserless_token2: str = ""
    _browserless_token3: str = ""
    _browserless_token4: str = ""
    # 本次实际使用的令牌：令牌轮换时由 _do_sign 指定，_bql 读它
    _token_used: str = ""
    _lucky_mode: bool = False
    _cron: str = _DEFAULT_CRON
    _retry: int = 2
    _onlyonce: bool = False

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
            self._browserless_token2 = str(config.get("browserless_token2") or "").strip()
            self._browserless_token3 = str(config.get("browserless_token3") or "").strip()
            self._browserless_token4 = str(config.get("browserless_token4") or "").strip()
            self._lucky_mode = bool(config.get("lucky_mode"))
            self._cron = str(config.get("cron") or _DEFAULT_CRON).strip()
            retry = config.get("retry")
            self._retry = 2 if retry is None else max(int(retry), 0)
            self._onlyonce = bool(config.get("onlyonce"))

        if self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            logger.info("栖影签到：收到立即执行指令，15 秒后开始")
            threading.Timer(15, self.run_task).start()

    def __update_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "username": self._username,
            "password": self._password,
            "browserless_url": self._browserless_url,
            "browserless_token": self._browserless_token,
            "browserless_token2": self._browserless_token2,
            "browserless_token3": self._browserless_token3,
            "browserless_token4": self._browserless_token4,
            "lucky_mode": self._lucky_mode,
            "cron": self._cron,
            "retry": self._retry,
            "onlyonce": False,
        })

    @property
    def _mode(self) -> str:
        return self.SIGN_MODE_GAMBLE if self._lucky_mode else self.SIGN_MODE_NORMAL

    @property
    def _mode_label(self) -> str:
        return "赌狗签" if self._mode == self.SIGN_MODE_GAMBLE else "普通签"

    # ---- 令牌轮换
    def _all_tokens(self) -> List[str]:
        """按配置顺序返回去重后的令牌（空值丢弃）。"""
        out: List[str] = []
        for tok in (self._browserless_token, self._browserless_token2,
                    self._browserless_token3, self._browserless_token4):
            if tok and tok not in out:
                out.append(tok)
        return out

    @staticmethod
    def _token_fp(tok: str) -> str:
        return hashlib.md5(str(tok).encode("utf-8")).hexdigest()[:8]

    def _saved_slot(self, tokens: List[str]) -> int:
        """上次成功的令牌槽位；若配置已变（令牌被换掉）则回到第一个。"""
        try:
            saved = self.get_data(_TOKEN_SLOT_KEY) or {}
            idx = int(saved.get("idx", -1))
            fp = str(saved.get("fp") or "")
            if 0 <= idx < len(tokens) and self._token_fp(tokens[idx]) == fp:
                return idx
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _save_slot(self, tokens: List[str], idx: int) -> None:
        try:
            self.save_data(_TOKEN_SLOT_KEY,
                           {"idx": idx, "fp": self._token_fp(tokens[idx])})
        except Exception:  # noqa: BLE001
            pass

    def get_state(self) -> bool:
        return bool(self._enabled and self._username and self._all_tokens())

    def stop_service(self) -> None:
        pass

    # ------------------------------------------------------------------ 调度
    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state() or not self._cron:
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
        """定时 / 手动签到入口，带失败重试。"""
        if not self._username or not self._password:
            return self._fail("未配置账号或密码")
        if not self._all_tokens():
            return self._fail("未配置 Browserless 令牌")

        logger.info(f"栖影签到开始，模式：{self._mode_label}")
        max_retry = max(int(self._retry or 0), 0)
        result: Dict[str, Any] = {}

        for attempt in range(max_retry + 1):
            try:
                result = self._do_sign()
            except Exception as err:  # noqa: BLE001
                logger.error(f"栖影签到：执行异常 {err}\n{traceback.format_exc()}")
                result = {"ok": False, "msg": f"执行异常：{err}"}

            if result.get("ok"):
                break

            msg = str(result.get("msg") or "")
            # 账号密码错、被风控这类重试也没用，直接停；Browserless 额度耗尽
            # 是确定性失败，重试只会再撞三次 401，也直接停。
            if any(k in msg for k in ("账号或密码", "人机验证", "未配置", "登录失败",
                                      "units usage limit", "Browserless 返回 401")):
                break
            if attempt < max_retry:
                logger.warning(f"栖影签到第 {attempt + 1} 次失败：{msg}，60 秒后重试")
                time.sleep(60)

        ok = bool(result.get("ok"))
        msg = str(result.get("msg") or "")
        if ok:
            logger.info(f"栖影签到：{msg}")
        else:
            logger.error(f"栖影签到最终失败：{msg}")

        self._append_record(result)
        if self._notify:
            self._send_notify(result)
        return ok, msg

    def _fail(self, msg: str) -> Tuple[bool, str]:
        logger.error(f"栖影签到：{msg}")
        return False, msg

    def _do_sign(self) -> Dict[str, Any]:
        """执行一次签到；令牌额度用尽或鉴权失败时自动换下一个令牌。

        Browserless 免费额度按账号计，一个令牌一个月只有 1000 units，
        签到一次约 13–16 units，配几个令牌轮换才能长期不断签。优先从上
        次成功的那个令牌开始，避免每天先撞一次 401。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        now_time = datetime.now().strftime("%H:%M:%S")
        tokens = self._all_tokens()
        if not tokens:
            return {"ok": False, "msg": "未配置 Browserless 令牌",
                    "date": today, "time": now_time, "mode": self._mode}

        start = self._saved_slot(tokens)
        last: Dict[str, Any] = {}
        for step in range(len(tokens)):
            idx = (start + step) % len(tokens)
            self._token_used = tokens[idx]
            if len(tokens) > 1:
                logger.info(f"栖影签到：使用令牌 {idx + 1}/{len(tokens)}")
            result = self._do_sign_once(today, now_time)
            if result.get("ok"):
                self._save_slot(tokens, idx)
                return result
            last = result
            msg = str(result.get("msg") or "")
            # 只有「令牌本身不可用」才值得换；账号密码错、CF 没解过、
            # 站点报错这类换令牌一样失败。
            if not _token_fault(msg):
                return result
            if step < len(tokens) - 1:
                logger.warning(f"栖影签到：令牌 {idx + 1}/{len(tokens)} 不可用"
                               f"（{msg[:80]}），换下一个令牌")
        return last or {"ok": False, "msg": "所有 Browserless 令牌均不可用",
                        "date": today, "time": now_time, "mode": self._mode}

    def _do_sign_once(self, today: str, now_time: str) -> Dict[str, Any]:
        """用当前选定的令牌跑一次完整签到，返回统一结构的结果字典。"""
        mode = self._mode

        result = self._bql(self._build_query())
        data = result.get("data") or {}
        if result.get("errors"):
            err = "; ".join(str(e.get("message") or "") for e in result["errors"])
            return {"ok": False, "msg": f"BrowserQL 返回错误：{err[:200]}",
                    "date": today, "time": now_time, "mode": mode}

        solve = data.get("solve") or {}
        if not solve.get("solved"):
            return {"ok": False, "msg": "人机验证未通过（Browserless 解 Turnstile 失败），"
                                        "稍后重试或更换 Browserless 区域端点",
                    "date": today, "time": now_time, "mode": mode}

        me_raw = str((data.get("m2") or {}).get("value") or "")
        ci_raw = str((data.get("k2") or {}).get("value") or "")

        me_status, me = _split_response(me_raw)
        if not 200 <= me_status < 300:
            hint = "登录失败"
            if me_status == 401:
                hint = "登录失败（账号或密码错误，或站点新增了校验）"
            return {"ok": False, "msg": f"{hint}：{str(me)[:160]}",
                    "date": today, "time": now_time, "mode": mode}

        # 站点统一返回 {"success": true, "data": {...}}，账户字段在 data 里
        info = me.get("data") if isinstance(me.get("data"), dict) else me
        points = info.get("points")
        level = str((info.get("level") or {}).get("name") or "")
        nickname = str(info.get("nickname") or info.get("id") or "")
        reward_base = info.get("checkin_reward")
        checkin_today = bool(info.get("checkin_today"))

        ci_status, ci = _split_response(ci_raw)
        inner = ci.get("data") if isinstance(ci.get("data"), dict) else ci
        if not isinstance(inner, dict):
            inner = {}

        if ci_status == 409 or checkin_today:
            streak = _to_int(inner.get("checkin_streak"))
            return {
                "ok": True, "already": True, "msg": _already_text(ci, points, level),
                "date": today, "time": now_time, "mode": mode, "tier": "",
                "award": 0, "balance": points, "streak": streak,
                "level": level, "nickname": nickname,
            }

        # 站点签到成功返回 201 Created（不是 200）。早期只认 200，导致每天
        # 签到其实已成功却被判失败、60 秒后重跑一次完整浏览器会话，Browserless
        # 免费额度因此翻倍消耗。这里按 2xx 全算成功。
        if 200 <= ci_status < 300:
            earned = _to_int(inner.get("earned"))
            balance = inner.get("points", points)
            streak = _to_int(inner.get("checkin_streak"))
            outcome = str(inner.get("outcome") or "")
            exp = _to_int(inner.get("experience_earned"))

            if mode == self.SIGN_MODE_GAMBLE:
                if outcome:
                    tail = (f"获得 {earned}" if earned and earned > 0
                            else (f"扣除 {-earned}" if earned and earned < 0 else "没有获得"))
                    msg = f"赌狗签到「{outcome}」：{tail} 积分"
                elif earned:
                    msg = f"赌狗签到：{'获得' if earned > 0 else '扣除'} {abs(earned)} 积分"
                else:
                    msg = "赌狗签到：没有获得积分"
            else:
                if earned:
                    msg = f"签到成功，获得 {earned} 积分"
                elif reward_base:
                    msg = f"签到成功，获得 {reward_base} 积分"
                else:
                    msg = "签到成功"
                if exp:
                    msg += f"、{exp} 经验"
            if streak:
                msg += f"，连续签到 {streak} 天"
            if balance is not None:
                msg += f"（当前 {balance} 积分"
                msg += f"，{level}）" if level else "）"
            if nickname:
                msg = f"{nickname} {msg}"

            return {
                "ok": True, "already": False, "msg": msg,
                "date": today, "time": now_time, "mode": mode,
                "tier": outcome, "award": earned or 0, "balance": balance,
                "streak": streak, "level": level, "nickname": nickname,
            }

        return {"ok": False, "msg": f"签到接口返回 {ci_status}：{str(ci)[:160]}",
                "date": today, "time": now_time, "mode": mode, "balance": points}

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
            "fetch('%s',{credentials:'include',headers:{Accept:'application/json'}})"
            ".then(r=>r.text().then(t=>window.__hdme=r.status+' '+t))"
            ".catch(e=>{window.__hdme='0 '+e});return 'ok';})()" % _API_ME
        )
        if self._mode == self.SIGN_MODE_GAMBLE:
            # 赌狗签不带 body，前端就是 fetch(url, {method:'POST'})
            start_ci = (
                "(()=>{window.__hdci='pending';"
                "fetch('%s',{method:'POST',credentials:'include',"
                "headers:{Accept:'application/json'}})"
                ".then(r=>r.text().then(t=>window.__hdci=r.status+' '+t))"
                ".catch(e=>{window.__hdci='0 '+e});return 'ok';})()" % _API_GAMBLE
            )
        else:
            start_ci = (
                "(()=>{window.__hdci='pending';"
                "fetch('%s',{method:'POST',credentials:'include',"
                "headers:{'Content-Type':'application/json',Accept:'application/json'},body:'{}'})"
                ".then(r=>r.text().then(t=>window.__hdci=r.status+' '+t))"
                ".catch(e=>{window.__hdci='0 '+e});return 'ok';})()" % _API_CHECKIN
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
            "}"
        ) % (_LOGIN_URL, json.dumps(fill), json.dumps(submit),
             json.dumps(start_me), json.dumps(start_ci))

    def _bql(self, query: str) -> Dict[str, Any]:
        endpoint = _bql_endpoint(self._browserless_url)
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}token={self._token_used or self._browserless_token}"
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

    # ------------------------------------------------------------------ 通知
    def _send_notify(self, result: Dict[str, Any]) -> None:
        if not result:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_label = "赌狗签" if result.get("mode") == self.SIGN_MODE_GAMBLE else "普通签"

        if not result.get("ok"):
            title = "【栖影签到】失败"
            text = "\n".join([
                "栖影签到失败",
                "",
                f"原因：{result.get('msg') or '未知错误'}",
                f"模式：{mode_label}",
                "",
                f"{now}",
            ])
        elif result.get("already"):
            title = "【栖影签到】今日已签到"
            text = "\n".join([
                "栖影今日已签到",
                "",
                f"积分：{_fmt(result.get('balance'))}",
                f"最近签到：{result.get('date') or '—'}",
                "",
                f"{now}",
            ])
        else:
            award = _to_int(result.get("award")) or 0
            tier = str(result.get("tier") or "")
            if result.get("mode") == self.SIGN_MODE_GAMBLE:
                title = "【栖影签到】" + (f"赌狗签 {tier}" if tier else "赌狗签完成")
                head = f"赌狗签{f'「{tier}」' if tier else ''}"
            else:
                title = "【栖影签到】成功"
                head = "普通签"
            text = "\n".join([
                f"栖影{head}完成",
                "",
                f"本次积分：{'+' if award > 0 else ''}{award}",
                f"当前余额：{_fmt(result.get('balance'))}",
                f"连续签到：{_fmt(result.get('streak'), ' 天')}",
                "",
                f"{now}",
            ])

        try:
            self.post_message(title=title, text=text)
        except Exception as err:  # noqa: BLE001
            logger.error(f"栖影签到：发送通知失败 {err}")

    # ------------------------------------------------------------------ 记录
    def _load_records(self) -> List[Dict[str, Any]]:
        data = self.get_data(_RECORDS_KEY) or []
        if not isinstance(data, list):
            data = []
        if not self.get_data("migrated_v12"):
            data = self._migrate_legacy(data)
        return data

    def _migrate_legacy(self, current: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把 1.1 及以前存在 history 键里的老记录换算成新结构，只做一次。"""
        legacy = self.get_data("history") or []
        merged = list(current)
        seen = {str(r.get("date")) for r in merged if isinstance(r, dict)}
        added = 0
        for item in legacy if isinstance(legacy, list) else []:
            if not isinstance(item, dict):
                continue
            stamp = str(item.get("time") or "")
            if not stamp or stamp[:10] in seen:
                continue
            merged.append({
                "date": stamp[:10],
                "time": stamp[11:19],
                "success": bool(item.get("ok")),
                "already": bool(item.get("ok")) and not item.get("changed", True),
                "mode": self.SIGN_MODE_NORMAL,
                "tier": "",
                "award": 0,
                "balance": item.get("points"),
                "streak": None,
                "level": item.get("level") or "",
                "nickname": "",
                "msg": str(item.get("msg") or ""),
            })
            seen.add(stamp[:10])
            added += 1
        merged.sort(key=lambda r: str(r.get("date")))
        merged = merged[-_RECORDS_LIMIT:]
        try:
            self.save_data(_RECORDS_KEY, merged)
            self.save_data("migrated_v12", True)
            if added:
                logger.info(f"栖影签到：已把 {added} 条老记录迁移到新结构")
        except Exception as err:  # noqa: BLE001
            logger.error(f"栖影签到：迁移老记录失败 {err}")
        return merged

    def _append_record(self, result: Dict[str, Any]) -> None:
        if not isinstance(result, dict) or not result.get("date"):
            return
        records = [r for r in self._load_records()
                   if str(r.get("date")) != str(result.get("date"))]
        records.append({
            "date": result.get("date"),
            "time": result.get("time"),
            "success": bool(result.get("ok")),
            "already": bool(result.get("already")),
            "mode": result.get("mode") or self.SIGN_MODE_NORMAL,
            "tier": result.get("tier") or "",
            "award": _to_int(result.get("award")) or 0,
            "balance": result.get("balance"),
            "streak": result.get("streak"),
            "level": result.get("level") or "",
            "nickname": result.get("nickname") or "",
            "msg": result.get("msg") or "",
        })
        records = records[-_RECORDS_LIMIT:]
        try:
            self.save_data(_RECORDS_KEY, records)
        except Exception as err:  # noqa: BLE001
            logger.error(f"栖影签到：保存记录失败 {err}")

    # ------------------------------------------------------------------ 页面
    def get_page(self) -> List[dict]:
        """统计页面：账号 KPI + 最近签到记录（数据取自最近一次签到，不额外消耗 Browserless）。"""
        records = self._load_records()
        items = sorted([r for r in records if isinstance(r, dict)],
                       key=lambda r: str(r.get("date")), reverse=True)
        latest = items[0] if items else {}

        total_award = sum(_to_int(r.get("award")) or 0 for r in items)
        success_days = len({r.get("date") for r in items if r.get("success")})
        nickname = str(latest.get("nickname") or "") or "未获取"
        level = str(latest.get("level") or "") or "—"
        balance = latest.get("balance")

        def kpi(icon: str, label: str, value: str, color: str = "", note: str = "") -> dict:
            value_cls = f"text-h6 font-weight-bold text-{color}" if color else "text-h6 font-weight-bold"
            children = [
                {"component": "VIcon",
                 "props": {"color": color or "primary", "size": "28", "class": "mb-1"},
                 "text": icon},
                {"component": "span", "props": {
                    "class": value_cls,
                    "style": "display:block; font-size: clamp(0.8rem, 3.8vw, 1.25rem);"
                             " white-space: normal; overflow-wrap: anywhere;"
                             " word-break: break-all; line-height: 1.2;",
                }, "text": value},
                {"component": "span",
                 "props": {"class": "text-caption text-medium-emphasis d-block mt-1"},
                 "text": label},
            ]
            if note:
                children.append({"component": "div",
                                 "props": {"class": "text-caption text-medium-emphasis"},
                                 "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [{"component": "VCardText",
                             "props": {"class": "text-center pa-3"}, "content": children}],
            }

        last_text = "—"
        if latest.get("date"):
            last_text = f"{latest.get('date')} {str(latest.get('time') or '')}".strip()

        kpis = [
            ("mdi-account", "账号", nickname, "primary", f"等级：{level}"),
            ("mdi-coins", "当前积分", _fmt(balance), "info", "来自最近一次签到"),
            ("mdi-gift", "累计获得", f"{total_award:,}", "warning",
             f"本地记录 {success_days} 天"),
            ("mdi-calendar-check", "最近签到", last_text, "success",
             "赌狗签" if latest.get("mode") == self.SIGN_MODE_GAMBLE else "普通签"),
        ]

        page: List[dict] = [{
            "component": "VRow",
            "props": {"dense": True},
            "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 6}, "content": [kpi(*kpis[0])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6}, "content": [kpi(*kpis[1])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6}, "content": [kpi(*kpis[2])]},
                {"component": "VCol", "props": {"cols": 6, "md": 6}, "content": [kpi(*kpis[3])]},
            ],
        }]

        rows = []
        for idx, r in enumerate(items[:30], start=1):
            ok = bool(r.get("success"))
            already = bool(r.get("already"))
            status = "已签到" if already else ("成功" if ok else "失败")
            status_color = "success" if (ok and not already) else ("info" if already else "error")
            mode = "赌狗签" if r.get("mode") == self.SIGN_MODE_GAMBLE else "普通签"
            tier = str(r.get("tier") or "")
            award = _to_int(r.get("award")) or 0
            award_text = f"{'+' if award > 0 else ''}{award}"
            rows.append([
                (str(idx), "text-body-2 text-medium-emphasis"),
                (str(r.get("date") or "—"), "text-body-2"),
                (str(r.get("time") or ""), "text-body-2 text-center"),
                (mode + (f" · {tier}" if tier else ""), "text-body-2 text-center"),
                (award_text, "text-body-2 font-weight-bold text-center text-"
                             + ("success" if award > 0 else ("error" if award < 0 else "medium-emphasis"))),
                (str(r.get("balance") if r.get("balance") is not None else "—"),
                 "text-body-2 text-center"),
                (status, f"text-body-2 text-center text-{status_color}"),
            ])

        header_cells = [
            ("#", "text-caption text-medium-emphasis"),
            ("日期", "text-caption text-medium-emphasis"),
            ("时间", "text-caption text-center text-medium-emphasis"),
            ("模式", "text-caption text-center text-medium-emphasis"),
            ("积分", "text-caption text-center text-medium-emphasis"),
            ("余额", "text-caption text-center text-medium-emphasis"),
            ("状态", "text-caption text-center text-medium-emphasis"),
        ]

        table_content = [{
            "component": "VTable",
            "props": {"density": "comfortable"},
            "content": [
                {"component": "thead", "content": [
                    {"component": "tr", "content": [
                        {"component": "th", "props": {"class": c}, "text": t}
                        for t, c in header_cells
                    ]},
                ]},
                {"component": "tbody", "content": [
                    {"component": "tr", "content": [
                        {"component": "td", "props": {"class": c}, "text": t} for t, c in row
                    ]} for row in rows
                ]},
            ],
        }] if rows else [
            {"component": "VAlert",
             "props": {"type": "info", "variant": "tonal",
                       "text": "暂无签到记录，点「立即运行一次」或等定时任务执行后生成"}},
        ]

        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {"component": "VCardTitle", "text": "签到记录（最近 30 条）"},
                        {"component": "VCardText", "props": {"class": "pa-2"},
                         "content": table_content},
                    ],
                }],
            }],
        })

        page.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {
                        "type": "info", "variant": "tonal",
                        "text": "普通签与赌狗签每天二选一：赌狗签按站点档位随机翻倍（暴富 4 倍 10%、小赚 1.5 倍 35%、"
                                "不亏 1 倍 25%、白签 10%、血亏 -1 倍 20%），加权期望约 0.975 倍且不加经验。"
                                "想稳定升级就在配置里关掉赌狗签。",
                    },
                }],
            }],
        })

        return page

    # ------------------------------------------------------------------ 表单
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        from .config_form import build_form
        return build_form()


# ---------------------------------------------------------------------- 工具
def _bql_endpoint(address: str) -> str:
    """把 Browserless 地址补成 BrowserQL 端点。"""
    base = address.rstrip("/")
    if base.endswith("/bql"):
        return base
    if base.endswith("/stealth"):
        return base + "/bql"
    return base + "/stealth/bql"


def _token_fault(msg: str) -> bool:
    """失败是否属于「令牌本身不可用」——额度耗尽、鉴权失败、限流。

    这类错误换一个令牌就有机会成功；账号密码错、CF 未过、站点报错换令牌
    一样失败，不该浪费另一个账号的额度去试。
    """
    text = str(msg or "")
    if "Browserless" not in text:
        return False
    return any(k in text for k in ("units usage limit", "401", "429",
                                   "鉴权失败", "Unauthorized", "Too Many Requests"))


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


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, suffix: str = "") -> str:
    num = _to_int(value)
    if num is not None:
        return f"{num:,}{suffix}"
    return "—" if value is None else f"{value}{suffix}"


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
