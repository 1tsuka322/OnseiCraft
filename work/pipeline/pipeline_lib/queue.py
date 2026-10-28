# -*- coding: utf-8 -*-
"""文件锁 + GPU 串行队列（8GB 单卡：ASR / 翻译 / 润色互斥）。

为什么用文件锁而不是「注意别同时跑」：whisper 与 7B 共存会 OOM，
而 OOM 后的失败看起来像「模型输出坏了」，历史上浪费过多轮。锁是硬约束。

## 锁语义（2026-10-26 按 GPT 评审重写）

- 锁文件 = `{token, pid, job, since}`，`token` 每次获取随机生成。
- **释放只删自己的锁**：退出时先读锁，`token` 相等才 `os.remove`。
  否则会出现「A 判定过期 → B 抢到新锁 → A 退出时把 B 的锁删了」。
- **清理过期锁用原子改名**：`os.replace(lock, lock.stale.<uuid>)`，只删改名后的旧文件。
  这样即使同时有别人在建锁，也不会误删活锁。
- **PID 存活就绝不抢**（哪怕超过 24 小时）。24 小时只是「PID 未知/跨机残留」时的兜底判据。
- 子进程在 `KIKOERU_GPU_LOCK_HELD=1` 下不再抢锁（队列已持锁，避免自锁死）。
"""
import json
import os
import subprocess
import sys
import time
import uuid

from . import paths

LOCK = os.path.join(paths.QUEUE, "gpu.lock")
JOBS = os.path.join(paths.QUEUE, "jobs")
STALE_SEC = 24 * 3600


def _pid_alive(pid):
    if not pid:
        return False
    try:
        # tasklist 输出是本地代码页（中文 Windows = GBK），不指定 errors 会在
        # 读取线程里抛 UnicodeDecodeError（噪声，且会走进 except 兜底分支）。
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid)],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15).stdout
        return str(pid) in out
    except Exception:
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False


