"""全局调度：跨任务共享的模型限流（速率+在途）+ 预处理并发 + 运行时设置。

两级闸门，获取顺序恒为 PIPELINE → MODEL，无反向嵌套：
- PIPELINE_LIMITER（上限 = max_in_flight + waiting_capacity）：流水线准入，
  整题（预处理 + 等模型槽 + 模型调用）持有——界定「抽帧中 + 已抽帧等模型」
  ≤ waiting_capacity、在途 ≤ max_in_flight，从而已准备好的关键帧总内存被封顶
  （防止抽完帧堆在模型闸前等太久导致内存暴涨）。已带 frames 的重跑题不走预处理，
  仅等模型、仍可占名额。
- MODEL_LIMITER（速率 = concurrency 令牌/每 RATE_WINDOW_S 秒 + 最大在途 =
  max_in_flight）：模型调用级限流，每次实际模型请求（含重试的每一次尝试）
  同时满足「一个速率令牌」与「一个在途名额」才发出——速率限制每窗口请求数
  （默认窗口 2 秒），在途上限防范模型服务商的在途并发限制。组第 2+ 轮 / 批
  第 2+ 条首次尝试 priority=True 插队头。

速率（concurrency）、最大在途（max_in_flight）、预处理并发（waiting_capacity）、
单题超时与裁判由 GET/PUT /api/settings 全局管理，持久化到
runs/web_settings.json（runs/ 不入库，重启后加载）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import RUNS_DIR


logger = logging.getLogger(__name__)

SETTINGS_PATH = RUNS_DIR / "web_settings.json"
DEFAULT_CONCURRENCY = 10  # 模型调用速率限流：每 2 秒至多 10 次请求
RATE_WINDOW_S = 2.0  # 速率限流窗口：concurrency 表示「每窗口窗口内 N 次」
DEFAULT_MAX_IN_FLIGHT = 50  # 模型调用最大在途上限（模型服务商有在途并发限制）
DEFAULT_WAITING_CAPACITY = 10  # 视频预处理并发上限：同时抽帧的评测数
DEFAULT_EVAL_TIMEOUT_S = 300.0
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 64
MIN_MAX_IN_FLIGHT, MAX_MAX_IN_FLIGHT = 1, 1024
MIN_WAITING_CAPACITY, MAX_WAITING_CAPACITY = 0, 256
MIN_EVAL_TIMEOUT_S, MAX_EVAL_TIMEOUT_S = 30.0, 3600.0


@dataclass
class RuntimeSettings:
    concurrency: int = DEFAULT_CONCURRENCY
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT  # 模型在途上限（模型服务商限制）
    waiting_capacity: int = DEFAULT_WAITING_CAPACITY
    eval_timeout_s: float = DEFAULT_EVAL_TIMEOUT_S
    # 裁判名列表（本模块不感知 config，只存原始名字；空 = 未设置，由
    # 调用方回落到配置的第一个裁判）。合法性校验在 server PUT 侧做。
    judges: list[str] = field(default_factory=list)


class ResizableLimiter:
    """FIFO 全局并发槽；运行中可调上限。

    不用 asyncio.Semaphore/Condition：它们在首次挂起时绑定事件循环，
    pytest-asyncio 每个测试新建循环会报 "bound to a different event loop"。
    本实现的等待 future 每次 acquire 现取 running loop 创建，跨循环安全。

    调大上限立即唤醒队头等待者；调小为软限制——不抢占运行中的任务，
    新的 acquire 按 `running < limit` 门控，运行完自然收敛。

    acquire(priority=True) 把等待者插到队头（_wake 的 popleft 天然先放行），
    供 session 组第 2+ 轮 / 批第 2+ 条优先续队：不排到新到达者之后。多个
    优先者之间为 LIFO——每组同一时刻至多一个优先等待者（组内轮次串行），
    有界可接受。取消清理（按 future 恒等出队 / 已预占则归还）与队列位置
    无关，priority 路径与普通路径同等安全。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._running = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    def stats(self) -> dict:
        return {
            "limit": self._limit,
            "running": self._running,
            "queued": sum(1 for w in self._waiters if not w.done()),
        }

    def set_limit(self, limit: int) -> None:
        new = max(1, int(limit))
        if new != self._limit:
            self._limit = new
            self._wake()  # 调大时立刻放行队头；调小时 _wake 自行按新上限门控

    def _wake(self) -> None:
        while self._waiters and self._running < self._limit:
            waiter = self._waiters.popleft()
            if waiter.done():  # 已被取消的滞留者
                continue
            # 先预占槽位再放行，防止连续唤醒超发
            self._running += 1
            waiter.set_result(None)

    async def acquire(self, *, priority: bool = False) -> None:
        if not self._waiters and self._running < self._limit:
            # 有等待者时禁止插队，保证 FIFO（显式 priority 除外）
            self._running += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(waiter)
        else:
            self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.cancelled() or not waiter.done():
                # 从未取得槽位：仅出队
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            else:
                # _wake 已替本协程预占槽位但协程仍被取消：必须归还，
                # 否则计数泄漏、后续请求被永久少一个槽
                self.release()
            raise

    def release(self) -> None:
        self._running = max(0, self._running - 1)
        self._wake()

    def would_block(self) -> bool:
        """现在 acquire 是否需要排队（供展示层决定是否发「等待槽位」事件）。"""
        return bool(self._waiters) or self._running >= self._limit

    async def __aenter__(self) -> "ResizableLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        """带参数的槽位 async CM（`async with lim:` 语法无法给 __aenter__ 传参）。"""
        return _LimiterSlot(self, priority)


class _LimiterSlot:
    def __init__(self, limiter: ResizableLimiter, priority: bool) -> None:
        self._limiter = limiter
        self._priority = priority

    async def __aenter__(self) -> ResizableLimiter:
        await self._limiter.acquire(priority=self._priority)
        return self._limiter

    async def __aexit__(self, *exc) -> None:
        self._limiter.release()


class TokenBucketRateLimiter:
    """模型调用级限流：令牌桶速率（每 RATE_WINDOW_S 秒 `rate` 个，突发 = rate）
    + 最大在途上限。

    每次实际模型请求（含重试的每一次 `_eval_one`）在 acquire 时须同时满足：
    - 一个速率令牌（每 RATE_WINDOW_S 秒生成 `rate` 个，折算每秒 rate/窗口）；
    - 一个在途名额（`in_flight < max_in_flight`，模型服务商有在途并发限制）。

    两者都满足才会发出请求：`_in_flight` 即当前在途的模型调用数，随请求开始
    `+1`、随请求结束 `release()` `-1`。acquire 申请到即返回；rate 不足时按率
    等待，在途满时等 `release()` 释放名额。

    实现：快路径同时校验令牌与在途名额；否则压入等待队列（priority=True 经
    appendleft 插队头）。`_refill_loop` 按 rate 间隔喂令牌；`release()` 释放
    在途名额后立即 `_pump` 唤醒因名额等待的队头。取消清理按 future 恒等出队，
    跨事件循环安全。set_rate / set_max_in_flight 调大立即放行、调小软生效。
    """

    def __init__(
        self,
        rate: int,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        self._rate = max(1, int(rate))  # 每窗口内的令牌数（窗口 = RATE_WINDOW_S）
        self._rate_per_sec = self._rate / RATE_WINDOW_S  # 折算每秒补币速率
        self._max_in_flight = max(1, int(max_in_flight))
        self._tokens = float(self._rate)  # 突发 = 整窗口额度；时间制，无持有
        self._last: float | None = None  # 上次补币的 loop 时间戳（None=未初始化）
        self._in_flight = 0  # 当前在途模型调用数（受 max_in_flight 约束）
        self._waiters: deque[asyncio.Future] = deque()
        self._minter: asyncio.Task | None = None

    @property
    def limit(self) -> int:
        return self._rate

    def stats(self) -> dict:
        return {
            "limit": self._rate,
            "running": self._in_flight,
            "queued": sum(1 for w in self._waiters if not w.done()),
            "max_in_flight": self._max_in_flight,
        }

    def set_rate(self, rate: int) -> None:
        new = max(1, int(rate))
        if new != self._rate:
            self._rate = new
            self._rate_per_sec = new / RATE_WINDOW_S
            # 已有等待者时立即泵送一次：新 rate 可能让存量令牌立即可喂
            self._pump(_loop_time())
            self._ensure_minter()

    def set_max_in_flight(self, cap: int) -> None:
        new = max(1, int(cap))
        if new != self._max_in_flight:
            self._max_in_flight = new
            self._pump(_loop_time())  # 调大立即放行因在途满而等待的队头
            self._ensure_minter()

    def would_block(self) -> bool:
        """现在 acquire 是否需等待（无令牌 或 在途满 或 已有排队者）。"""
        return (
            self._tokens < 1.0
            or self._in_flight >= self._max_in_flight
            or bool(self._waiters)
        )

    def _servable(self) -> bool:
        return self._tokens >= 1.0 and self._in_flight < self._max_in_flight

    def _refill(self, now: float) -> None:
        if self._last is None:
            self._last = now  # 首次基线：只记录时间戳，不凭空补突发
        elapsed = now - self._last
        if elapsed > 0:
            # 每秒补 rate_per_sec 个（= 每 RATE_WINDOW_S 秒补 rate 个）
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate_per_sec)
        self._last = now

    def _feed_front(self) -> None:
        """贪婪喂给队头等待者：每喂一个占一个令牌与一个在途名额。"""
        while self._waiters and self._servable():
            waiter = self._waiters[0]
            if waiter.done():  # 已被取消清理的滞留者
                self._waiters.popleft()
                continue
            self._waiters.popleft()
            self._tokens -= 1.0
            self._in_flight += 1
            waiter.set_result(None)

    def _pump(self, now: float) -> None:
        self._refill(now)
        self._feed_front()

    def _ensure_minter(self) -> None:
        if self._waiters and (
            self._minter is None or self._minter.done() or self._minter.cancelled()
        ):
            self._minter = asyncio.create_task(self._refill_loop())

    async def acquire(self, *, priority: bool = False) -> None:
        if not self._waiters and self._servable():
            # 快路径：令牌足够、在途有名额且无人排队，直接消费
            self._tokens -= 1.0
            self._in_flight += 1
            return
        fut = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(fut)
        else:
            self._waiters.append(fut)
        self._ensure_minter()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.cancelled() or not fut.done():
                # 从未取得令牌/名额：仅出队（minter/release 也未喂，未计在途）
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            else:
                # 竞态兜底：已喂令牌+名额但协程被取消——归还展示计数
                self._in_flight = max(0, self._in_flight - 1)
            raise

    async def _refill_loop(self) -> None:
        """按 rate 把令牌喂给队头等待者；队列清空即退出。"""
        while self._waiters:
            now = asyncio.get_running_loop().time()
            self._pump(now)
            if not self._waiters:
                return
            if self._in_flight >= self._max_in_flight:
                # 在途满：只等 release() 释放名额后泵送；这里短轮询兜底防漏唤醒
                await asyncio.sleep(0.02)
                continue
            if self._tokens < 1.0:
                await asyncio.sleep((1.0 - self._tokens) / self._rate_per_sec)
                continue  # 循环顶部重补币；rate/priority 可随后续请求变化
            await asyncio.sleep(0)  # _servable 已满足但未喂到（理论不会到这里）

    def release(self) -> None:
        """请求结束：释放一个在途名额并立即唤醒因在途满而等待的队头。"""
        self._in_flight = max(0, self._in_flight - 1)
        self._pump(_loop_time())
        self._ensure_minter()

    async def __aenter__(self) -> "TokenBucketRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        return _LimiterSlot(self, priority)


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return 0.0


MODEL_LIMITER: ResizableLimiter | TokenBucketRateLimiter = TokenBucketRateLimiter(
    DEFAULT_CONCURRENCY, DEFAULT_MAX_IN_FLIGHT
)
PIPELINE_LIMITER = ResizableLimiter(
    DEFAULT_MAX_IN_FLIGHT + DEFAULT_WAITING_CAPACITY
)

_settings = RuntimeSettings()


def get_settings() -> RuntimeSettings:
    return _settings


def _sync_pipeline_limit() -> None:
    """流水线准入 = 最大在途 + 预处理等待容量；整题持有，封顶关键帧内存。"""
    PIPELINE_LIMITER.set_limit(
        max(1, _settings.max_in_flight + _settings.waiting_capacity)
    )


def apply_settings(
    *,
    concurrency: int | None = None,
    max_in_flight: int | None = None,
    waiting_capacity: int | None = None,
    eval_timeout_s: float | None = None,
    judges: list[str] | None = None,
) -> RuntimeSettings:
    """更新运行时设置并即时对齐限流器（越界值 clamp 到合法区间）。

    concurrency = 模型调用速率（每 RATE_WINDOW_S 秒的令牌数）→ MODEL_LIMITER.set_rate；
    max_in_flight = 模型最大在途（模型服务商并发限制）→ MODEL_LIMITER.set_max_in_flight；
    waiting_capacity = 「预处理并发 + 预处理后等模型」的额度 → PIPELINE_LIMITER。
    流水线准入 = max_in_flight + waiting_capacity（整题持有至模型结束），
    从而在途 ≤ max_in_flight、「抽帧中 + 已抽帧等模型」≤ waiting_capacity——
    关键帧内存被该名额封顶。max_in_flight 或 waiting_capacity 任一变化都重算
    PIPELINE 上限。waiting_capacity=0 时 pipeline = max_in_flight（最严不排队，
    预处理也要等模型释放，ResizableLimiter 再 clamp 到 >=1）。
    """
    global _settings
    if concurrency is not None:
        _settings.concurrency = min(
            MAX_CONCURRENCY, max(MIN_CONCURRENCY, int(concurrency))
        )
        MODEL_LIMITER.set_rate(_settings.concurrency)
    if max_in_flight is not None:
        _settings.max_in_flight = min(
            MAX_MAX_IN_FLIGHT, max(MIN_MAX_IN_FLIGHT, int(max_in_flight))
        )
        MODEL_LIMITER.set_max_in_flight(_settings.max_in_flight)
        _sync_pipeline_limit()
    if waiting_capacity is not None:
        _settings.waiting_capacity = min(
            MAX_WAITING_CAPACITY, max(MIN_WAITING_CAPACITY, int(waiting_capacity))
        )
        _sync_pipeline_limit()
    if eval_timeout_s is not None:
        _settings.eval_timeout_s = min(
            MAX_EVAL_TIMEOUT_S, max(MIN_EVAL_TIMEOUT_S, float(eval_timeout_s))
        )
    if judges is not None:
        _settings.judges = list(dict.fromkeys(str(name) for name in judges))
    return _settings


def load_persisted_settings(path: Path | None = None) -> RuntimeSettings:
    """启动时加载持久化设置；文件缺失/损坏一律回退默认值，绝不阻断启动。"""
    global _settings
    target = path or SETTINGS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    concurrency = None
    max_in_flight = None
    waiting_capacity = None
    eval_timeout_s = None
    judges = None
    if isinstance(raw, dict):
        if isinstance(raw.get("concurrency"), int):
            concurrency = raw["concurrency"]
        if isinstance(raw.get("max_in_flight"), int):
            max_in_flight = raw["max_in_flight"]
        if isinstance(raw.get("waiting_capacity"), int):
            waiting_capacity = raw["waiting_capacity"]
        if isinstance(raw.get("eval_timeout_s"), (int, float)):
            eval_timeout_s = float(raw["eval_timeout_s"])
        persisted_judges = raw.get("judges")
        if isinstance(persisted_judges, list) and all(
            isinstance(name, str) for name in persisted_judges
        ):
            judges = persisted_judges
    apply_settings(
        concurrency=concurrency,
        max_in_flight=max_in_flight,
        waiting_capacity=waiting_capacity,
        eval_timeout_s=eval_timeout_s,
        judges=judges,
    )
    return _settings


def persist_settings(path: Path | None = None) -> bool:
    """原子写（tmp + os.replace，镜像 history.save_task 的模式）。best-effort。"""
    target = path or SETTINGS_PATH
    payload = {
        "concurrency": _settings.concurrency,
        "max_in_flight": _settings.max_in_flight,
        "waiting_capacity": _settings.waiting_capacity,
        "eval_timeout_s": _settings.eval_timeout_s,
        "judges": _settings.judges,
        "updated_at": time.time(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return True
    except OSError:
        logger.exception("保存系统设置失败: %s", target)
        return False
