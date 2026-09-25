"""
MoviePilot 对接客户端。

只做一件事：把求片下发给 MoviePilot 的订阅系统，让 MP 负责搜索、下载、整理。
之后由 FnPulse 的「检测入库闭环」（比对 trimmedia.db）把状态推进到已入库。

接口依据（MoviePilot v2 /api/v1）：
    POST /api/v1/login/access-token   表单 username/password -> access_token
    GET  /api/v1/media/search         搜索媒体信息（title/page/count）
    POST /api/v1/subscribe/           新增订阅（Subscribe JSON）
    GET  /api/v1/subscribe/           查询订阅

两个易错点，均已处理：
    * Subscribe.type 是中文枚举：电影 / 电视剧（不是 movie / tv）
    * 登录是 OAuth2 表单体，不是 JSON
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import cfg


class MoviePilotError(Exception):
    pass


class MoviePilotClient:
    TOKEN_TTL = 3600

    def __init__(self):
        self._lock = threading.RLock()
        self._token = ""
        self._token_at = 0.0
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # ---------------- 配置 ----------------
    @property
    def host(self) -> str:
        return str(cfg.get("mp_host", "")).rstrip("/")

    @property
    def username(self) -> str:
        return cfg.get("mp_username", "")

    @property
    def password(self) -> str:
        return cfg.get("mp_password", "")

    @property
    def static_token(self) -> str:
        return cfg.get("mp_token", "")

    def is_configured(self) -> bool:
        return bool(self.host and (self.static_token or (self.username and self.password)))

    # ---------------- 鉴权 ----------------
    def _login(self) -> str:
        if not self.host:
            raise MoviePilotError("未配置 MoviePilot 地址")
        if not (self.username and self.password):
            raise MoviePilotError("未配置 MoviePilot 用户名/密码")
        try:
            resp = self.session.post(
                f"{self.host}/api/v1/login/access-token",
                data={"username": self.username, "password": self.password},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            raise MoviePilotError(f"MoviePilot 登录请求失败：{e}")
        except ValueError:
            raise MoviePilotError("MoviePilot 登录响应不是合法 JSON")

        token = payload.get("access_token")
        if not token:
            raise MoviePilotError(f"MoviePilot 登录失败：{payload.get('detail') or payload}")
        with self._lock:
            self._token = token
            self._token_at = time.time()
        return token

    def _token_value(self, force: bool = False) -> str:
        if self.static_token:
            return self.static_token
        with self._lock:
            fresh = (time.time() - self._token_at) < self.TOKEN_TTL
            if not force and self._token and fresh:
                return self._token
        return self._login()

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None, json_body: Any = None,
                 form: Optional[dict] = None) -> Any:
        if not self.host:
            raise MoviePilotError("未配置 MoviePilot 地址")
        headers = {"Authorization": f"Bearer {self._token_value()}"}
        url = f"{self.host}{path}"
        try:
            resp = self.session.request(method, url, headers=headers, params=params,
                                        json=json_body, data=form, timeout=25)
        except requests.exceptions.RequestException as e:
            raise MoviePilotError(f"MoviePilot 请求失败：{e}")

        if resp.status_code == 401 and not self.static_token:
            # token 过期，强制重登一次
            headers = {"Authorization": f"Bearer {self._token_value(force=True)}"}
            try:
                resp = self.session.request(method, url, headers=headers, params=params,
                                            json=json_body, data=form, timeout=25)
            except requests.exceptions.RequestException as e:
                raise MoviePilotError(f"MoviePilot 请求失败：{e}")

        try:
            return resp.json()
        except ValueError:
            raise MoviePilotError(f"MoviePilot 响应不是合法 JSON（HTTP {resp.status_code}）")

    # ---------------- 业务 ----------------
    @staticmethod
    def _normalize_media(m: dict) -> dict:
        """把 MoviePilot 的 MediaInfo 归一化，同时兼容 v2 的若干字段名。"""
        mtype = str(m.get("type") or "").lower()
        if mtype in ("电影", "movie"):
            media_type = "movie"
        elif mtype in ("电视剧", "tv", "show"):
            media_type = "series"
        else:
            media_type = mtype or "movie"
        mediaid = m.get("mediaid") or ""
        tmdb_id = m.get("tmdb_id")
        if not tmdb_id and isinstance(mediaid, str) and mediaid.startswith("tmdb:"):
            try:
                tmdb_id = int(mediaid.split(":", 1)[1])
            except ValueError:
                tmdb_id = None
        return {
            "external_source": "tmdb" if (tmdb_id or "tmdb" in str(mediaid)) else "other",
            "external_id": tmdb_id,
            "mediaid": mediaid,
            "media_type": media_type,
            "title": m.get("title") or "",
            "year": str(m.get("year") or ""),
            "poster_url": m.get("poster_path") or "",
            "overview": (m.get("overview") or "")[:300],
            "rating": m.get("vote_average") or 0,
        }

    def search_media(self, title: str, count: int = 12) -> List[dict]:
        payload = self._request("GET", "/api/v1/media/search",
                                params={"title": title, "page": 1, "count": count})
        # 兼容两种返回：纯列表，或 {"persons": [...], "movies": [...], "tvs": [...]}
        rows: List[dict] = []
        if isinstance(payload, list):
            rows = [x for x in payload if isinstance(x, dict)]
        elif isinstance(payload, dict):
            for k in ("movies", "tvs", "medias", "results", "items"):
                v = payload.get(k)
                if isinstance(v, list):
                    rows.extend(x for x in v if isinstance(x, dict))
        return [self._normalize_media(x) for x in rows if x.get("title")]

    @staticmethod
    def build_subscribe_payload(media: dict, season: int = 0,
                                note: str = "") -> Dict[str, Any]:
        """
        组装 Subscribe JSON。type 必须是中文枚举。
        纯函数，便于离线测试 —— 这是与 MoviePilot 对齐的关键接缝。

        外部 ID 按来源二选一：
            tmdb   -> tmdbid
            douban -> doubanid   （MP 原生支持豆瓣订阅，冷门华语剧也能订阅）
        两者都没有时不带 ID 字段（MP 会按名称识别，成功率较低）。
        """
        media_type = media.get("media_type") or "movie"
        payload: Dict[str, Any] = {
            "name": media.get("title") or "",
            "type": "电影" if media_type == "movie" else "电视剧",
            "year": str(media.get("year") or ""),
            "poster": media.get("poster_url") or "",
            "description": (media.get("overview") or note or "")[:500],
        }
        if media_type != "movie":
            payload["season"] = max(1, int(season or 1))
        if str(media.get("external_source") or "") == "douban" and media.get("douban_id"):
            payload["doubanid"] = str(media["douban_id"])
        elif media.get("external_id"):
            payload["tmdbid"] = media["external_id"]
        return payload

    def add_subscribe(self, payload: dict) -> Tuple[bool, str, Optional[int]]:
        resp = self._request("POST", "/api/v1/subscribe/", json_body=payload)
        if isinstance(resp, dict) and "success" in resp:
            ok = bool(resp.get("success"))
            msg = str(resp.get("message") or "")
            data = resp.get("data")
            sub_id = None
            if isinstance(data, dict):
                sub_id = data.get("id")
            elif isinstance(data, int):
                sub_id = data
            return ok, msg or ("成功" if ok else "失败"), sub_id
        # 某些版本直接返回订阅对象
        if isinstance(resp, dict) and resp.get("id"):
            return True, "成功", resp.get("id")
        return False, str(resp)[:200], None

    def list_subscribes(self) -> List[dict]:
        payload = self._request("GET", "/api/v1/subscribe/")
        return payload if isinstance(payload, list) else []

    def test_connection(self) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "未配置 MoviePilot 地址或账号"
        try:
            subs = self.list_subscribes()
            return True, f"连接成功，当前订阅 {len(subs)} 条"
        except MoviePilotError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"未知异常：{e}"


moviepilot_client = MoviePilotClient()
