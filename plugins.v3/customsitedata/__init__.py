"""
自定义站点用户数据补丁（CustomSiteData）。

MoviePilot 只按站点字典里的 schema 选择「用户数据解析器」。站点管理里手工添加的
自定义站点，定义存在 site.note 这个 json 列（id/name/domain/search/category/
torrents/conf），没有 schema 字段，于是点「更新数据」只会拿到：

    站点 UBits 未找到站点解析器，schema：None
    未获取到站点用户数据，请检查 Cookie 是否有效！

2026-11-01 在 v3.1.7 / v3.2.3 上验证过：往 note 里补 "schema" 无效（SitesHelper
不从 note 读 schema），清空 note 回落内置资源也无效（资源包里没有这两个站的用户
数据规则）。但直接用 NexusPHP 解析器带站点 Cookie 抓页面是能正常出用户数据的。

本插件在 SiteChain.refresh_userdata 入口按域名补 schema，命中映射才注入，其余站点
原样透传。这样「更新数据」按钮和站点数据定时刷新都会走正常解析流程。
"""

from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.log import logger
from app.plugins import _PluginBase

# 已经打过补丁的标记，避免插件重载时重复包装
_PATCH_FLAG = "_hermes_custom_site_schema_patch"
# 生效中的映射挂在 SiteChain 上：包装函数每次调用都重新读，
# 这样插件重载/升级后不会读到被清空的旧字典（旧写法是模块级全局，重载后永久失效）
_MAP_FLAG = "_hermes_custom_site_schema_map"
# 原始 refresh_userdata 的存根位置，重载后据此重新包装，避免叠加多层
_ORIG_FLAG = "_hermes_custom_site_original_refresh"
_MAPPING: Dict[str, str] = {}
_ORIGINAL: Optional[Any] = None

_DEFAULT_MAPPING = ""