class FileLock(object):
    """带 token 的独占文件锁。抢不到就退出（退出码 2），不排队等。"""

    def __init__(self, path, job="pipeline", steal_stale=True):
        self.path = path
        self.job = job
        self.steal = steal_stale
        self.token = uuid.uuid4().hex
        self.held = False

    def _read(self):
        try:
            return json.load(open(self.path, encoding="utf-8"))
        except Exception:
            return {}

    def __enter__(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        for _ in range(4):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"token": self.token, "pid": os.getpid(),
                               "job": self.job,
                               "since": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
                self.held = True
                return self
            except FileExistsError:
                info = self._read()
                pid = info.get("pid")
                alive = _pid_alive(pid)
                age = time.time() - os.path.getmtime(self.path)
                # PID 存活 → 绝不抢（年龄只用于 PID 不可判时的兜底）
                if alive or not self.steal:
                    print("[lock] 被占用：job=%s pid=%s since=%s（%.0f 分钟）"
                          % (info.get("job"), pid, info.get("since"), age / 60))
                    print("       等它结束；确认进程已死再删 %s" % self.path)
                    raise SystemExit(2)
                if pid and age < STALE_SEC:
                    # pid 判死但锁很新：可能是别人刚建的瞬时窗口，退一步重试
                    time.sleep(0.2)
                    continue
                stale = "%s.stale.%s" % (self.path, uuid.uuid4().hex)
                try:
                    os.replace(self.path, stale)     # 原子夺走，不碰新锁
                except OSError:
                    continue
                print("[lock] 清理过期锁（pid=%s，%.0f 分钟前）"
                      % (pid, age / 60))
                try:
                    os.remove(stale)
                except OSError:
                    pass
                continue
        print("[lock] 抢锁失败：%s" % self.path)
        raise SystemExit(2)

    def __exit__(self, *a):
        if not self.held:
            return False
        if self._read().get("token") == self.token:
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.held = False
        return False


class GpuLock(FileLock):
    def __init__(self, job="pipeline"):
        FileLock.__init__(self, LOCK, job)

    def __enter__(self):
        # 外层（队列）已持锁时不再抢：同进程内嵌套加锁会自锁死。
        if os.environ.get("KIKOERU_GPU_LOCK_HELD") == "1":
            self.held = False
            return self
        return FileLock.__enter__(self)


def lock_status():
    if not os.path.exists(LOCK):
        return None
    try:
        d = json.load(open(LOCK, encoding="utf-8"))
    except Exception:
        d = {}
    d["age_sec"] = round(time.time() - os.path.getmtime(LOCK), 1)
    d["alive"] = _pid_alive(d.get("pid"))
    return d


# ---------------------------------------------------------------- 队列
# ⚠️ 队列是实验层（GPT 评审建议：单人本地先靠「直接执行 + GPU 锁」就够）。
# 保留是因为它已跑通，但**不要在它上面堆关键语义**；核心是 run/阶段/锁。
def _job_path(jid):
    return os.path.join(JOBS, jid + ".json")


def add(argv, gpu=True, note=""):
    os.makedirs(JOBS, exist_ok=True)
    jid = "%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    job = {"id": jid, "argv": argv, "gpu": bool(gpu), "note": note,
           "status": "queued", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "pid": None, "exit": None, "log": os.path.join(paths.QUEUE, "logs", jid + ".log")}
    fd = os.open(_job_path(jid), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    print("已入队 %s（%s）%s" % (jid, "GPU" if gpu else "CPU", " ".join(argv)))
    return jid


def jobs():
    if not os.path.isdir(JOBS):
        return []
    out = []
    for f in sorted(os.listdir(JOBS)):
        if f.endswith(".json"):
            try:
                out.append(json.load(open(os.path.join(JOBS, f), encoding="utf-8")))
            except Exception:
                pass
    return out


def run_queue(limit=0):
    """串行执行队列。GPU 任务持锁；失败即停（不掩盖错误）。"""
    os.makedirs(os.path.join(paths.QUEUE, "logs"), exist_ok=True)
    done = 0
    for job in jobs():
        if job["status"] not in ("queued",):
            continue
        if limit and done >= limit:
            break
        logp = job["log"]
        print("\n[queue] 开始 %s：%s" % (job["id"], " ".join(job["argv"])))
        job["status"] = "running"
        job["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(job, open(_job_path(job["id"]), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        t0 = time.time()
        with open(logp, "a", encoding="utf-8") as lf:
            ctx = GpuLock("queue:" + job["id"]) if job["gpu"] else _Null()
            try:
                with ctx:
                    if job["gpu"]:
                        os.environ["KIKOERU_GPU_LOCK_HELD"] = "1"
                    p = subprocess.Popen(job["argv"], stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", bufsize=1)
                    job["pid"] = p.pid
                    for line in p.stdout:
                        try:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                        except (BrokenPipeError, OSError):
                            pass          # 管道被截断（| head）不算任务失败
                        lf.write(line)
                    p.wait()
                    code = p.returncode
            except SystemExit as e:
                lf.write(str(e) + "\n")
                code = 99
            finally:
                os.environ.pop("KIKOERU_GPU_LOCK_HELD", None)
        job["exit"] = code
        job["status"] = "ok" if code == 0 else "failed"
        job["duration_sec"] = round(time.time() - t0, 1)
        job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(job, open(_job_path(job["id"]), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        done += 1
        print("[queue] %s → %s（%.1f 分钟）" % (job["id"], job["status"],
                                              job["duration_sec"] / 60))
        if code != 0:
            print("[queue] 停止：失败任务 %s，后面的不动（避免在错状态上继续）" % job["id"])
            break
    return done


class _Null(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def status():
    st = lock_status()
    print("GPU 锁：%s" % ("空闲" if not st else
                       "被占用 job=%s pid=%s（%.0f 分钟，进程%s）"
                       % (st.get("job"), st.get("pid"), st["age_sec"] / 60,
                          "存活" if st.get("alive") else "已死→可清理")))
    js = jobs()
    if not js:
        print("队列：空")
        return
    print("队列 %d 项：" % len(js))
    for j in js:
        print("  %-24s %-8s %s%s" % (j["id"], j["status"],
                                     " ".join(j["argv"])[:80],
                                     "" if j.get("exit") is None else "  exit=%s" % j["exit"]))
