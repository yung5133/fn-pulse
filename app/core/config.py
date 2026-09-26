import json
import os
import threading

# ================= 路径 =================
# 容器内默认 /app/config，本地开发时可用环境变量覆盖，避免强制要求 root 目录结构
CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/config")
if not os.path.exists(CONFIG_DIR):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        CONFIG_DIR = os.path.join(os.getcwd(), "config")
        os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# 本项目自身的业务库（与飞牛的 trimmedia.db 严格区分，绝不同库）
BIZ_DB_PATH = os.path.join(CONFIG_DIR, "fnpulse.db")

# 飞牛影视媒体库数据库（SQLite 引擎只读来源）
DEFAULT_FN_DB = "/fn-data/trimmedia.db"

def _resolve_version() -> str:
    """版本号单一来源：优先环境变量（镜像构建时注入），否则读仓库根的 VERSION 文件。"""
    env = os.getenv("APP_VERSION", "").strip()
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    return "0.4.0"


APP_VERSION = _resolve_version()

DEFAULT_CONFIG = {
    # ---- 飞牛影视连接 ----
    "fn_host": os.getenv("FN_HOST", "http://127.0.0.1:5666").rstrip("/"),
    "fn_username": os.getenv("FN_USERNAME", "").strip(),
    "fn_password": os.getenv("FN_PASSWORD", "").strip(),
    "fn_app_name": os.getenv("FN_APP_NAME", "trimemedia-web").strip(),
    # authx 签名密钥两段（内嵌于官方 Web 客户端，已随项目内置，无需配置）。
    # 若飞牛升级后出现 code=5000 invalid sign，才需要覆盖这两个值。
    "fn_api_key": os.getenv("FN_API_KEY", "").strip(),      # 签名串第一段
    "fn_api_secret": os.getenv("FN_API_SECRET", "").strip(),  # 签名串末段
    "fn_public_url": os.getenv("FN_PUBLIC_URL", "").strip().rstrip("/"),

    # ---- 数据源双擎 ----
    # sqlite: 直接只读 trimmedia.db（默认，推荐，能力最全）
    # api   : 走 /v/api/v1 REST 管理接口（仅媒体库/任务类操作，播放统计不可用）
    "playback_data_mode": os.getenv("PLAYBACK_DATA_MODE", "sqlite").strip().lower(),
    "fn_db_path": os.getenv("FN_DB_PATH", DEFAULT_FN_DB),
    # trimmedia.db 处于 WAL 且被飞牛服务持续持有，必须复制到副本再读。
    # 该值为副本有效期（秒），到期后下一次查询自动重建副本。
    "db_copy_ttl": 60,

    # ---- 统计偏好 ----
    "hidden_users": [],          # 飞牛 user.guid 列表，统计时忽略
    "timezone_offset_hours": 8,  # 飞牛库内时间为 UTC 毫秒时间戳，展示时按此偏移

    # ---- 外部服务 ----
    "tmdb_api_key": os.getenv("TMDB_API_KEY", "").strip(),
    "proxy_url": os.getenv("PROXY_URL", "").strip(),

    # ---- 通知（预留） ----
    "webhook_token": "fnpulse",

    # ---- 求片门户 ----
    # portal_auth_mode 三档：
    #   fn        必须登录飞牛影视账号（默认，推荐）—— 提交人取自会话，不可伪造
    #   passcode  只需提交口令（request_passcode），提交人自报
    #   none      完全开放自报（仅内网/测试）
    "request_enabled": True,
    "portal_auth_mode": "fn",
    "request_passcode": "",
    # 选片搜索源：douban（默认，无需任何 Key）/ tmdb（需 tmdb_api_key，国内要代理）
    "search_source": "douban",

    # ---- MoviePilot 对接（求片一键下发为 MP 订阅）----
    # 地址如 http://127.0.0.1:3000；账号密码走 OAuth2 表单登录拿 access_token。
    # mp_token 为 MP 管理员 API_TOKEN，通过 X-API-KEY 头使用（免登录、管理员身份）；
    # 与账号密码二选一。注意不能装进 Authorization: Bearer —— 它不是 JWT，
    # 那样会走 JWT 解码分支报 "token校验不通过"。
    "mp_host": os.getenv("MP_HOST", "").strip().rstrip("/"),
    "mp_username": os.getenv("MP_USERNAME", "").strip(),
    "mp_password": os.getenv("MP_PASSWORD", "").strip(),
    "mp_token": os.getenv("MP_TOKEN", "").strip(),

    # ---- 企业微信智能机器人（求片对话入口）----
    # 走「智能机器人」的 WebSocket 长连接 wss://openws.work.weixin.qq.com，
    # 容器只需能出网即可 —— 不需要公网 IP、域名、ICP 备案或内网穿透。
    # 注意这与「自建应用 + API 接收消息」是两套机制，后者才要求公网回调地址。
    # bot_id / secret 在企业微信管理后台创建智能机器人后获取。
    "wecom_bot_enabled": False,
    "wecom_bot_id": os.getenv("WECOM_BOT_ID", "").strip(),
    "wecom_bot_secret": os.getenv("WECOM_BOT_SECRET", "").strip(),
    "wecom_ws_url": os.getenv("WECOM_WS_URL", "").strip(),
}

_LOCK = threading.RLock()


class ConfigManager:
    """线程安全的配置管理器：环境变量播种 -> JSON 覆盖 -> 内存 -> 落盘。"""

    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    self.config.update(saved)
            except Exception as e:
                print(f"[配置] 读取失败，回退默认值: {e}")

    def save(self):
        with _LOCK:
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                return True
            except Exception as e:
                print(f"[配置] 写入失败: {e}")
                return False

    def get(self, key, default=None):
        if default is None:
            default = DEFAULT_CONFIG.get(key)
        return self.config.get(key, default)

    def __getitem__(self, key):
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    def __setitem__(self, key, value):
        with _LOCK:
            self.config[key] = value
            self.save()

    def set(self, key, value):
        self[key] = value

    def get_all(self):
        return dict(self.config)

    def update_many(self, mapping: dict):
        with _LOCK:
            self.config.update(mapping)
            self.save()

    @property
    def mode(self) -> str:
        m = str(self.get("playback_data_mode", "sqlite")).lower()
        return m if m in ("sqlite", "api") else "sqlite"

    def public_url(self) -> str:
        """对外可访问的飞牛影视地址，未配置则用内网地址兜底。"""
        raw = (self.get("fn_public_url") or "").strip().rstrip("/")
        if raw.startswith("http"):
            return raw
        return str(self.get("fn_host", "")).rstrip("/")


cfg = ConfigManager()

SECRET_KEY = os.getenv("SECRET_KEY", "fnpulse_secret_key_2026")
# 绕过 emby-pulse 占用的 10307 / 10308 段，整体下移到 102xx：
#   10207  管理员后台
#   10208  用户求片门户（独立 ASGI 引擎，物理隔离，无法越权进入后台）
# 两者都可用环境变量覆盖：PORT / USER_PORT
PORT = int(os.getenv("PORT", "10207"))
USER_PORT = int(os.getenv("USER_PORT", "10208"))
