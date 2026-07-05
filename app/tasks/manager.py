"""五队列任务管理器：后台跑、可暂停、关服恢复、刷新可见进度。

五队列：
  index  扫路径、对账新增/删除
  thumb  生成缩略图、套 LUT
  text   文本索引和轻量候选处理
  ai     视觉打标
  export 复制原素材、渲染导出图、生成清单

并发：本地 AI 默认 1 线（强机 2）/ API AI 默认 3 线（高速 5）；缩略图 2–4 线；
文本/导出可多线。暂停 AI 不影响 扫盘/缩略图/文本/导出。

持久化：tasks + task_items。刷新页面任务继续；关服当前项中断、已完成不丢；
再开把残留 running 视为可恢复，点继续只处理 剩余/失败/中断项。
"""
from __future__ import annotations
import threading
import time
import traceback
from queue import Queue, Empty

from app import db
from app.config import get_cfg
from app.core.ids import new_id

QUEUES = ["index", "thumb", "text", "ai", "export"]


def _workers(queue: str) -> int:
    cfg = get_cfg()
    return {
        "index": cfg.workers_index,
        "thumb": cfg.workers_thumb,
        "text": cfg.workers_text,
        "ai": cfg.ai_workers(),
        "export": cfg.workers_export,
    }.get(queue, 1)


class TaskManager:
    def __init__(self):
        self.handlers: dict[str, callable] = {}
        self.finalizers: dict[str, callable] = {}
        self.ai_paused = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False

    # ── 注册某 kind 的"处理一个 item"与可选收尾 ──
    def register(self, kind: str, handler, finalize=None):
        self.handlers[kind] = handler
        if finalize:
            self.finalizers[kind] = finalize

    def start(self):
        if self._started:
            return
        self._started = True
        db.init_db()
        # 恢复：上次没跑完的 running → pending（继续只处理剩余/失败项）
        def _recover(conn):
            conn.execute("UPDATE tasks SET status='pending' WHERE status='running'")
            conn.execute("UPDATE task_items SET status='pending' WHERE status='running'")
        db.write(_recover)
        for q in QUEUES:
            t = threading.Thread(target=self._pump, args=(q,), daemon=True, name=f"pump-{q}")
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()

    # ── 每个队列一个泵线程：依次取该队列的待处理任务并执行 ──
    def _pump(self, queue: str):
        """泵线程是常驻基础设施：任何异常都不允许带死线程（旧版 _next_task 裸奔在
        try 外面，重置数据库的连接竞态曾把 ai 泵打死——表现为打标永远不开始，重启才好）。"""
        while not self._stop.is_set():
            task = None
            try:
                task = self._next_task(queue)
                if not task:
                    time.sleep(0.5)
                    continue
                self._run_task(task)
            except Exception:
                traceback.print_exc()
                if task:
                    try:
                        self._set_status(task["task_id"], "failed",
                                         error=traceback.format_exc()[-500:])
                    except Exception:
                        pass
                time.sleep(1.0)   # 异常后歇一拍再继续轮询，绝不退出

    def _next_task(self, queue: str) -> dict | None:
        conn = db.connect()
        r = conn.execute(
            "SELECT * FROM tasks WHERE queue=? AND status IN ('pending','running') ORDER BY created_at LIMIT 1",
            (queue,),
        ).fetchone()
        return dict(r) if r else None

    def _run_task(self, task: dict):
        tid = task["task_id"]
        queue = task["queue"]
        kind = task["kind"]
        handler = self.handlers.get(kind)
        if not handler:
            self._set_status(tid, "failed", error=f"无处理器: {kind}")
            return
        self._set_status(tid, "running")
        conn = db.connect()
        pending = [r[0] for r in conn.execute(
            "SELECT item_key FROM task_items WHERE task_id=? AND status!='done'", (tid,)).fetchall()]
        if not pending:
            self._complete(task)
            return

        q: Queue = Queue()
        for k in pending:
            q.put(k)
        n = max(1, _workers(queue))
        cancelled = threading.Event()

        def worker():
            while not self._stop.is_set() and not cancelled.is_set():
                # 暂停 AI：只挡 ai 队列，不消费、不报错，等恢复
                if queue == "ai" and self.ai_paused.is_set():
                    time.sleep(0.4)
                    continue
                try:
                    st = self._task_status(tid)
                except Exception:   # 瞬时连接竞态不杀 worker，歇一拍重查
                    time.sleep(0.5)
                    continue
                if st == "paused":
                    break   # 退出本任务（保留进度），泵会接着跑同队列的下一个任务；恢复后泵再回来
                if st in ("cancelled",):
                    cancelled.set()
                    break
                try:
                    item = q.get_nowait()
                except Empty:
                    break
                self._run_item(task, item)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        st = self._task_status(tid)
        if st == "cancelled":
            return
        if st == "paused":
            return   # 留着，恢复后泵会再进来
        # 还有剩余（被 stop 打断）→ 留 pending 下次继续
        remaining = conn.execute(
            "SELECT COUNT(*) FROM task_items WHERE task_id=? AND status='pending'", (tid,)).fetchone()[0]
        if remaining and self._stop.is_set():
            self._set_status(tid, "pending")
            return
        self._complete(task)

    def _run_item(self, task: dict, item_key: str):
        tid = task["task_id"]
        handler = self.handlers[task["kind"]]
        try:
            self._mark_item(tid, item_key, "running")
            handler(task, item_key)
            self._mark_item(tid, item_key, "done")
            self._bump(tid, "done")
        except Exception as e:
            self._mark_item(tid, item_key, "failed", str(e)[:300])
            self._bump(tid, "failed")

    def _complete(self, task: dict):
        tid = task["task_id"]
        fin = self.finalizers.get(task["kind"])
        if fin:
            try:
                fin(task)
            except Exception:
                traceback.print_exc()
        self._set_status(tid, "done")

    # ── 状态/计数小工具（带锁，避免多 worker 抢更新）──
    def _bump(self, tid: str, field: str):
        with self._lock:
            db.write(lambda conn: conn.execute(
                f"UPDATE tasks SET {field}={field}+1, updated_at=? WHERE task_id=?",
                (db.now(), tid),
            ))

    def _mark_item(self, tid: str, key: str, status: str, error: str | None = None):
        with self._lock:
            db.write(lambda conn: conn.execute(
                "UPDATE task_items SET status=?, error=? WHERE task_id=? AND item_key=?",
                (status, error, tid, key),
            ))

    def _set_status(self, tid: str, status: str, error: str | None = None):
        db.write(lambda conn: conn.execute(
            "UPDATE tasks SET status=?, error=COALESCE(?,error), updated_at=? WHERE task_id=?",
            (status, error, db.now(), tid),
        ))

    def _task_status(self, tid: str) -> str:
        r = db.connect().execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        return r[0] if r else "done"


