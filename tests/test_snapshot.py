"""
trimmedia 快照构建的离线回归测试。

针对线上故障 "服务异常: database disk image is malformed" 的三条根因：
    1. 撕裂读 —— 用垃圾字节覆盖快照后，query() 必须自愈而不是抛错
    2. WAL 数据必须被回放 —— 未 checkpoint 的行也要在快照里可见
    3. 损坏源必须报明确错误、且不得把坏文件留下当作可用快照
"""

import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmpdir = tempfile.mkdtemp(prefix="fnpulse-snap-test-")
os.environ["CONFIG_DIR"] = os.path.join(_tmpdir, "config")
os.environ["FN_DB_PATH"] = os.path.join(_tmpdir, "trimmedia.db")

import app.core.config as _cfgmod  # noqa: E402
from app.core.config import cfg  # noqa: E402
from app.core.media_source import SourceError, _SqliteEngine  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)[:170]))


def make_source(path, rows=3, checkpoint=True):
    """造一个 WAL 模式的 trimmedia 源库。checkpoint=False 时数据留在 WAL 里。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS item (guid TEXT PRIMARY KEY, title TEXT)")
        conn.executemany("INSERT OR REPLACE INTO item VALUES (?, ?)",
                         [(f"g{i}", f"片名{i}") for i in range(rows)])
        conn.commit()
        if checkpoint:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    finally:
        conn.close()


def new_engine(src_path, workdir):
    """每个用例独立 config 目录，避免快照互相干扰。"""
    os.makedirs(workdir, exist_ok=True)
    _cfgmod.CONFIG_DIR = workdir
    cfg.set("fn_db_path", src_path)
    return _SqliteEngine()


def corrupt(path):
    """写出一个「有合法 SQLite 头但页面全坏」的库，触发 malformed。"""
    with open(path, "wb") as fh:
        fh.write(b"SQLite format 3\x00")
        fh.write(os.urandom(4096 * 3))


# ==============================================================================
def test_normal_build():
    wd = os.path.join(_tmpdir, "t1")
    src = os.path.join(wd, "trimmedia.db")
    os.makedirs(wd, exist_ok=True)
    make_source(src)
    eng = new_engine(src, os.path.join(wd, "cfg"))

    rows = eng.query("SELECT COUNT(*) AS c FROM item")
    check("正常构建快照并可查询", rows and rows[0]["c"] == 3, str(rows[0]["c"]) if rows else "无结果")

    ok, msg = eng.verify()
    check("verify() 报告 ok", ok and msg == "ok", msg)

    # 快照应为单文件，不留 -wal/-shm
    leftovers = [s for s in ("-wal", "-shm") if os.path.exists(eng.snapshot_path + s)]
    check("快照为单文件（无 -wal/-shm 残留）", not leftovers, str(leftovers))


def test_wal_replay():
    wd = os.path.join(_tmpdir, "t2")
    src = os.path.join(wd, "trimmedia.db")
    os.makedirs(wd, exist_ok=True)
    make_source(src, rows=2)

    # 关键：保持连接**不关闭**，否则 SQLite 会在最后一个连接关闭时自动
    # checkpoint，数据就落进主库了 —— 那样测不出 WAL 回放。
    holder = sqlite3.connect(src)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("INSERT OR REPLACE INTO item VALUES ('g9', '只在 WAL 里的片子')")
        holder.commit()
        wal_size = os.path.getsize(src + "-wal") if os.path.exists(src + "-wal") else 0

        eng = new_engine(src, os.path.join(wd, "cfg"))
        rows = eng.query("SELECT COUNT(*) AS c FROM item")
        titles = [r["title"] for r in eng.query("SELECT title FROM item")]
        check("WAL 中的未 checkpoint 数据被回放进快照",
              wal_size > 0 and rows[0]["c"] == 3 and "只在 WAL 里的片子" in titles,
              f"wal={wal_size}B rows={rows[0]['c']} titles={titles}")
    finally:
        holder.close()


def test_self_heal_corrupt_snapshot():
    """核心回归：这个用例在修复前会抛 DatabaseError。"""
    wd = os.path.join(_tmpdir, "t3")
    src = os.path.join(wd, "trimmedia.db")
    os.makedirs(wd, exist_ok=True)
    make_source(src, rows=4)
    eng = new_engine(src, os.path.join(wd, "cfg"))

    eng.query("SELECT 1")                    # 先正常建好快照
    corrupt(eng.snapshot_path)               # 再把快照弄坏
    eng._copy_at = time.time()               # 伪装成“刚构建”，使 TTL 不触发重建

    try:
        rows = eng.query("SELECT COUNT(*) AS c FROM item")
        check("读坏快照时自动丢弃并重建（自愈）", rows and rows[0]["c"] == 4,
              str(rows[0]["c"]) if rows else "无结果")
    except Exception as exc:  # noqa: BLE001
        check("读坏快照时自动丢弃并重建（自愈）", False, f"{type(exc).__name__}: {exc}")

    ok, msg = eng.verify()
    check("自愈后 verify() 恢复 ok", ok, msg)


def test_corrupt_source_reports_clearly():
    wd = os.path.join(_tmpdir, "t4")
    src = os.path.join(wd, "trimmedia.db")
    os.makedirs(wd, exist_ok=True)
    corrupt(src)                             # 源库本身就是坏的
    eng = new_engine(src, os.path.join(wd, "cfg"))

    err = ""
    try:
        eng.query("SELECT COUNT(*) AS c FROM item")
    except SourceError as exc:
        err = str(exc)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"

    check("损坏源给出 SourceError 且含重试说明",
          "快照" in err and ("重试" in err or "校验" in err or "读取快照" in err), err)
    check("损坏源不会留下 .building 残留文件",
          not os.path.exists(eng.snapshot_path + ".building"), "")


def test_missing_source():
    wd = os.path.join(_tmpdir, "t5")
    os.makedirs(wd, exist_ok=True)
    eng = new_engine(os.path.join(wd, "not-there.db"), os.path.join(wd, "cfg"))
    err = ""
    try:
        eng.query("SELECT 1")
    except SourceError as exc:
        err = str(exc)
    check("源库不存在时提示挂载路径",
          "找不到飞牛影视数据库" in err and "fn_db_path" in err, err)


def test_ttl_rebuild():
    wd = os.path.join(_tmpdir, "t6")
    src = os.path.join(wd, "trimmedia.db")
    os.makedirs(wd, exist_ok=True)
    make_source(src, rows=1)
    eng = new_engine(src, os.path.join(wd, "cfg"))
    eng.query("SELECT 1")
    first_at = eng._copy_at

    # 源库新增数据，并把快照时间戳推过期（ttl 有最小 5 秒钳制，不去真等）
    conn = sqlite3.connect(src)
    conn.execute("INSERT OR REPLACE INTO item VALUES ('gx','新片')")
    conn.commit()
    conn.close()
    eng._copy_at = time.time() - (eng.ttl + 1)
    rows = eng.query("SELECT COUNT(*) AS c FROM item")
    check("TTL 到期后重建并看到新数据",
          rows[0]["c"] == 2 and eng._copy_at > first_at, f"count={rows[0]['c']}")

    check("invalidate() 令下一次查询强制重建",
          (eng.invalidate() or True) and eng._copy_at == 0.0, str(eng._copy_at))


def main() -> int:
    for fn in (test_normal_build, test_wal_replay, test_self_heal_corrupt_snapshot,
               test_corrupt_source_reports_clearly, test_missing_source, test_ttl_rebuild):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 抛出异常", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\n===== {len(results) - len(failed)}/{len(results)} passed, "
          f"{len(failed)} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
