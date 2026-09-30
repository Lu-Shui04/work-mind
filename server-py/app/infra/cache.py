# server-py/app/infra/cache.py
"""两级答案缓存：L1 进程内 + L2 Redis。

## 为什么从"一个 dict"换成两级
原来就是个进程内 dict，三个问题：
1. **多实例不共享**：起两个 worker（或滚动发布时新旧实例并存），命中率直接掉一半，
   用户会感觉"同一个问题有时秒回、有时又等 3 秒"。
2. **重启即失**：改一行代码重建容器缓存全空，冷启动那几分钟所有请求都打真模型。
3. **容量不可控**：只有"超过 500 条删最老 50 条"这种粗糙 LRU，长文本堆积时内存没有上限。

现在：
- **L1（进程内）**：最热的问答走这里，不跨进程、不序列化；
- **L2（Redis）**：跨实例共享 + 重启不丢，TTL 交给 Redis 自己过期（不用扫表）；
- **Redis 不可用时自动降级为纯 L1**：缓存是加速手段，绝不能因为它挂了让业务不可用；
  降级事实体现在 get_stats()["backend"] 上，看板直接能看到。

## 键必须带"权限隔离域"
同一句"年假几天"在不同身份下答案不同（可见文档不同）。scope 由调用方给出
（租户 + 部门 + 密级 + 命中的切片集合）并拼进 key —— 否则会把 A 权限的答案发给 B。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from app.core.config import config
from app.core.logger import logger

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# L1 容量上限；L2 的容量由 Redis 的 maxmemory 策略管
L1_MAX_ENTRIES = int(os.getenv("CACHE_L1_MAX", "500"))
# 单条答案的字符上限：太长就不缓存（别把整篇长文塞进内存和 Redis）
L2_MAX_CHARS = int(os.getenv("CACHE_MAX_CHARS", "20000"))
KEY_PREFIX = os.getenv("CACHE_KEY_PREFIX", "workmind:answer:")


class ExactCache:
    """精确匹配缓存：system prompt + message + scope 完全一致才命中。

    语义与原来一致（只做"完全相同的问题复用答案"），换的是存储与容量控制；
    对外方法是 **async**（Redis 必须异步），调用方记得 await。
    """

    def __init__(self) -> None:
        self._l1: dict[str, dict] = {}
        self._redis = None
        self._redis_ready = False
        self._redis_failed_at = 0.0
        self.stats = {"hits": 0, "misses": 0, "savedTokens": 0}
        self.l2_hits = 0
        self.l2_writes = 0
        self.degraded = False

    # ── key ───────────────────────────────────────────────────────
    def _key(self, system_prompt: str | None, message: str, scope: str = "") -> str:
        raw = f"{scope}||{system_prompt or ''}||{message}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def ttl_seconds(self) -> int:
        """config 里的 TTL 是毫秒（沿用前端老实现），Redis 用秒。"""
        return max(1, int(config.cache.ttl / 1000))

    # ── L2：Redis 连接（惰性 + 失败退避）──────────────────────────
    async def _get_redis(self):
        if self._redis_ready:
            return self._redis
        # 失败后 30 秒内不再重试：Redis 挂了不能让每个请求都卡一次连接超时
        if self._redis_failed_at and (time.time() - self._redis_failed_at) < 30:
            return None
        try:
            import redis.asyncio as aioredis  # 延迟导入：没装也能纯 L1 运行
            client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True,
                                       socket_connect_timeout=1.5, socket_timeout=1.5)
            await client.ping()
            self._redis = client
            self._redis_ready = True
            self.degraded = False
            logger.info("cache: Redis 已连接（L1 进程内 + L2 共享）", {"url": _safe(REDIS_URL)})
            return client
        except Exception as err:  # noqa: BLE001 - 缓存连不上不是致命错误
            self._redis_failed_at = time.time()
            self._redis = None
            self._redis_ready = False
            self.degraded = True
            logger.warn("cache: Redis 不可用，降级为纯进程内缓存", {"error": str(err)[:160]})
            return None

    # ── 读 ────────────────────────────────────────────────────────
    async def get(self, system_prompt: str | None, message: str, scope: str = "") -> dict | None:
        k = self._key(system_prompt, message, scope)

        entry = self._l1.get(k)
        if entry is not None:
            if (time.time() * 1000) - entry["ts"] > config.cache.ttl:
                self._l1.pop(k, None)
            else:
                self.stats["hits"] += 1
                self.stats["savedTokens"] += int(entry.get("tokens") or 0)
                return entry

        client = await self._get_redis()
        if client is not None:
            try:
                raw = await client.get(KEY_PREFIX + k)
                if raw:
                    data = json.loads(raw)
                    data["ts"] = time.time() * 1000
                    self._l1[k] = data            # 回填 L1：热数据下次不必再走网络
                    self._trim_l1()
                    self.stats["hits"] += 1
                    self.stats["savedTokens"] += int(data.get("tokens") or 0)
                    self.l2_hits += 1
                    return data
            except Exception as err:  # noqa: BLE001
                self._redis_ready = False
                self.degraded = True
                logger.warn("cache: Redis 读取失败，本次降级 L1", {"error": str(err)[:160]})

        self.stats["misses"] += 1
        return None

    # ── 写 ────────────────────────────────────────────────────────
    async def set(self, system_prompt: str | None, message: str, content: str,
                  tokens: int = 0, scope: str = "") -> None:
        if not content or len(content) > L2_MAX_CHARS:
            return
        k = self._key(system_prompt, message, scope)
        entry = {"content": content, "tokens": int(tokens or 0), "ts": time.time() * 1000}
        self._l1[k] = entry
        self._trim_l1()

        client = await self._get_redis()
        if client is not None:
            try:
                await client.set(KEY_PREFIX + k, json.dumps(entry, ensure_ascii=False),
                                 ex=self.ttl_seconds)
                self.l2_writes += 1
            except Exception as err:  # noqa: BLE001
                self._redis_ready = False
                self.degraded = True
                logger.warn("cache: Redis 写入失败，本次降级 L1", {"error": str(err)[:160]})

    def _trim_l1(self) -> None:
        """L1 超限时按写入时间丢最老的一批（LRU 的近似，够用且没有锁开销）。"""
        if len(self._l1) <= L1_MAX_ENTRIES:
            return
        oldest = sorted(self._l1.items(), key=lambda kv: kv[1].get("ts", 0))
        for key, _ in oldest[: max(1, L1_MAX_ENTRIES // 10)]:
            self._l1.pop(key, None)

    # ── 维护 ──────────────────────────────────────────────────────
    @property
    def hit_rate(self) -> str:
        total = self.stats["hits"] + self.stats["misses"]
        return "0%" if total == 0 else f"{self.stats['hits'] / total * 100:.1f}%"

    async def clear(self) -> dict:
        """清空缓存内容与统计（测试 / 一键重置用）。"""
        size = len(self._l1)
        self._l1.clear()
        client = await self._get_redis()
        removed = 0
        if client is not None:
            try:
                # 按前缀 SCAN 删：不用 KEYS（线上 KEYS 会阻塞整个 Redis）
                async for key in client.scan_iter(match=KEY_PREFIX + "*", count=500):
                    await client.delete(key)
                    removed += 1
            except Exception as err:  # noqa: BLE001
                logger.warn("cache: Redis 清空失败", {"error": str(err)[:160]})
        self.reset_stats()
        return {"cleared": size + removed}

    def reset_stats(self) -> None:
        self.stats = {"hits": 0, "misses": 0, "savedTokens": 0}
        self.l2_hits = 0
        self.l2_writes = 0

    def get_stats(self) -> dict:
        return {
            "size": len(self._l1),
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "hitRate": self.hit_rate,
            "savedTokens": self.stats["savedTokens"],
            # 后端与降级状态必须显式给出：出问题时第一眼要看"缓存还活着吗"
            "backend": "redis+l1" if self._redis_ready else "memory-only",
            "degraded": self.degraded,
            "l2Hits": self.l2_hits,
            "l2Writes": self.l2_writes,
            "ttlSeconds": self.ttl_seconds,
        }

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._redis = None
        self._redis_ready = False


def _safe(url: str) -> str:
    """日志里不打 Redis 密码。"""
    if "@" in url:
        head, tail = url.rsplit("@", 1)
        return f"{head.split('://', 1)[0]}://***@{tail}"
    return url


# 全局单例
cache = ExactCache()
