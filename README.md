# FnPulse · 飞牛映迹

[![CI](https://github.com/yung5133/fn-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/yung5133/fn-pulse/actions/workflows/ci.yml)
[![构建镜像](https://github.com/yung5133/fn-pulse/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/yung5133/fn-pulse/actions/workflows/docker-publish.yml)

飞牛影视（fnOS `trim.media`）的数据洞察与管理面板。项目结构与数据组织方式参考
[emby-pulse](https://github.com/zeyu8023/emby-pulse)，数据适配层针对飞牛影视重新实现。

> 一句话概括：**让飞牛服主看清谁在看、看什么、什么时候看、用什么画质看。**

---

## 先看结论：与 Emby 版的核心差异

emby-pulse 依赖 Emby 官方 **Playback Reporting 插件**提供的 `submit_custom_query`
任意 SQL 穿透接口，因此远程 API 也能取到完整播放流水。**飞牛影视没有等价能力。**

因此本项目的双擎职责被重新划分 —— 这是移植时唯一、也是最关键的架构决策：

| 引擎 | 能力 | 用途 |
| --- | --- | --- |
| **SQLite**（默认，推荐） | 直接只读 `trimmedia.db`，**完整播放流水** | 仪表盘 / 播放历史 / 风云榜 / 洞察 / 用户中心 |
| **HTTP REST** | `/v/api/v1/*` | 媒体库列表、触发扫描、停止任务 |

好消息是：飞牛影视的 `trimmedia.db` **原生自带**完整的 `item_user_play` 播放流水表，
不需要像 Emby 那样额外装插件。SQLite 模式开箱可用。

---

## 快速部署

镜像由 GitHub Actions 自动构建并推送到 GHCR，支持 `linux/amd64` 与 `linux/arm64`。

| 端口 | 用途 |
| --- | --- |
| `http://<你的IP>:10207` | 🔒 管理员后台 |
| `http://<你的IP>:10208` | 👤 用户求片门户 |

```yaml
version: "3.8"
services:
  fn-pulse:
    image: ghcr.io/yung5133/fn-pulse:latest
    container_name: fn-pulse
    restart: unless-stopped
    # 双端口：10207 管理后台 / 10208 用户求片门户
    network_mode: host
    volumes:
      - ./config:/app/config
      # 飞牛影视的媒体数据库目录，强烈建议只读挂载
      - /usr/local/apps/@appdata/trim.media/database:/fn-data:ro
    environment:
      - TZ=Asia/Shanghai
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=请改成强密码
```

### 端口号：为什么是 10207 / 10208

上游 emby-pulse 占用 `10307`（管理后台）和 `10308`（用户求片门户）——
`main.py` 里的 `start_10308_server()` 是独立绑定 10308 的，**不只是 10307**。
所以本项目整体下移到 `10207 / 10208`，才能与 emby-pulse 在同一台机器上共存。

两个端口都可以用环境变量覆盖：`PORT`（后台）与 `USER_PORT`（门户）。

### 版本号与镜像更新

版本号以仓库根目录的 **`VERSION` 文件为唯一来源**（当前 `0.4.0`）。
每次推送到 `main` 都会构建并打上两类 tag：

| tag | 是否可变 | 用途 |
| --- | --- | --- |
| `0.4.0` | 可变（跟随 main） | 日常更新：`docker compose pull && up -d` |
| `0.4.0-r12` | **不可变**（每次构建唯一） | 锁定与回滚 |
| `latest` | 可变 | 与 main 同步 |
| `v1.2.3` | 语义化 | 推 `v*` git tag 时产生 |
| `sha-<短哈希>` | 不可变 | 对应具体提交 |

版本号同时作为构建参数注入容器，所以**界面显示的版本号与镜像 tag 永远一致**：

```bash
docker pull ghcr.io/yung5133/fn-pulse:0.4.0        # 最新 0.4.0
docker pull ghcr.io/yung5133/fn-pulse:0.4.0-r12    # 精确到某次构建
```

发新版本：改 `VERSION` 文件（如 `0.4.0` → `0.5.0`）后推送即可。

### 界面预览

想在不部署的情况下看界面长什么样：

```bash
python tools/make_preview.py     # 生成 preview/*.html，含各页面快照
```

它用合成数据渲染全部页面（含已登录状态与若干求片记录），
可直接用浏览器打开，用于改样式前对版。

### 本地开发

```bash
pip install -r requirements.txt
CONFIG_DIR=./config FN_DB_PATH=/path/to/trimmedia.db PORT=10207 USER_PORT=10208 python run.py
```

跑测试（使用合成的 `trimmedia.db`，不接触真实数据）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
python tests/smoke.py      # 退出码 0 表示全部通过
```

---

## 数据源实现要点

### SQLite 引擎为什么不直接读库？

`trimmedia.db` 由飞牛影视服务持续持有（通常为 WAL 模式），直接连接会与其它进程争锁，
在只读挂载下更无法创建 `-shm`。处理办法：

1. 把主库连同 `-wal` / `-shm` 一起复制到临时文件
2. `os.replace()` 原子替换成快照
3. 以 `file:...?mode=ro` 只读打开快照

副本默认 60 秒重建一次（`db_copy_ttl` 可调），也可在设置页手动重建。

### 已知表结构

```
user             guid, username, last_login_time(毫秒), is_admin, status
item             guid, title, original_title, parent_guid, overview, type,
                 season_number, episode_number, runtime(分钟), release_date
item_user_play   item_guid, user_guid, visible, update_time(毫秒),
                 create_time(毫秒), ts(已播秒数), watched, type, resolution
```

三处**单位陷阱**，均已处理：`update_time/create_time` 是毫秒时间戳、
`item.runtime` 是**分钟**、`item_user_play.ts` 是**秒**。

未能确认的列（如媒体库归属、软删标记）一律通过 `PRAGMA` 运行期探测后决定是否启用，
不硬编码猜测列名。

---

## REST 签名（authx）

飞牛影视的 REST 接口要求带签名头。签名密钥**已随本项目内置**（来自官方 Web 客户端，
逆向自 MoviePilot 的 trimemedia 模块与 bili-plan 的 fnos.rs，两个独立实现交叉印证），
开箱即用，无需任何配置。

```
authx: nonce=<6位数字>&timestamp=<毫秒>&sign=<md5>

sign       = md5( API_KEY _ path _ nonce _ timestamp _ body_hash _ API_SECRET )
             API_KEY   = NDzZTVxnRKP8Z0jXg1VAMonaG8akvh      （第一段）
             API_SECRET= 16CCEB3D-AB42-077D-36A1-F355324E4237 （末段）
body_hash(GET)  = md5("k=v&k2=v2")     # 未 urlencode 的原文，按传入顺序
body_hash(其他) = md5(请求体 JSON 原文)

登录：v2  POST /v/api/v2/user/loginByPassword   密码为 SHA256 小写 hex
      v1  POST /v/api/v1/login                  明文（旧版服务端兜底）
会话：Header  Authorization: <token>              （不带 Bearer 前缀）
业务码：code == 0 成功；code == -2 需重新登录；code == -14 重复任务；5000 签名无效
```

两个易错点（本项目已处理，自己实现时务必注意）：
* **GET 的 body_hash 不能 urlencode** —— 官方客户端用的是原文拼接，urlencode 后验签失败
* **签名用的 path 要带 `/v` 前缀** —— 与实际请求路径一致

### 飞牛升级后签名失效怎么办

表现是 REST 接口返回 `code=5000 invalid sign`。这说明飞牛改了前端密钥，
用 `tools/verify_authx.py` 重新核对即可：

```bash
# 从浏览器 Network 面板任选一条请求，复制 URL 与 authx 请求头
python tools/verify_authx.py \
    --url "http://<NAS>:5666/v/api/v1/mdb/list" \
    --method GET \
    --authx "nonce=123456&timestamp=1735000000000&sign=abcd..."
```

该工具不联网，本地按同算法重算比对；省略 `--api-key/--secret` 时用内置值。
若确认密钥已变，在后台「系统设置 → REST 签名密钥」覆盖，或用环境变量
`FN_API_KEY` / `FN_API_SECRET`。算法实现与工具之间有一致性测试（金标比对）保证不漂移。

> 说明：这两个值不是安全意义上的机密——每个打开过飞牛 Web 端的浏览器都下载过它们。
> 此前本 README 写过"官方未公开、无法内置"，经 MoviePilot / bili-plan 的公开实现
> 交叉验证后已更正，此前的表述是错误的。

---

## 功能模块

| 模块 | 路径 | 数据来源 |
| --- | --- | --- |
| 全景仪表盘 | `/` | 总量 / 累计 / 活跃用户 / 30 天趋势图 |
| 播放历史 | `/history` | 全站流水，支持用户 / 片名 / 时间范围筛选 |
| 内容风云榜 | `/content` | 播放次数与累计时长排行，按类型拆分 |
| 用户中心 | `/users` | 账号只读 + 本地备注 / 到期日 / 统计开关 |
| 数据洞察 | `/insight` | 作息分布、画质结构、用户画像与勋章 |
| 媒体库 | `/library` | 列表与扫描下发（飞牛 REST，密钥已内置） |
| **求片系统** | `/requests_admin`（后台）· `/request`（门户 `10208`） | 见下节 |
| **MoviePilot 对接** | 求片管理页内一键下发 | MP `/api/v1/subscribe` |
| **企业微信机器人** | 后台「系统设置 → 企业微信机器人」 | 长连接，无需公网/备案 |
| 系统设置 | `/settings` | 双擎诊断、一键测试、快照重建 |

### 企业微信机器人（求片对话入口）

后台 → **系统设置 → 企业微信机器人**。填 `Bot ID` 与 `Bot Secret`、勾选启用、
点「测试连接」，显示「已连接」即通。

**为什么不需要公网**：走企业微信**智能机器人**的 WebSocket 长连接
（`wss://openws.work.weixin.qq.com`），容器只需能出网。这与
**自建应用的「API 接收消息」是两套机制** —— 后者要求回调 URL 公网可达
（需备案域名或内网穿透），前者不需要。判断依据是 MoviePilot 的实现
（`app/modules/wechat/wechatbot.py`）：认证只用 `bot_id` + `secret`，
无 CorpID / Token / EncodingAESKey，也没有 HTTP 回调入口。

| 配置项 | 说明 |
| --- | --- |
| `wecom_bot_enabled` | 启用开关（凭证不全时即使打开也不会连接） |
| `wecom_bot_id` / `wecom_bot_secret` | 企微后台创建智能机器人后获取 |
| `wecom_ws_url` | 长连接地址，一般留空；排障时可改 |

运行状态与最近日志直接显示在设置页，另有 `重连` 按钮（网络恢复后不必重启容器）。
状态接口只回显 `secret_set` 布尔值，不返回密钥原文。

接口：`GET /api/wecom/status` · `POST /api/wecom/test` · `POST /api/wecom/restart`。

> 当前机器人已可连接并响应 `帮助` 指令；**求片命令（搜索 → 选片 → 提交）与
> 企微 userid ↔ 飞牛账号绑定尚在开发中**，这是下一步。

### 求片系统

对应 emby-pulse 的求片中心，形态上做了两处适配：

**1. 门户默认要求用飞牛影视账号登录（`portal_auth_mode=fn`）。**

用户必须先用**自己的飞牛影视账号**登录门户，之后提交的求片，**提交人取自服务端会话**
（不是前端自报），用户无法伪造成他人，后台列表会标注「已验证」。

之所以现在能做（早期版本做不到）：飞牛 REST 的 authx 签名密钥已内置，
且 `/v/api/v2|v1` 的登录接口可以用任意用户自己的账号密码校验，
无需管理员 token —— 与 emby-pulse 用 `/Users/AuthenticateByName` 同理。
校验在独立实例上发起，不影响后台持有的管理员登录态。

三档鉴权（后台「系统设置 → 求片门户」切换）：

| `portal_auth_mode` | 行为 | 提交人 |
| --- | --- | --- |
| `fn`（默认） | 必须登录飞牛影视账号 | 取自会话，标注「已验证」 |
| `passcode` | 只需提交口令 `request_passcode` | 用户自报，标注「自报」 |
| `none` | 完全开放 | 用户自报（仅内网/测试） |

**2. 「入库闭环」靠直读媒体库比对，而非等 webhook。**

emby-pulse 依赖 Emby 的 webhook 通知来闭环；飞牛影视没有 webhook。
本项目改为主动比对：后台点一下「检测入库闭环」，把「待处理/已下载」的求片
与 `trimmedia.db` 里的条目按标题匹配，命中即自动置为「已入库」。

其余能力与原版对齐：**豆瓣搜索选片**（海报、年份、类型识别，无需任何 Key）、
状态链路（待处理 → 已下载 → 已入库 / 已拒绝）、管理员备注回传给提交人、
提交人可按用户名查询自己的历史。

**对接 MoviePilot**：求片可直接「下发 MP」，转为 MoviePilot 的订阅——
之后搜索、下载、整理全部交给 MP，本项目的「检测入库闭环」再把状态推进到已入库，
形成 `求片 → MP 订阅 → 自动下载 → 入库闭环` 的完整链路。

下发策略按来源分流（这是能订阅冷门华语剧的关键）：

| 求片来源 | 下发方式 |
| --- | --- |
| 豆瓣（默认选片源） | **直接带 `doubanid` 建 MP 订阅**，不做搜索 —— MP 原生支持豆瓣订阅，华语新剧/冷门剧照样能订 |
| TMDB | 直接带 `tmdbid` |
| 手填片名（无 ID） | 在 MP 里按标题搜索，用类型 + 年份收敛；命中多条弹候选列表，无命中明确报错 |

MP 的 `type` 用的是中文枚举（`电影` / `电视剧`），本项目已做映射。

**选片搜索源**：默认走豆瓣（`movie.douban.com/j/subject_suggest`，免 Key、国内无墙）。
豆瓣联想接口不给剧情简介与评分，这是它的限制而非实现缺失；需要更全的元数据时
可在后台把搜索源切到 TMDB（需 API Key，国内要代理）。豆瓣没有官方公开 API，
用的是其 Web 端联想接口，豆瓣若调整该接口可能需要跟进。

> 暂未对齐：Telegram 机器人协同。它需要 webhook 才能推送播放/入库事件，而飞牛影视
> 不提供事件流 —— 这部分与实时会话监控一样，属于依赖 Emby 服务器能力的范畴。

### 关于用户管理的边界

账号的创建、改密、删除请在飞牛影视中操作。**本项目刻意不提供账号写操作** ——
直接改写 `trimmedia.db` 有损坏风险。本地可维护的只有备注、到期日、是否计入统计。

---

## 与原版（emby-pulse）的能力差异

一句话结论：**能从数据源拿到的，都已实现或可实现；依赖 Emby 服务器自身能力的，做不了。**

| 原版模块 | 本项目状态 | 原因 |
| --- | --- | --- |
| 全景仪表盘 / 播放历史 / 风云榜 / 洞察 | ✅ 已实现 | `trimmedia.db` 原生自带流水 |
| 用户中心 | ✅ 已实现（账号只读） | 飞牛无安全的账号写接口，详见上节 |
| 求片系统 | ✅ 已实现 | 见上节，鉴权方式与闭环机制有差异 |
| MoviePilot 对接 | ✅ 已实现 | 一键下发为 MP 订阅，搜索收敛逻辑见上节 |
| 媒体库扫描下发 | ✅ 已实现 | 飞牛 REST `/v/api/v1/mdb/scan/{guid}` |
| 追剧日历 / 缺集管理 / 映迹工坊 / Telegram | ⏳ 待移植 | 数据可得，纯工程量问题 |
| 实时会话监控（并发人数/IP/转码负荷） | ❌ 不可移植 | 依赖 Emby `/Sessions` 实时接口，飞牛无等价物 |
| 风控中心（并发超限告警 / 黑名单客户端拦截） | ❌ 不可移植 | 依赖 Emby 的 Webhook 事件流，飞牛不提供 |
| 去重管理 | ⚠️ 待定 | 需 `item` 表含文件路径列，尚未确认其存在 |

「⏳ 待移植」的部分欢迎提 issue 催更或直接 PR —— 它们不涉及数据源障碍。

---

## 配置项

| 键 | 说明 |
| --- | --- |
| `fn_host` | 飞牛影视地址，如 `http://127.0.0.1:5666` |
| `fn_username` / `fn_password` | 管理员账号（REST 登录用） |
| `fn_api_key` / `fn_api_secret` | authx 签名密钥两段，已内置默认值，仅飞牛升级后需覆盖 |
| `fn_public_url` | 对外访问地址，用于生成跳转 |
| `playback_data_mode` | `sqlite`（默认）/ `api` |
| `fn_db_path` | trimmedia.db 路径 |
| `db_copy_ttl` | 快照有效期（秒），默认 60 |
| `timezone_offset_hours` | 时区偏移，默认 8 |
| `hidden_users` | 不参与统计的用户 guid 列表 |
| `request_enabled` | 求片通道开关 |
| `portal_auth_mode` | 门户鉴权档位：`fn`（默认，飞牛账号登录）/ `passcode` / `none` |
| `request_passcode` | `passcode` 档使用的提交口令 |
| `search_source` | 求片选片搜索源：douban（默认）/ tmdb |
| `mp_host` | MoviePilot 地址，如 `http://127.0.0.1:3000` |
| `mp_username` / `mp_password` | MP 账号（OAuth2 表单登录换 access_token） |
| `mp_token` | MP 管理员 API_TOKEN，经 `X-API-KEY` 头使用（免登录），与账号密码二选一 |
| `wecom_bot_enabled` / `wecom_bot_id` / `wecom_bot_secret` | 企业微信智能机器人（长连接，无需公网） |
| `wecom_ws_url` | 机器人长连接地址，默认 `wss://openws.work.weixin.qq.com` |

---

## 登录

两条通道：

1. **本地管理员**（默认，永远可用）—— PBKDF2-SHA256 加盐存储。
   首次启动由 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 播种，默认为 `admin / fnpulse`，
   启动日志会强提示修改。
2. **飞牛账号透传**（可选）—— 在系统设置里填好飞牛地址与账号即可，校验走飞牛影视的 REST 登录（v2 SHA256 优先，v1 兜底）。

---

## 兼容性

依赖版本已在 `requirements.txt` 中锁定并通过端到端验证：

```
fastapi 0.141.1 / starlette 1.7.0 / uvicorn 0.53.0 / jinja2 3.1.6
```

> ⚠️ `fastapi >= 0.141` 起 `TemplateResponse` **只接受具名参数**
> （`request=` / `name=` / `context=`），旧的位置参数写法已被移除。
> 升级依赖前请先回归所有页面渲染。

---

## 验证状态

`tests/smoke.py` 覆盖 95 项：鉴权拦截与错误密码、7 个页面渲染、18 个业务接口、
求片全链路（提交 / 校验 / 状态流转 / **入库闭环** / 豆瓣解析器 / 优雅降级）、
**MoviePilot 对接**（未配置提示 / 载荷构造的中文枚举映射 / 配置回填）、
**求片门户鉴权三档**（未登录拒绝 / 登录错误提示 / 口令校验 / 不泄露他人记录）、
**企业微信机器人**（默认不启用 / 凭证不全不连接 / 状态不泄露密钥 / 帮助指令回复）、
门户物理隔离断言、写操作与非法参数过滤。
测试使用合成的 `trimmedia.db`（5 个媒体条目 / 82 条流水），不涉及任何真实用户数据，
且 CI **不依赖豆瓣、TMDB 或 MoviePilot 可用性**（网络路径只做离线解析与载荷验证）。

CI 在每次 push / PR 时于 Python 3.12 与 3.13 上各跑一遍；
`docker-publish.yml` 则会构建 `amd64` / `arm64` 双架构镜像并推送到 GHCR。

---

## 致谢与许可

本项目的**架构与设计思路**参考了 [EmbyPulse（映迹）](https://github.com/zeyu8023/emby-pulse)
（本项目作者维护的镜像：[yung5133/emby-pulse](https://github.com/yung5133/emby-pulse)），
在此感谢原作者的开源贡献。

**但两者的实现完全独立**：飞牛影视的数据模型（trimmedia.db 的三张核心表）、
REST 签名协议（authx）、快照读取机制均与 Emby 体系无关，
本项目未复制 emby-pulse 的任何一行源码，全部代码为针对飞牛影视场景的独立实现。

反过来说，两边接口能力的差异也决定了二者无法共用实现：Emby 版依赖
Playback Reporting 插件的 SQL 穿透接口，而飞牛版必须走 SQLite 直读 + REST 双擎分流。

本项目自身采用 **MIT** 许可证（见 [LICENSE](./LICENSE)），与上游 EmbyPulse 无附属或派生关系。
