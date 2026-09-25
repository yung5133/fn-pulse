"""
authx 签名验证工具 —— 校验签名实现与抓包是否一致。

背景：authx 的
    sign = md5( API_KEY _ path _ nonce _ timestamp _ body_hash _ API_SECRET )
两段密钥内嵌于官方 trimemedia-web 前端（逆向自 MoviePilot / bili-plan），
已随本项目内置为默认值。本工具不联网，纯本地重算比对，用途：
    * 校验本项目/你自己的签名实现是否正确
    * 飞牛升级后若出现 code=5000 invalid sign，用新抓的包验证新密钥

用法一（推荐）：贴一条抓到的请求
    python tools/verify_authx.py \
        --url "http://nas:5666/v/api/v1/mdb/list" \
        --method GET \
        --authx "nonce=123456&timestamp=1735000000000&sign=abcd..."

省略 --secret/--api-key 时使用内置默认密钥；覆盖时显式传参即可。

用法二：手动给分量
    python tools/verify_authx.py \
        --path /v/api/v1/mdb/list --method GET \
        --nonce 123456 --timestamp 1735000000000 --sign abcd...

注意：GET 的 body_hash 用「未 urlencode」的 k=v&k2=v2 原文（与官方客户端一致）；
     POST 用请求体 JSON 原文。
"""

import argparse
import hashlib
import json
import sys
from urllib.parse import parse_qsl, urlsplit

# 与 app/core/fn_client.py 保持一致的内置密钥
DEFAULT_API_KEY = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
DEFAULT_API_SECRET = "16CCEB3D-AB42-077D-36A1-F355324E4237"


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def serialize_body(data) -> str:
    """与 app.core.fn_client.FnClient._serialize_body 严格一致。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_hash_for(method: str, params=None, body=None) -> str:
    """
    与 fn_client._cse_sign 的分支一致。
    GET 用「未 urlencode」的 k=v&k2=v2 原文（按传入顺序），
    这一点与官方客户端一致，urlencode 反而会验签失败。
    """
    if method.upper() == "GET":
        if not params:
            return md5("")
        return md5("&".join(f"{k}={v}" for k, v in params.items()))
    return md5(serialize_body(body))


def compute_sign(secret: str, path: str, nonce: str, timestamp: str,
                 bh: str, api_key: str) -> str:
    raw = "_".join([secret, path, nonce, timestamp, bh, api_key])
    return md5(raw)


def parse_authx(authx: str):
    """nonce=..&timestamp=..&sign=.. -> (nonce, timestamp, sign)"""
    out = {}
    for part in (authx or "").split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out.get("nonce", ""), out.get("timestamp", ""), out.get("sign", "")


def verify(secret: str, api_key: str, method: str, path: str,
           params=None, body=None, nonce: str = "", timestamp: str = "",
           sign: str = "") -> dict:
    bh = body_hash_for(method, params, body)
    expected = compute_sign(secret, path, nonce, timestamp, bh, api_key)
    return {
        "match": hmac_equal(expected, sign),
        "expected_sign": expected,
        "given_sign": sign,
        "body_hash": bh,
        "string_to_sign": "_".join([secret, path, nonce, timestamp, bh, api_key]),
    }


def hmac_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest((a or "").lower(), (b or "").lower())


def main() -> int:
    p = argparse.ArgumentParser(description="验证飞牛影视 authx 签名")
    p.add_argument("--secret", default=DEFAULT_API_SECRET,
                   help="签名串末段（默认用内置 API_SECRET）")
    p.add_argument("--api-key", default=DEFAULT_API_KEY,
                   help="签名串第一段（默认用内置 API_KEY）")
    p.add_argument("--url", help="完整请求 URL（含查询串）")
    p.add_argument("--path", help="相对路径，如 /v/api/v1/mdb/list")
    p.add_argument("--method", default="GET", help="GET / POST，默认 GET")
    p.add_argument("--body", help="POST 请求体 JSON 字符串")
    p.add_argument("--params", help="GET 查询串，如 a=1&b=2（与 --url 二选一即可）")
    p.add_argument("--authx", help="抓到的 authx 头，形如 nonce=..&timestamp=..&sign=..")
    p.add_argument("--nonce", help="或手动给 nonce")
    p.add_argument("--timestamp", help="或手动给 timestamp")
    p.add_argument("--sign", help="或手动给 sign")
    args = p.parse_args()

    if not args.url and not args.path:
        p.error("--url 与 --path 至少给一个")

    if args.url:
        parts = urlsplit(args.url)
        path = parts.path
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
    else:
        path = args.path
        params = dict(parse_qsl(args.params or "", keep_blank_values=True))

    body = None
    if args.body:
        try:
            body = json.loads(args.body)
        except ValueError:
            body = args.body  # 允许非 JSON 原文

    if args.authx:
        nonce, timestamp, sign = parse_authx(args.authx)
    else:
        nonce, timestamp, sign = args.nonce or "", args.timestamp or "", args.sign or ""
    if not (nonce and timestamp and sign):
        p.error("需要 --authx，或同时给 --nonce / --timestamp / --sign")

    r = verify(args.secret, args.api_key, args.method, path, params, body,
               nonce, timestamp, sign)

    print(f"path          : {path}")
    print(f"method        : {args.method.upper()}")
    print(f"api_key(1st)  : {args.api_key}")
    print(f"api_secret(end): {args.secret}")
    print(f"body_hash     : {r['body_hash']}")
    print(f"string_to_sign: {r['string_to_sign']}")
    print(f"expected sign : {r['expected_sign']}")
    print(f"given sign    : {r['given_sign']}")
    print()
    if r["match"]:
        print("OK  匹配 —— 签名算法与密钥一致。")
        return 0
    print("X   不匹配 —— 可能原因：")
    print("    1. 密钥已随飞牛升级变更（出现 code=5000 时优先怀疑）")
    print("    2. path 必须是相对路径（不含主机名），且与抓包时完全一致")
    print("    3. GET 的查询串不能 urlencode；POST 用请求体 JSON 原文")
    return 1


if __name__ == "__main__":
    sys.exit(main())