MANAGER = TaskManager()


# ── 对外便捷函数 ──
def register(kind: str, handler, finalize=None):
    MANAGER.register(kind, handler, finalize)


def create_task(queue: str, kind: str, title: str, items: list[str],
                scope: dict | None = None, params: dict | None = None) -> str:
    if queue not in QUEUES:
        raise ValueError(f"未知队列: {queue}")
    tid = new_id("task")
    items = [str(i) for i in (items or [])]

    def _w(conn):
        t = db.now()
        conn.execute(
            "INSERT INTO tasks(task_id,queue,kind,title,scope_json,params_json,total,done,failed,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,0,0,?,?,?)",
            (tid, queue, kind, title, db.jdumps(scope or {}), db.jdumps(params or {}),
             len(items), "done" if not items else "pending", t, t),
        )
        conn.executemany("INSERT OR IGNORE INTO task_items(task_id,item_key,status) VALUES (?,?,'pending')",
                         [(tid, k) for k in items])

    db.write(_w)
    return tid


def snapshot() -> dict:
    conn = db.connect()
    rows = conn.execute(
        "SELECT task_id,queue,kind,title,total,done,failed,status FROM tasks "
        "WHERE status IN ('pending','running','paused') ORDER BY created_at"
    ).fetchall()
    tasks = []
    for r in rows:
        d = dict(r)
        cur = conn.execute(
            "SELECT item_key FROM task_items WHERE task_id=? AND status='running' LIMIT 1",
            (d["task_id"],),
        ).fetchone()
        d["current"] = cur[0] if cur else ""
        tasks.append(d)
    # 队列汇总
    roll = {}
    for q in QUEUES:
        agg = conn.execute(
            "SELECT COALESCE(SUM(total),0), COALESCE(SUM(done),0) FROM tasks "
            "WHERE queue=? AND status IN ('pending','running','paused')", (q,)).fetchone()
        roll[q] = {"total": agg[0], "done": agg[1]}
    return {"tasks": tasks, "rollup": roll, "active": len(tasks),
            "ai_paused": MANAGER.ai_paused.is_set()}


def pause_ai():
    MANAGER.ai_paused.set()


def resume_ai():
    MANAGER.ai_paused.clear()


def pause_task(tid: str):
    MANAGER._set_status(tid, "paused")


def resume_task(tid: str):
    MANAGER._set_status(tid, "pending")


def cancel_task(tid: str):
    MANAGER._set_status(tid, "cancelled")