def _domain_of(value: Any) -> str:
    """把 url 或域名统一成裸域名（去协议、路径、端口、www）。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if ":" in text:
        text = text.split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text


def _parse_mapping(text: str) -> Dict[str, str]:
    """解析「域名=schema」多行配置，兼容逗号分隔。"""
    result: Dict[str, str] = {}
    for raw in str(text or "").replace(",", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        domain, schema = line.split("=", 1)
        domain = _domain_of(domain)
        schema = schema.strip()
        if domain and schema:
            result[domain] = schema
    return result


def _active_mapping() -> Dict[str, str]:
    """取当前生效的映射：优先读挂在 SiteChain 上的那份，没有则退回模块内副本。"""
    from app.chain.site import SiteChain

    current = getattr(SiteChain, _MAP_FLAG, None)
    return current if isinstance(current, dict) else _MAPPING


def _fill_schema(site: Optional[dict], mapping: Optional[Dict[str, str]] = None) -> Optional[dict]:
    """站点没有 schema 且命中映射时，复制一份并补上 schema。"""
    if not isinstance(site, dict) or site.get("schema"):
        return site
    rules = _active_mapping() if mapping is None else mapping
    domain = _domain_of(site.get("domain") or site.get("url"))
    schema = rules.get(domain)
    if not schema:
        return site
    patched = dict(site)
    patched["schema"] = schema
    return patched


def _install_patch() -> bool:
    """把 SiteChain.refresh_userdata 包一层。

    原始方法记在 SiteChain 上，每次重载都重新包一次，这样插件升级后新代码能立刻生效，
    也不会出现「包了旧的、新的不包」导致补丁失效的情况。
    """
    from app.chain.site import SiteChain

    original = getattr(SiteChain, _ORIG_FLAG, None)
    if original is None:
        original = SiteChain.refresh_userdata
        setattr(SiteChain, _ORIG_FLAG, original)

    def refresh_userdata(self, site: Optional[dict] = None, *args, **kwargs):
        return original(self, _fill_schema(site), *args, **kwargs)

    refresh_userdata.__name__ = getattr(original, "__name__", "refresh_userdata")
    refresh_userdata.__doc__ = getattr(original, "__doc__", None)
    SiteChain.refresh_userdata = refresh_userdata
    setattr(SiteChain, _PATCH_FLAG, True)
    return True


class CustomSiteData(_PluginBase):
    # 插件名称
    plugin_name = "自定义站点数据"
    # 插件描述
    plugin_desc = "给自定义站点补上用户数据解析器（schema），修好站点管理里点「更新数据」报 schema：None 的问题。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "hermes"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "customsitedata_"
    # 加载顺序
    plugin_order = 22
    # 可使用的用户级别
    auth_level = 1

    _enabled: bool = False
    _mapping_text: str = _DEFAULT_MAPPING

    def init_plugin(self, config: Optional[dict] = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            text = str(config.get("sites") or "").strip()
            if text:
                self._mapping_text = text
        elif not self._mapping_text:
            self._mapping_text = _DEFAULT_MAPPING

        _MAPPING.clear()
        if self._enabled:
            _MAPPING.update(_parse_mapping(self._mapping_text))

        try:
            from app.chain.site import SiteChain

            # 先发布映射再装补丁：补丁每次调用都读这份，重载后立刻生效
            setattr(SiteChain, _MAP_FLAG, dict(_MAPPING))
            _install_patch()
            if self._enabled:
                logger.info(f"自定义站点数据：已启用，schema 映射 {_MAPPING or '（空）'}")
            else:
                logger.info("自定义站点数据：插件未启用，不注入 schema")
        except Exception as err:
            logger.error(f"自定义站点数据：安装补丁失败 {err}")

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ 接口

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/state",
            "endpoint": self.api_state,
            "methods": ["GET"],
            "summary": "查看补丁与映射状态",
            "description": "返回补丁是否生效、当前生效的域名映射",
        }]

    def api_state(self) -> schemas.Response:
        from app.chain.site import SiteChain

        data = {
            "enabled": self._enabled,
            "patched": bool(getattr(SiteChain, _PATCH_FLAG, False)),
            "mapping": dict(_active_mapping()),
        }
        return schemas.Response(success=True, data=data)

    # ------------------------------------------------------------------ 页面

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '只对「没有 schema 的自定义站点」生效，内置站点不受影响。'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'sites',
                                            'label': '域名与解析器映射',
                                            'rows': 5,
                                            'placeholder': 'ubits.club=NexusPhp\nqingwapt.com=NexusPhp',
                                            'hint': '每行一条「域名=解析器」，解析器名与 MP 内置 schema 一致，NexusPHP 站一般填 NexusPhp'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": self._enabled,
            "sites": self._mapping_text,
        }

    def get_page(self) -> List[dict]:
        from app.chain.site import SiteChain

        patched = bool(getattr(SiteChain, _PATCH_FLAG, False))
        rows = []
        for domain, schema in sorted(_active_mapping().items()):
            rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-sm'}, 'text': domain},
                    {'component': 'td', 'props': {'class': 'text-sm'}, 'text': schema},
                ]
            })
        if not rows:
            rows.append({
                'component': 'tr',
                'content': [
                    {
                        'component': 'td',
                        'props': {'colspan': 2, 'class': 'text-sm text-medium-emphasis'},
                        'text': '当前没有生效的映射'
                    }
                ]
            })

        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'success' if patched else 'warning',
                                    'variant': 'tonal',
                                    'text': '补丁已装入 SiteChain' if patched else '补丁未装入，重启插件或容器后再看'
                                }
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VTable',
                'props': {'hover': True},
                'content': [
                    {
                        'component': 'thead',
                        'content': [
                            {
                                'component': 'tr',
                                'content': [
                                    {'component': 'th', 'props': {'class': 'text-start text-xs'}, 'text': '域名'},
                                    {'component': 'th', 'props': {'class': 'text-start text-xs'}, 'text': '注入的 schema'},
                                ]
                            }
                        ]
                    },
                    {'component': 'tbody', 'content': rows},
                ]
            },
        ]

    def stop_service(self):
        """停用插件时清空映射，让 refresh_userdata 恢复原样。"""
        _MAPPING.clear()
        try:
            from app.chain.site import SiteChain

            setattr(SiteChain, _MAP_FLAG, {})
        except Exception:
            pass
