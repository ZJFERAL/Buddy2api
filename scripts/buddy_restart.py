"""Buddy2api 重启助手：停掉旧实例 → 按 start-oneclick.bat 的配置起新实例 → 等健康。

为什么需要它：
- 改完 provider/server 代码必须重启才生效，而手工重启（关窗口 / 重跑 bat）容易
  踩到「端口被占」或「新进程随终端退出」。
- 关键参数（端口 / 监听地址 / admin-token）以 start-oneclick.bat 为唯一事实来源，
  避免命令行里手打 token 打错导致管理页登录不上。

用法：
    .venv/Scripts/python.exe scripts/buddy_restart.py            # 重启并等健康
    .venv/Scripts/python.exe scripts/buddy_restart.py --status   # 只看状态
    .venv/Scripts/python.exe scripts/buddy_restart.py --no-kill  # 只起（端口空闲时）
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(REPO, "start-oneclick.bat")
LOG = os.path.join(REPO, "logs", "buddy.log")
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")


def read_bat_config() -> dict:
    """从 start-oneclick.bat 取端口 / 主机 / admin-token。"""
    with open(BAT, "r", encoding="gbk", errors="ignore") as fh:
        text = fh.read()
    cfg: dict = {}
    for line in text.splitlines():
        line = line.strip()
        for key, name in (("BUDDY_PORT", "port"), ("BUDDY_HOST", "host")):
            m = re.match(rf'set "{key}=([^"]*)"', line, re.I)
            if m:
                cfg[name] = m.group(1)
        m = re.match(r'set "BUDDY_AUTH=--admin-token\s+(\S+)"', line, re.I)
        if m:
            cfg["token"] = m.group(1)
    cfg.setdefault("port", "8787")
    cfg.setdefault("host", "0.0.0.0")
    return cfg


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _capture(cmd: list[str]) -> str:
    """跑命令拿 stdout。沙箱下 stdout 可能为 None / 编码为 GBK，故双通道兜底。"""
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
        out = _decode(res.stdout)
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with os.popen(" ".join(cmd), "rb") as pipe:
            return _decode(pipe.read())
    except OSError:
        return ""


def listening_pid(port: str) -> int | None:
    for line in _capture(["netstat", "-ano"]).splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def http_status(url: str, timeout: float = 5.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.status
    except urllib.error.HTTPError as err:
        return err.code
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只报告状态")
    ap.add_argument("--no-kill", action="store_true", help="不动旧进程")
    ap.add_argument(
        "--foreground",
        action="store_true",
        help="用当前进程接管服务（配合后台任务托管；分离启动在部分沙箱下 socket 会被回收）",
    )
    ap.add_argument("--wait", type=int, default=60, help="健康等待上限（秒）")
    args = ap.parse_args()

    cfg = read_bat_config()
    base = f"http://127.0.0.1:{cfg['port']}"
    pid = listening_pid(cfg["port"])
    print(f"[config] port={cfg['port']} host={cfg['host']} token={(cfg.get('token') or '')[:14]}…")
    print(f"[state ] pid={pid} health={http_status(base + '/')}")

    if args.status:
        return 0

    if pid and not args.no_kill:
        print(f"[stop  ] taskkill -PID {pid} -F")
        res = subprocess.run(
            ["taskkill", "-PID", str(pid), "-F"], capture_output=True, text=True
        )
        print("[stop  ] " + (res.stdout or res.stderr).strip().replace("\n", " | "))
        for _ in range(40):
            if listening_pid(cfg["port"]) is None:
                break
            time.sleep(0.25)
        else:
            print("[stop  ] 端口仍被占用，放弃启动")
            return 1
    elif pid:
        print("[start ] 端口已被占用（--no-kill），跳过启动")
        return 0

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    cmd = [PY, os.path.join(REPO, "server.py"), "--port", cfg["port"], "--host", cfg["host"]]
    if cfg.get("token"):
        cmd += ["--admin-token", cfg["token"]]
    if args.foreground:
        # 用 runpy 在当前进程内起服务：进程不换壳，后台任务托管才能正确跟踪/保活
        # （用 os.execv 替换进程映像会让托管方以为任务已结束，随即回收整棵进程树）
        import runpy

        os.chdir(REPO)
        sys.path.insert(0, REPO)  # runpy.run_path 不会自动把脚本目录加进 sys.path
        sys.argv = [os.path.join(REPO, "server.py"), "--port", cfg["port"], "--host", cfg["host"]]
        if cfg.get("token"):
            sys.argv += ["--admin-token", cfg["token"]]
        print(f"[start ] 前台接管：--port {cfg['port']} --host {cfg['host']}（日志见 logs/buddy.log）")
        sys.stdout.flush()
        runpy.run_path(os.path.join(REPO, "server.py"), run_name="__main__")
        return 0
    log_fh = open(LOG, "a", encoding="utf-8", errors="ignore")
    log_fh.write(f"\n[restart {time.strftime('%Y-%m-%d %H:%M:%S')}] {' '.join(cmd[:4])} …\n")
    log_fh.flush()
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    proc = subprocess.Popen(
        cmd, cwd=REPO, stdout=log_fh, stderr=subprocess.STDOUT, creationflags=flags
    )
    print(f"[start ] pid={proc.pid} → {base}/  日志 logs/buddy.log")

    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[fail  ] 进程已退出（exit={proc.returncode}），看 logs/buddy.log 末尾")
            return 1
        code = http_status(base + "/", timeout=3)
        if code == 200:
            new_pid = listening_pid(cfg["port"])
            print(f"[ready ] pid={new_pid} health={code} 用时 {int(args.wait - (deadline - time.time()))}s")
            return 0
        time.sleep(1)
    print("[fail  ] 健康检查超时")
    return 1


if __name__ == "__main__":
    sys.exit(main())
