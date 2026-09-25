"""
飞牛影视 HTTP 管理接口客户端（/v/api/v1/*）。

协议要点（与 MoviePilot 的 trimemedia 模块、bili-plan 的 fnos.rs 交叉验证一致）：
    * 统一前缀      /v/api/v1
    * 登录          v2: POST /v/api/v2/user/loginByPassword（密码为 SHA256 小写 hex）
                   v1: POST /v/api/v1/login（明文，旧版服务端回退用）
                   新版服务端已废弃 v1，故 v2 优先、v1 兜底
    * 会话鉴权      Header  Authorization: <token>   （不带 Bearer 前缀）
    * 请求签名      Header  authx: nonce=<6位数字>&timestamp=<毫秒>&sign=<md5>
                   sign = md5( API_KEY _ path _ nonce _ timestamp _ body_hash _ API_SECRET )
                   body_hash(GET)  = md5("k=v&k2=v2")  —— 未 urlencode 的原文
                   body_hash(其他) = md5(请求体 JSON 原文)
    * 业务码        code == 0 成功；code == -2 需要重新登录；5000 签名无效

签名用的两段密钥内嵌于官方 Web 客户端（逆向所得），已作为默认值内置，
无需用户配置；飞牛升级前端导致 `code=5000 invalid sign` 时才需要覆盖。
"""

import hashlib
import json
import random
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import cfg

# 签名密钥两段，内嵌于官方 trimemedia-web 前端。
# 命名与 MoviePilot / bili-plan 的逆向结果保持一致：
#   API_KEY   是拼接串的第一段，API_SECRET 是最后一段。
# 若飞牛升级后出现 code=5000 invalid sign，优先怀疑这两个值变了。
DEFAULT_API_KEY = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
DEFAULT_API_SECRET = "16CCEB3D-AB42-077D-36A1-F355324E4237"
DEFAULT_APP_NAME = "trimemedia-web"


