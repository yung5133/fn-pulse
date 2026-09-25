"""
authx 签名验证工具 —— 确认你从飞牛影视 Web 端提取的素材是否正确。

背景：authx 的 sign = md5(secret + "_" + path + "_" + nonce + "_" + timestamp
                      + "_" + body_hash + "_" + api_key)
其中 secret / api_key 是飞牛影视 Web 前端内置常量，官方未公开，
只能从浏览器加载的 JS 里提取。本工具不联网，纯粹本地重算并比对，
让你在配置之前就能确认提取是否成功。

用法一（推荐）：贴一条抓到的请求
    python tools/verify_authx.py \
        --secret "你的secret" --api-key "你的key" \
        --url "http://nas:5666/v/api/v1/mdb/list" \
        --method GET \
        --authx "nonce=123456&timestamp=1735000000000&sign=abcd..."

用法二：手动给分量
    python tools/verify_authx.py \
        --secret S --api-key K \
        --path /v/api/v1/mdb/list --method GET \
        --nonce 123456 --timestamp 1735000000000 --sign abcd...

GET 请求的查询串用 --url 带上即可（会自动解析）；POST 请求加 --body '{...}'。
"""

import argparse
import hashlib
import json
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit


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
    """与 fn_client._cse_sign 的分支一致：GET 用排序后的查询串，其余用 JSON。"""
    if method.upper() == "GET":
        items = [(str(k), str(v)) for k, v in (params or {}).items()]
        return md5(urlencode(sorted(items)))
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
    p = argparse.ArgumentParser(description="验证飞牛影视 authx 签名素材")
    p.add_argument("--secret", required=True, help="fn_secret_string")
    p.add_argument("--api-key", required=True, help="fn_api_key")
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
    print(f"body_hash     : {r['body_hash']}")
    print(f"string_to_sign: {r['string_to_sign']}")
    print(f"expected sign : {r['expected_sign']}")
    print(f"given sign    : {r['given_sign']}")
    print()
    if r["match"]:
        print("✅ 匹配 —— 签名素材正确，可直接填入 FnPulse 配置。")
        return 0
    print("❌ 不匹配 —— secret 或 api_key 提取有误，或 path/body 与抓包时不一致。")
    print("   提示：确认用的是相对路径（不含主机名），且 method/查询串/请求体与抓包一致。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
