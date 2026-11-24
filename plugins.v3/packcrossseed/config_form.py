# -*- coding: utf-8 -*-
"""拆包辅种 - 配置页 Vuetify JSON 拼装。

设计原则：
  - 卡片分区，图标 + 色彩编码，hint 常驻，手机端自适应。
  - 所有涉及个人环境的字段默认值一律留空：下载器、目标目录、站点范围
    都由使用者自己填，或从 MoviePilot 运行时拉取（choices 入参）。
  - 源码内不含任何真实域名 / IP / 账号 / 目录。
"""
from typing import Any, Dict, List, Tuple


def build_form(
    config: Dict[str, Any],
    downloader_choices: List[dict] | None = None,
    site_choices: List[dict] | None = None,
) -> Tuple[List[dict], Dict[str, Any]]:
    downloader_choices = downloader_choices or []
    site_choices = site_choices or []

    def card(icon: str, color: str, title: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                    {"component": "VIcon", "props": {"color": color, "class": "mr-2"}, "text": icon},
                    {"component": "span", "text": title},
                ]},
                {"component": "VDivider"},
                {"component": "VCardText", "content": rows},
            ],
        }

    def col(md: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    def row(items: List[dict]) -> dict:
        return {"component": "VRow", "content": items}

    def _props(model: str, label: str, kwargs: dict) -> dict:
        props = {"model": model, "label": label}
        for k, v in kwargs.items():
            props[k.replace("_", "-")] = v
        return props

    def textfield(model: str, label: str, **kwargs) -> dict:
        return {"component": "VTextField", "props": _props(model, label, kwargs)}

    def textarea(model: str, label: str, **kwargs) -> dict:
        return {"component": "VTextarea", "props": _props(model, label, kwargs)}

    def switch(model: str, label: str, color: str = "primary", **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["color"] = color
        return {"component": "VSwitch", "props": props}

    def select(model: str, label: str, items: List[dict], **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["items"] = items
        return {"component": "VSelect", "props": props}

    def combobox(model: str, label: str, items: List[dict], **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["items"] = items
        return {"component": "VCombobox", "props": props}

    def alert(atype: str, text: str, **kwargs) -> dict:
        props = {"type": atype, "variant": "tonal", "text": text}
        props.update(kwargs)
        return {"component": "VAlert", "props": props}

    form = [
        {
            "component": "VForm",
            "content": [
                alert(
                    "info",
                    "把整包按电影 / 单集拆开，逐个到站点找独立种子，做 piece 级内容校验，"
                    "确认一致后用硬链接生成不占空间的副本，交给下载器重新校验并辅种。"
                    "原包和原任务全程不动。默认只试跑不写盘，确认结果后再打开执行。",
                    class_="mb-2",
                ),

                # ── 基础设置 ──
                card("mdi-cog", "primary", "基础设置", [
                    row([
                        col(6, switch("enabled", "启用插件", "primary",
                                      hint="总开关；关闭后定时任务不执行", persistent_hint=True)),
                        col(6, switch("dry_run", "试跑模式（不写盘不加种）", "warning",
                                      hint="开启时只做发现、匹配、校验，不创建副本、不添加任务。强烈建议先开着跑一轮",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, select("source_downloaders", "来源下载器（可多选）",
                                      downloader_choices,
                                      multiple=True, chips=True, clearable=True,
                                      hint="从这些下载器的已完成任务里拆包；留空=所有已配置的下载器",
                                      persistent_hint=True)),
                        col(6, select("target_downloader", "加种下载器",
                                      downloader_choices,
                                      hint="校验通过的种子加到这一台；留空则不指定，由下载器决定",
                                      persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(12, textfield("output_dir", "辅种副本输出目录",
                                          placeholder="在此填写 NAS 上的目标目录，例如 /volumeX/xxx/crossseed",
                                          hint="独立副本写到哪里；需与源目录同一文件系统才能硬链接。此项不预填，请自行填写",
                                          persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(12, textfield("torrent_dirs", "种子缓存目录（可多个，逗号分隔）",
                                          placeholder="各下载器 .torrent 缓存在插件容器内的路径，用逗号分隔",
                                          hint="插件按 infohash 在这些目录里找 .torrent 解析包内文件清单；留空则自动探测常见挂载点",
                                          persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(12, textarea("path_mappings", "路径映射（可选，每行一条）",
                                         placeholder="下载器路径=>容器内路径",
                                         hint="下载器上报的保存路径与容器内不一致时填写，例如 /downloads=>/mnt/media/downloads；留空表示一致",
                                         persistent_hint=True, clearable=True, rows=2)),
                    ]),
                ]),

                # ── 发现与拆分 ──
                card("mdi-package-variant-closed", "teal", "发现与拆分", [
                    row([
                        col(3, textfield("min_size_mb", "最小文件体积（MB）",
                                         hint="小于此值的视频文件不参与匹配，用来过滤花絮 / 样本",
                                         persistent_hint=True)),
                        col(3, textfield("max_per_task", "每个任务最多拆几个",
                                         hint="0=不限。整季包可限制数量，先小步试跑",
                                         persistent_hint=True)),
                        col(3, textfield("max_units", "单轮最多处理几个单元",
                                         hint="0=不限。限制一轮的总搜索量，避免一次跑太久",
                                         persistent_hint=True)),
                        col(3, switch("only_finished", "只处理已完成的任务", "success",
                                      hint="只从做种 / 已完成的任务里拆包",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(12, switch("split_only", "只做拆分作种（整包交 iyuu）", "teal",
                                      hint="开启时只加种源种子集号的真子集（单集 {1} 或两集 {1,2}）；"
                                           "完整整包（{1,2,3}）一律跳过，交给 iyuu 等通用辅种插件。"
                                           "关闭则恢复为允许整包辅种",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("include_keywords", "只处理这些任务（可选）",
                                         placeholder="用逗号分隔，例如 剧名A,剧名B",
                                         hint="按任务名过滤，命中任意一个才拆包；留空=处理全部任务。试点时用它锁定一个包",
                                         persistent_hint=True, clearable=True)),
                        col(6, textfield("exclude_keywords", "排除关键词",
                                         placeholder="用逗号分隔，例如 sample,trailer,花絮",
                                         hint="文件名含这些词就跳过，不区分大小写",
                                         persistent_hint=True, clearable=True)),
                    ]),
                ]),

                # ── 匹配与校验 ──
                card("mdi-magnify-scan", "indigo", "匹配与校验", [
                    row([
                        col(6, combobox("sites", "参与匹配的站点（可多选 / 输入）",
                                        site_choices,
                                        multiple=True, chips=True, clearable=True,
                                        hint="站点从 MoviePilot 运行时拉取；留空=使用所有启用的站点",
                                        persistent_hint=True)),
                        col(6, textfield("max_candidates", "每个单元最多校验候选数",
                                         hint="按相似度和体积排序后取前 N 个做 piece 校验，控制站点请求量",
                                         persistent_hint=True)),
                    ]),
                    row([
                        col(4, textfield("size_tolerance", "体积容差（%）",
                                         hint="候选种子与本地文件体积差在多少以内才进入校验",
                                         persistent_hint=True)),
                        col(4, textfield("min_similarity", "名称相似度下限（0-1）",
                                         hint="搜索词与候选标题的词集合相似度下限",
                                         persistent_hint=True)),
                        col(4, textfield("pieces_to_check", "校验片数（0=全量）",
                                         hint="试点时填一个较小值只验前几片；正式跑填 0 做全量校验",
                                         persistent_hint=True)),
                    ]),
                    row([
                        col(12, switch("accept_other_release", "接受不同压制版本", "info",
                                      hint="标题相同但压制组 / 编码不同的候选也纳入；反正 piece 校验会兜底拦掉内容不一致的",
                                      persistent_hint=True)),
                    ]),
                ]),

                # ── 物化与加种 ──
                card("mdi-file-link", "success", "物化与加种", [
                    row([
                        col(6, select("link_mode", "副本生成方式",
                                      [
                                          {"title": "自动（硬链优先，逐级降级）", "value": "auto"},
                                          {"title": "仅硬链接", "value": "hardlink"},
                                          {"title": "仅软链接", "value": "symlink"},
                                          {"title": "仅复制", "value": "copy"},
                                      ],
                                      hint="硬链接不占额外空间；跨文件系统会失败，此时按策略降级",
                                      persistent_hint=True)),
                        col(6, textfield("label", "加种标签",
                                         placeholder="自行填写，例如 拆包辅种",
                                         hint="新任务打上的标签，方便和原包区分；另会自动加上候选来源站点名作为第二个标签",
                                         persistent_hint=True, clearable=True)),
                    ]),
                    row([
                        col(6, switch("auto_start", "加种后立即开始做种", "success",
                                      hint="关闭则以暂停状态添加，由下载器后续校验",
                                      persistent_hint=True)),
                        col(6, switch("notify", "运行结束发送通知", "primary",
                                      hint="每轮跑完推送发现 / 校验 / 加种统计",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, switch("verify_after_add", "加种后复核任务状态", "teal",
                                      hint="开启后校验新任务真实 hash 并触发 recheck，防止加种接口返回旧任务导致误报；已存在且被暂停的完整任务会自动恢复做种",
                                      persistent_hint=True)),
                        col(6, switch("check_site_flow", "下载前检查站点流控", "orange",
                                      hint="开启后下载种子前查询站点流控状态，命中则跳过该候选，避免频繁访问触发站点限制",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, switch("use_cache", "启用增量缓存", "indigo",
                                      hint="记住每个单元上一轮的结论（已辅种/已存在/无候选/校验不过）与候选种子文件，下轮直接跳过，避免每轮全量重跑；试跑模式始终全量",
                                      persistent_hint=True)),
                        col(6, textfield("cache_days", "失败结论缓存天数",
                                         type="number",
                                         hint="搜不到/校验不过的结论保留天数，过期后重试（站点可能后来新发种）；已辅种/已存在的按 3 倍天数兜底",
                                         persistent_hint=True)),
                    ]),
                ]),

                # ── 定时与调试 ──
                card("mdi-clock-fast", "warning", "定时与调试", [
                    row([
                        col(6, textfield("cron", "定时表达式",
                                         hint="Cron 表达式；手动模式可留空",
                                         persistent_hint=True)),
                        col(6, switch("auto_mode", "自动定时执行", "warning",
                                      hint="先关闭，用详情页按钮手动跑通后再打开",
                                      persistent_hint=True)),
                    ]),
                    row([
                        col(6, switch("onlyonce", "保存后立即运行一次", "info",
                                      hint="保存配置后自动触发一轮，用于试跑", persistent_hint=True)),
                        col(6, switch("debug", "详细日志", "info",
                                      hint="把每个候选的校验细节写进插件日志", persistent_hint=True)),
                    ]),
                ]),

                alert("warning",
                      "隐私与安全：本插件不在源码里保存任何站点、账号、目录或网络信息。"
                      "所有下载器与站点列表在打开配置页时从 MoviePilot 运行时读取；"
                      "站点 Cookie 只在内存里交给 MoviePilot 的种子下载链路使用，不落盘、不进日志。"
                      "公开仓库版本所有字段默认值为空，请使用者自行填写。"),
            ],
        },
    ]

    model = {
        "enabled": False,
        "dry_run": True,
        "source_downloaders": [],
        "target_downloader": "",
        "output_dir": "",
        "torrent_dirs": "",
        "path_mappings": "",
        "min_size_mb": 200,
        "max_per_task": 0,
        "max_units": 0,
        "only_finished": True,
        "include_keywords": "",
        "exclude_keywords": "sample,trailer,花絮",
        "sites": [],
        "max_candidates": 5,
        "size_tolerance": 8,
        "min_similarity": 0.25,
        "pieces_to_check": 3,
        "accept_other_release": True,
        "split_only": True,
        "link_mode": "auto",
        "label": "",
        "auto_start": True,
        "verify_after_add": True,
        "check_site_flow": True,
        "use_cache": True,
        "cache_days": 7,
        "notify": True,
        "cron": "",
        "auto_mode": False,
        "onlyonce": False,
        "debug": False,
    }
    # 用已保存配置覆盖默认值（不含任何敏感项硬编码）
    for k in list(model):
        if k in config and config[k] is not None:
            model[k] = config[k]
    return form, model
