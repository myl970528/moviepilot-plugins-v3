# MoviePilot 插件库

自用插件集合，全部在 MoviePilot v3.x 上跑过。公开仓库，随意取用，不保证兼容所有站点的改版。

## 插件

| 插件 | 版本 | 说明 |
|---|---|---|
| **Piggo（猪猪）** | 2.1 | 猪猪站（piggo.me）一体化：搜索索引 + 跨站自动辅种（可选实验室 API 增量索引）+ 每日签到 |
| **CustomSiteData（自定义站点数据）** | 1.1 | 给自定义站点注入用户数据解析器 schema，修「更新数据」报 schema：None |
| **QingwaDaily（青蛙每日福利）** | 1.0 | 每天自动买青蛙蝌蚪商店的「每日福利：1000蝌蚪」，1 蝌蚪换 1000 |

## 安装

MoviePilot 的「设置 - 插件市场」里添加本仓库地址：

```
https://github.com/<owner>/moviepilot-plugins-v3
```

把 `<owner>` 换成仓库实际所属账号，然后搜索插件名安装。

`package.v3.json` 是市场索引，`plugins.v3/<插件ID小写>/` 是插件本体。

> 目录名必须是**插件 ID 的小写**（`Piggo` → `plugins.v3/piggo/`）。写成大写时 MoviePilot
> 会报「插件源码目录不存在」，索引本身却能正常解析，很容易误判。

Piggo 2.1 起可在插件设置里填「实验室 API 令牌」（个人中心生成的 32 位令牌）：填了就走
`api.piggo.me` 按发布时间增量刷新辅种索引，并用站点当前做种清单提前判重；留空或 API 失败
会自动回退原来的前台列表，行为与 2.0 一致。令牌等同账号密码，只存在本机插件配置里。
需要给另一台下载器重新补同样资源时，把「跳过站点已在做种的种子」关掉。

CustomSiteData 装好是空的，要在插件设置里填 `域名=解析器`，一行一个：

```
naxx.xxx=NexusPhp
```

> 国内网络直连 `raw.githubusercontent.com` 经常不通。如果在插件市场里拉不到索引，在
> MoviePilot 的「设置 - 系统 - GITHUB_PROXY」里填一个 GitHub 加速前缀（例如 `https://gh-proxy.com`），
> 它会与完整的 raw 地址直接拼接使用。

QingwaDaily 另带一个 `install.sh`：容器重建或插件从页面消失后，在宿主机执行
`sh install.sh`，会自动从容器挂载点推断插件目录、重建软链，最后提示重启容器。

## sites/ 目录

这里的两个文件**不是独立插件**，是 `PTSiteSignIn`（PT 站点签到）的站点模块，用于规则表达不了的站点：

- `hxpt.py` — 好学（hxpt.org，FluxTorrent 新版）。签到走 JSON 接口 `GET /api/v1/attendance` 查状态、`POST /api/v1/attendance/checkin` 提交，需 `x-csrf-token`（值取 Cookie 里的 `flux_csrf`）。
- `pt52.py` — 52PT。签到页改版后是 Cloudflare Turnstile + 本站 Referer 校验，用 Browserless BrowserQL 打开页面、solve、点击提交。**需要自备 Browserless token**，写在 `/config/ptsignin_52pt.json`：`{"address": "https://production-sfo.browserless.io", "token": "..."}`。

### 手动安装（容器内）

```bash
docker cp hxpt.py moviepilot-v3:/app/app/plugins/ptsitesignin/sites/hxpt.py
docker cp pt52.py moviepilot-v3:/app/app/plugins/ptsitesignin/sites/pt52.py
docker restart moviepilot-v3
```

模块放在容器 `/app` 里，**MoviePilot 升级或容器重建后会丢**，需要重新拷一次。插件会自动扫描 `sites/` 目录，模块优先级高于签到规则，不用改站点配置。

## 说明

- 代码里不含任何站点凭据、代理地址或主机路径，全部通过插件配置、站点配置或环境变量传入。
- 依赖站点私有接口，站点改版后可能失效，按需自行维护。
- 这些接口都是抓包摸出来的，没有官方文档，随时可能变。

## 常见问题

**市场里点安装报「非法插件ID」**
该插件已经在 `/config/plugins/<id>` 存在，并且 `/app/app/plugins/<id>` 是指向它的软链接（本地插件
就是这样挂载的）。MoviePilot 安装前做目录快照时会 `resolve()` 这个软链，路径跑到插件根目录之外，
于是判定 ID 非法。要么先在容器里 `rm /app/app/plugins/<id>` 再装，要么干脆继续用本地插件方式，
把代码放 `/config/plugins/<id>` 并建软链。

**市场里点安装报「插件源码目录不存在」**
仓库目录名大小写不对，见上面的说明。

**装完在市场里显示的还是本地版本**
同名的本地插件优先级更高，列表里 `is_local` 为 true 的那条就是本地目录里的代码。