class FnApiError(Exception):
    """飞牛接口返回非 0 业务码。"""

    def __init__(self, code: int, msg: str, path: str):
        super().__init__(f"[{path}] code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.path = path


class FnClient:
    """带自动登录、自动签名、自动重试的飞牛 REST 客户端。"""

    API_LOGIN = "/v/api/v1/login"
    API_LOGIN_V2 = "/v/api/v2/user/loginByPassword"
    API_MDB_LIST = "/v/api/v1/mdb/list"
    API_MDB_SCAN = "/v/api/v1/mdb/scan/{guid}"
    API_TASK_STOP = "/v/api/v1/task/stop"
    API_TASK_LIST = "/v/api/v1/task/list"

    CODE_OK = 0
    CODE_AUTH_FAILED = -2
    CODE_TASK_DUPLICATE = -14

    def __init__(self):
        self._lock = threading.RLock()
        self._token: str = ""
        self._token_at: float = 0.0
        # token 有效期保守定 12 小时，到期自动重登
        self._token_ttl = 12 * 3600

        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.4, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # ---------------- 配置读取 ----------------
    @property
    def host(self) -> str:
        return str(cfg.get("fn_host", "")).rstrip("/")

    @property
    def username(self) -> str:
        return cfg.get("fn_username", "")

    @property
    def password(self) -> str:
        return cfg.get("fn_password", "")

    @property
    def app_name(self) -> str:
        return cfg.get("fn_app_name", DEFAULT_APP_NAME)

    @property
    def api_key_first(self) -> str:
        """签名串第一段。默认内置官方值，飞牛升级后可覆盖。"""
        return str(cfg.get("fn_api_key") or DEFAULT_API_KEY)

    @property
    def api_secret_last(self) -> str:
        """签名串最后一段。默认内置官方值，飞牛升级后可覆盖。"""
        return str(cfg.get("fn_api_secret") or DEFAULT_API_SECRET)

    @property
    def has_signature_material(self) -> bool:
        # 密钥已内置默认值，恒为可用；保留该属性以兼容既有调用方
        return True

    # ---------------- 签名 ----------------
    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_body(data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _raw_query(params: Optional[dict]) -> str:
        """
        GET 的 body_hash 原文：`k=v&k2=v2`，未 urlencode、按传入顺序。
        与 MoviePilot 的 `queries_unquoted` 一致 —— 用 urlencode 会导致
        中文/特殊字符场景验签失败。
        """
        if not params:
            return ""
        return "&".join(f"{k}={v}" for k, v in params.items())

    def _cse_sign(self, method: str, path: str,
                  params: Optional[dict] = None, data: Any = None,
                  nonce: Optional[str] = None, timestamp: Optional[str] = None) -> str:
        nonce = nonce or str(random.randint(100000, 999999))
        timestamp = timestamp or str(int(time.time() * 1000))

        if method.upper() == "GET":
            serialized = self._raw_query(params)
        else:
            serialized = self._serialize_body(data)

        body_hash = self._md5(serialized)
        raw = "_".join([self.api_key_first, path, nonce, timestamp, body_hash,
                        self.api_secret_last])
        return f"nonce={nonce}&timestamp={timestamp}&sign={self._md5(raw)}"

    # ---------------- 登录 ----------------
    def _login(self) -> str:
        if not self.host:
            raise FnApiError(-1, "未配置飞牛影视地址 fn_host", self.API_LOGIN)
        if not (self.username and self.password):
            raise FnApiError(-1, "未配置飞牛影视管理员账号/密码", self.API_LOGIN)

        # v2 协议：密码传 SHA256 小写 hex。新版服务端已废弃 v1 明文登录。
        sha256 = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
        try:
            resp = self._raw("POST", self.API_LOGIN_V2,
                             data={"username": self.username, "password": sha256,
                                   "app_name": self.app_name})
            token = (resp.get("data") or {}).get("token") if isinstance(resp, dict) else None
            if token:
                with self._lock:
                    self._token = token
                    self._token_at = time.time()
                return token
            raise FnApiError(-1, "v2 登录未返回 token", self.API_LOGIN_V2)
        except FnApiError as e:
            # v2 登录接口存在但账号密码错误 -> 不回退（回退也一样失败）
            if e.code not in (0, -1):
                raise
            # 走到这里说明 v2 接口不可用（404/非 JSON 等），回退旧版 v1 明文登录
        except requests.exceptions.RequestException:
            pass  # 网络/HTTP 层失败，回退 v1 再试

        resp = self._raw("POST", self.API_LOGIN,
                         data={"username": self.username, "password": self.password,
                               "app_name": self.app_name})
        token = (resp.get("data") or {}).get("token") if isinstance(resp, dict) else None
        if not token:
            raise FnApiError(-1, "登录未返回 token，请检查账号密码", self.API_LOGIN)
        with self._lock:
            self._token = token
            self._token_at = time.time()
        return token

    def _ensure_token(self, force: bool = False) -> str:
        with self._lock:
            fresh = (time.time() - self._token_at) < self._token_ttl
            if not force and self._token and fresh:
                return self._token
        return self._login()

    # ---------------- 请求核心 ----------------
    def _raw(self, method: str, path: str,
             params: Optional[dict] = None, data: Any = None) -> dict:
        url = f"{self.host}{path}"
        headers = {
            "Content-Type": "application/json",
            "Referer": f"{self.host}/",
            "authx": self._cse_sign(method, path, params, data),
        }
        # 登录接口本身没有 token，其余接口都带上
        if path != self.API_LOGIN and self._token:
            headers["Authorization"] = self._token

        body = self._serialize_body(data) if data is not None else ""
        try:
            resp = self.session.request(
                method, url, headers=headers, params=params,
                data=body.encode("utf-8") if body else None, timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise FnApiError(-1, f"网络请求失败: {e}", path)
        except ValueError:
            raise FnApiError(-1, f"响应不是合法 JSON: {resp.text[:160]}", path)

        code = payload.get("code")
        if code is None:
            raise FnApiError(-1, "响应缺少 code 字段", path)
        if code == self.CODE_OK:
            return payload
        raise FnApiError(code, payload.get("msg", "未知错误"), path)

    def request(self, method: str, path: str,
                params: Optional[dict] = None, data: Any = None) -> dict:
        """单次请求，遇 -2 自动重登后重试一次。"""
        self._ensure_token()
        try:
            return self._raw(method, path, params, data)
        except FnApiError as e:
            if e.code == self.CODE_AUTH_FAILED and path != self.API_LOGIN:
                self._ensure_token(force=True)
                return self._raw(method, path, params, data)
            raise

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, data: Any = None) -> dict:
        return self.request("POST", path, data=data or {})

    # ---------------- 普通用户凭证校验（求片门户登录用） ----------------
    def verify_credentials(self, username: str, password: str) -> Tuple[bool, str]:
        """
        用给定的账号密码做一次登录校验，返回 (是否通过, 失败原因)。

        刻意在**独立实例**上发起请求，避免覆盖单例持有的管理员 token。
        校验失败与网络失败的文案分开，便于用户自查。
        """
        if not self.host:
            return False, "未配置飞牛影视地址，无法校验账号"

        tmp = FnClient()
        sha256 = hashlib.sha256(password.encode("utf-8")).hexdigest()

        # v2：密码传 SHA256；v2 接口不可用时回退 v1 明文
        try:
            resp = tmp._raw("POST", FnClient.API_LOGIN_V2,
                            data={"username": username, "password": sha256,
                                  "app_name": self.app_name})
            if (resp.get("data") or {}).get("token"):
                return True, ""
        except FnApiError as e:
            # v2 存在但被拒 -> 账号密码问题，不必回退
            if e.code not in (0, -1):
                return False, "账号或密码错误"
        except requests.exceptions.RequestException:
            pass  # 落到 v1 再试

        try:
            resp = tmp._raw("POST", FnClient.API_LOGIN,
                            data={"username": username, "password": password,
                                  "app_name": self.app_name})
            if (resp.get("data") or {}).get("token"):
                return True, ""
            return False, "账号或密码错误"
        except FnApiError as e:
            if e.code == 5000:
                return False, "签名校验失败（code=5000）：飞牛升级后签名密钥可能已变更"
            return False, f"校验失败：{e.msg}"
        except requests.exceptions.RequestException as e:
            return False, f"无法连接飞牛影视：{e}"

    # ---------------- 健康自检 ----------------
    def test_connection(self) -> Tuple[bool, str]:
        """返回 (是否可用, 说明)。用于设置页连通性测试。"""
        if not self.host:
            return False, "未配置飞牛影视地址"
        if not (self.username and self.password):
            return False, "未配置飞牛影视账号，无法调用 REST 接口；播放统计不受影响"
        try:
            libs = self.library_list()
            return True, f"连接成功，共读取到 {len(libs)} 个媒体库"
        except FnApiError as e:
            if e.code == 5000:
                return False, "签名无效（code=5000）：飞牛升级后签名密钥可能已变更，需更新内置常量"
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"未知异常: {e}"

    # ---------------- 业务方法 ----------------
    def library_list(self) -> list:
        """媒体库列表，每项含 name / guid。"""
        resp = self.get(self.API_MDB_LIST)
        data = resp.get("data")
        return data if isinstance(data, list) else []

    def library_scan(self, guid: str, dir_list: Optional[list] = None) -> Tuple[bool, str]:
        """触发媒体库扫描。返回 (是否成功, 说明)。"""
        path = self.API_MDB_SCAN.format(guid=guid)
        try:
            self.post(path, {"dir_list": dir_list} if dir_list else {})
            return True, "扫描指令已下发"
        except FnApiError as e:
            if e.code == self.CODE_TASK_DUPLICATE:
                return True, "该媒体库已有正在执行的扫描任务"
            return False, str(e)

    def task_stop(self, guid: str, task_type: str = "TaskItemScrap") -> Tuple[bool, str]:
        try:
            self.post(self.API_TASK_STOP, {"guid": guid, "type": task_type})
            return True, "任务已停止"
        except FnApiError as e:
            return False, str(e)

    def task_list(self) -> list:
        try:
            resp = self.get(self.API_TASK_LIST)
            data = resp.get("data")
            if isinstance(data, dict):
                data = data.get("list") or data.get("items") or []
            return data if isinstance(data, list) else []
        except FnApiError:
            # 端点路径在不同版本可能不同，失败不应影响调用方
            return []


# 全局单例
fn_client = FnClient()
