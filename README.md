# FnPulse · 飞牛映迹

飞牛影视（fnOS `trim.media`）的数据洞察与管理面板。项目结构与数据组织方式参考
[emby-pulse](https://github.com/yung5133/emby-pulse)，数据适配层针对飞牛影视重新实现。

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

### Docker Compose（推荐）

```yaml
version: "3.8"
services:
  fn-pulse:
    image: fn-pulse:latest
    container_name: fn-pulse
    restart: unless-stopped
    network_mode: host          # 管理后台端口 10307
    volumes:
      - ./config:/app/config
      # 飞牛影视的媒体数据库目录，强烈建议只读挂载
      - /usr/local/apps/@appdata/trim.media/database:/fn-data:ro
    environment:
      - TZ=Asia/Shanghai
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=请改成强密码
```

启动后访问 `http://<你的IP>:10307`。

### 本地开发

```bash
pip install -r requirements.txt
CONFIG_DIR=./config FN_DB_PATH=/path/to/trimmedia.db PORT=10307 python run.py
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

飞牛影视的 REST 接口要求带签名头，算法如下：

```
authx: nonce=<6位数字>&timestamp=<毫秒>&sign=<md5>

sign       = md5(secret + "_" + path + "_" + nonce + "_" + timestamp
                 + "_" + body_hash + "_" + api_key)
body_hash(GET)  = md5(urlencode(sorted(params.items())))
body_hash(其他) = md5(json.dumps(body, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False))

登录：POST /v/api/v1/login  {"username","password","app_name"} -> data.token
会话：Header  Authorization: <token>
业务码：code == 0 成功；code == -2 需重新登录；code == -14 重复任务
```

> ⚠️ `secret_string` 与 `api_key` 是飞牛影视 Web 端的内置常量，**官方从未公开**，
> 本项目无法内置。未配置时媒体库列表与扫描功能不可用，界面会明确提示；
> **播放统计不受任何影响**。

---

## 功能模块

| 模块 | 路径 | 数据来源 |
| --- | --- | --- |
| 全景仪表盘 | `/` | 总量 / 累计 / 活跃用户 / 30 天趋势图 |
| 播放历史 | `/history` | 全站流水，支持用户 / 片名 / 时间范围筛选 |
| 内容风云榜 | `/content` | 播放次数与累计时长排行，按类型拆分 |
| 用户中心 | `/users` | 账号只读 + 本地备注 / 到期日 / 统计开关 |
| 数据洞察 | `/insight` | 作息分布、画质结构、用户画像与勋章 |
| 媒体库 | `/library` | 列表与扫描下发（需 REST 签名素材） |
| 系统设置 | `/settings` | 双擎诊断、一键测试、快照重建 |

### 关于用户管理的边界

账号的创建、改密、删除请在飞牛影视中操作。**本项目刻意不提供账号写操作** ——
直接改写 `trimmedia.db` 有损坏风险。本地可维护的只有备注、到期日、是否计入统计。

---

## 配置项

| 键 | 说明 |
| --- | --- |
| `fn_host` | 飞牛影视地址，如 `http://127.0.0.1:5666` |
| `fn_username` / `fn_password` | 管理员账号（REST 登录用） |
| `fn_secret_string` / `fn_api_key` | authx 签名素材，可选 |
| `fn_public_url` | 对外访问地址，用于生成跳转 |
| `playback_data_mode` | `sqlite`（默认）/ `api` |
| `fn_db_path` | trimmedia.db 路径 |
| `db_copy_ttl` | 快照有效期（秒），默认 60 |
| `timezone_offset_hours` | 时区偏移，默认 8 |
| `hidden_users` | 不参与统计的用户 guid 列表 |

---

## 登录

两条通道：

1. **本地管理员**（默认，永远可用）—— PBKDF2-SHA256 加盐存储。
   首次启动由 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 播种，默认为 `admin / fnpulse`，
   启动日志会强提示修改。
2. **飞牛账号透传**（可选）—— 需先配置 REST 签名素材。

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

端到端测试覆盖 40 项：鉴权拦截与错误密码、7 个页面渲染、18 个业务接口、
写操作与非法参数过滤，全部通过。
测试使用合成的 `trimmedia.db`（5 个媒体条目 / 82 条流水），不涉及真实用户数据。

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
