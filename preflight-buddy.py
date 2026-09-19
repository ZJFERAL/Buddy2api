#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Buddy2api 启动前体检（自包含，放在 Buddy2api 目录下，不随上游 git pull 被覆盖）。

做两件事，任何一步失败都不阻断启动（退出码恒 0）：

[1] 数据库孤儿外键修复
    Buddy2api >= 2.1.5 的 init_db() 会执行 _migrate_daily_usage()：
        INSERT INTO api_key_daily_usage(api_key_id, ...)
        SELECT api_key_id, ... FROM logs WHERE api_key_id IS NOT NULL ...
    而 api_key_daily_usage 带外键
        FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
    logs 里若残留指向"已删除 API key"的孤儿记录，该迁移会抛
        sqlite3.IntegrityError: FOREIGN KEY constraint failed
    结果 server.py 启动直接崩溃（一闪而过、8787 起不来）。
    处理：把孤儿 api_key_id 置 NULL（保留日志本体）；派生缓存表删孤儿行。

[2] 依赖同步
    从远程同步后 requirements.txt 常会新增依赖，而 .venv 是旧的。
    仅在 requirements.txt 内容(md5)变化时执行一次 pip install，
    用 .venv/.requirements.md5 做戳记，避免每次启动都跑 pip。

用法：
    python preflight-buddy.py
    python preflight-buddy.py [db_path] [venv_dir] [install_log_path]
"""

import hashlib
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "codebuddy_gateway.db")
DEFAULT_VENV = os.path.join(HERE, ".venv")

# (子表, 外键列, 父表, 父表主键列) —— 派生缓存/统计表，孤儿直接删行
FK_RELATIONS = [
    ("account_resource_cache", "account_id", "accounts", "id"),
    ("account_checkin_cache", "account_id", "accounts", "id"),
    ("api_key_daily_usage", "api_key_id", "api_keys", "id"),
]
# logs 单独处理：置 NULL 保留日志内容
LOGS_FK = ("logs", "api_key_id", "api_keys", "id")


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def fix_db(db_path):
    if not os.path.exists(db_path):
        print("[preflight] 数据库不存在，跳过体检：%s" % db_path)
        return
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    changed = False
    try:
        child, col, parent, pk = LOGS_FK
        if table_exists(conn, child) and table_exists(conn, parent):
            n = conn.execute(
                """
                SELECT COUNT(*) FROM {c}
                WHERE {col} IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM {p} WHERE {p}.{pk} = {c}.{col})
                """.format(c=child, col=col, p=parent, pk=pk)
            ).fetchone()[0]
            if n:
                conn.execute(
                    """
                    UPDATE {c} SET {col}=NULL
                    WHERE {col} IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM {p} WHERE {p}.{pk} = {c}.{col})
                    """.format(c=child, col=col, p=parent, pk=pk)
                )
                print("[preflight] logs: %d 条孤儿 %s 已置 NULL" % (n, col))
                changed = True

        for child, col, parent, pk in FK_RELATIONS:
            if not table_exists(conn, child) or not table_exists(conn, parent):
                continue
            n = conn.execute(
                """
                SELECT COUNT(*) FROM {c}
                WHERE NOT EXISTS (SELECT 1 FROM {p} WHERE {p}.{pk} = {c}.{col})
                """.format(c=child, col=col, p=parent, pk=pk)
            ).fetchone()[0]
            if n:
                conn.execute(
                    """
                    DELETE FROM {c}
                    WHERE NOT EXISTS (SELECT 1 FROM {p} WHERE {p}.{pk} = {c}.{col})
                    """.format(c=child, col=col, p=parent, pk=pk)
                )
                print("[preflight] %s: 已删除 %d 行孤儿记录" % (child, n))
                changed = True

        if changed:
            conn.commit()
        else:
            print("[preflight] 数据库健康，无孤儿外键")

        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        print("[preflight] foreign_key_check 违规行：%d" % len(bad))
    except Exception as exc:
        print("[preflight] 数据库体检异常（已忽略）：%r" % (exc,))
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def sync_deps(venv_dir, install_log):
    req = os.path.join(os.path.dirname(venv_dir), "requirements.txt")
    if not os.path.exists(req):
        print("[preflight] 无 requirements.txt，跳过依赖检查")
        return

    venv_py = os.path.join(venv_dir, "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        venv_py = os.path.join(venv_dir, "bin", "python")
    if not os.path.exists(venv_py):
        print("[preflight] 虚拟环境尚未创建，交由启动脚本处理")
        return

    try:
        with open(req, "rb") as fh:
            digest = hashlib.md5(fh.read()).hexdigest()
    except Exception as exc:
        print("[preflight] 读取 requirements.txt 失败：%r" % (exc,))
        return

    stamp = os.path.join(venv_dir, ".requirements.md5")
    try:
        if os.path.exists(stamp):
            with open(stamp, "r", encoding="utf-8") as fh:
                if fh.read().strip() == digest:
                    print("[preflight] 依赖已是最新，跳过安装")
                    return
    except Exception:
        pass

    print("[preflight] requirements.txt 已变化，正在同步依赖 ...")
    try:
        with open(install_log, "a", encoding="utf-8", errors="replace") as log:
            log.write("\n===== %s pip install -r requirements.txt =====\n" % req)
            proc = subprocess.run(
                [venv_py, "-m", "pip", "install", "-r", req],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
                cwd=os.path.dirname(venv_dir),
            )
        if proc.returncode == 0:
            with open(stamp, "w", encoding="utf-8") as fh:
                fh.write(digest)
            print("[preflight] 依赖同步完成")
        else:
            print("[preflight] 依赖同步失败(returncode=%s)，详见 %s"
                  % (proc.returncode, install_log))
    except Exception as exc:
        print("[preflight] 依赖同步异常（已忽略）：%r" % (exc,))


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    venv_dir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VENV
    install_log = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        HERE, "logs", "buddy-install.log"
    )
    fix_db(db_path)
    sync_deps(venv_dir, install_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
