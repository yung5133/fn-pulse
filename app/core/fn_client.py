"""
飞牛影视 HTTP 管理接口客户端（/v/api/v1/*）。

协议要点（逆向自社区实现，非官方公开文档）：
    * 统一前缀      /v/api/v1
    * 登录          POST /v/api/v1/login  {"username","password","app_name"} -> data.token
    * 会话鉴权      Header  Authorization: <token>
    * 请求签名      Header  authx: nonce=<6位数字>&timestamp=<毫秒>&sign=<md5>
                   sign = md5(secret + "_" + path + "_" + nonce + "_" + timestamp
                              + "_" + body_hash + "_" + api_key)
                   body_hash(GET)  = md5(urlencode(sorted(params.items())))
                   body_hash(其他) = md5(json.dumps(body, sort_keys=True,
                                                    separators=(",", ":"),
                                                    ensure_ascii=False))
    * 业务码        code == 0 成功；code == -2 需要重新登录

关于 secret / api_key：
    这两个值是飞牛影视 Web 端内置常量，官方未公开，本项目无法内置。
    未配置时本客户端会直接返回明确错误；SQLite 引擎不依赖它们，
    因此播放统计等全部核心功能仍可正常使用。
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
        return cfg.get("fn_app_name", "trimemedia-web")

    @property
    def secret_string(self) -> str:
        return cfg.get("fn_secret_string", "")

    @property
    def api_key(self) -> str:
        return cfg.get("fn_api_key", "")

    @property
    def has_signature_material(self) -> bool:
        return bool(self.secret_string and self.api_key)

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

    def _cse_sign(self, method: str, path: str,
                  params: Optional[dict], data: Any) -> str:
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time() * 1000))

        if method.upper() == "GET":
            serialized = urlencode(sorted(params.items())) if params else ""
        else:
            serialized = self._serialize_body(data)

        body_hash = self._md5(serialized)
        raw = "_".join([self.secret_string, path, nonce, timestamp, body_hash, self.api_key])
        return f"nonce={nonce}&timestamp={timestamp}&sign={self._md5(raw)}"

    # ---------------- 登录 ----------------
    def _login(self) -> str:
        if not self.host:
            raise FnApiError(-1, "未配置飞牛影视地址 fn_host", self.API_LOGIN)
        if not self.username or not self.password:
            raise FnApiError(-1, "未配置飞牛影视管理员账号/密码", self.API_LOGIN)
        if not self.has_signature_material:
            raise FnApiError(
                -1,
                "缺少 authx 签名素材（fn_secret_string / fn_api_key），HTTP 引擎不可用；"
                "请从飞牛影视 Web 端获取，或改用 SQLite 数据源模式",
                self.API_LOGIN,
            )

        payload = {"username": self.username, "password": self.password, "app_name": self.app_name}
        resp = self._raw("POST", self.API_LOGIN, data=payload)
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

    # ---------------- 健康自检 ----------------
    def test_connection(self) -> Tuple[bool, str]:
        """返回 (是否可用, 说明)。用于设置页连通性测试。"""
        if not self.host:
            return False, "未配置飞牛影视地址"
        if not self.has_signature_material:
            return False, (
                "未配置 authx 签名素材（fn_secret_string / fn_api_key）。"
                "HTTP 引擎需要这两个值；当前请使用 SQLite 数据源模式。"
            )
        try:
            libs = self.library_list()
            return True, f"连接成功，共读取到 {len(libs)} 个媒体库"
        except FnApiError as e:
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
