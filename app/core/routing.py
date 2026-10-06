"""Capability-aware endpoint selection and backend health (PRD §15).

Selection order:
    enabled -> protocol match -> modality match -> healthy -> below concurrency
    -> highest priority -> weighted round-robin within that priority tier

Health is tracked with hysteresis (N consecutive failures to open, M consecutive
successes to close) so a single blip does not flap an endpoint out of rotation.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from collections.abc import Collection
from dataclasses import dataclass, field

import httpx

from app.core.capability import endpoint_supports, upstream_model_for
from app.core.errors import ErrorCode, GatewayError, describe
from app.core.inflight import InFlightLimiter, LocalInFlightLimiter
from app.core.multimodal import RequestProfile
from app.registry.schema import Endpoint, ModelDefinition
from app.registry.store import RegistryStore, endpoint_key, slot_key

log = logging.getLogger(__name__)


# Failures where sending the same request to a different machine is a fair bet.
# A 4xx is the backend's verdict on the request itself, and every other backend
# will reach the same one - retrying that is just a slower way to fail twice.
RETRYABLE_ERRORS = frozenset({ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.UPSTREAM_UNAVAILABLE})


# 4xx ที่เป็นคำตัดสินเรื่อง *คำขอ* ไม่ใช่เรื่องสุขภาพของเครื่อง: prompt ยาวเกิน context ·
# body ใหญ่เกิน · พารามิเตอร์ผิด · backend ที่ตอบแบบนี้ยังมีชีวิตและทำงานถูกต้อง
#
# เดิมทุกสถานะ >= 400 ถูกนับเป็นความล้มเหลวของ endpoint — ผู้ใช้คนเดียวที่ส่ง prompt
# ยาวเกินสามครั้งติดกัน (agent ที่ retry เองทำแบบนี้เป็นปกติ) จึงทำให้ backend ที่ดีอยู่
# ถูกตีว่า unhealthy และหลุดจากการจ่ายงานของทุกคน จนกว่ารอบ health probe จะกู้กลับ
#
# 401/403/404 ไม่อยู่ในนี้โดยเจตนา: คีย์ผิดหรือโมเดลไม่ได้โหลดคือ backend ใช้ไม่ได้จริง
REQUEST_FAULT_STATUSES = frozenset({400, 413, 422})


def is_request_fault(status: int) -> bool:
    return status in REQUEST_FAULT_STATUSES


def is_retryable_status(status: int) -> bool:
    """502/503 mean the machine is unwell; 408 and 429 mean it is out of room."""
    return status >= 500 or status in (408, 429)


@dataclass
class EndpointHealth:
    healthy: bool = True
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_error: str = ""
    last_checked_at: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    # สิ่งที่ health path ตอบครั้งล่าสุด ("HTTP 200") — ว่าง = ยังไม่เคยได้คำตอบ
    last_probe: str = ""


@dataclass
class EndpointState:
    """Runtime state keyed by '<alias>:<endpoint name>'."""

    health: dict[str, EndpointHealth] = field(default_factory=dict)

    def get(self, key: str) -> EndpointHealth:
        return self.health.setdefault(key, EndpointHealth())


class Router:
    def __init__(self, registry: RegistryStore) -> None:
        self._probe_client: httpx.AsyncClient | None = None
        # ค่าเริ่มต้นนับในเครื่อง — state.start() สลับเป็นตัวที่แชร์ข้าม worker ถ้ามี Redis
        self._limiter: InFlightLimiter = LocalInFlightLimiter()
        self._registry = registry
        self._state = EndpointState()
        # คำขอที่กำลังวิ่งต่อ *backend* (คีย์ = slot_key) — สุขภาพเป็นเรื่องของ alias:endpoint
        # แต่ช่องเป็นของ process ที่เสิร์ฟ สอง alias บนเครื่องเดียวกันต้องเห็นตัวเลขเดียวกัน
        self._in_flight: dict[str, int] = {}
        # ใบจองที่ถืออยู่ → คีย์ที่ใช้ตอนจอง · release ต้องคืนที่เดิมเสมอ แม้ทะเบียนจะถูก
        # แก้ระหว่างที่คำขอยังวิ่ง (เปลี่ยน base_url/upstream_model แล้วคีย์ที่คำนวณใหม่
        # ไม่ตรงกับตอนจอง = ตัวนับค้างถาวร และ endpoint นั้น "เต็ม" ไปจนกว่าจะ restart)
        self._held: dict[tuple[str, str], str] = {}
        # ใบจองที่กำลังคืนอยู่เบื้องหลัง (ดู release)
        self._releases: set[asyncio.Task] = set()
        self._rr: dict[str, itertools.count] = {}
        self._lock = asyncio.Lock()
        self._health_task: asyncio.Task | None = None

    # -- selection ---------------------------------------------------------
    def select(
        self,
        model: ModelDefinition,
        profile: RequestProfile,
        protocol: str,
        exclude: Collection[str] = (),
    ) -> Endpoint:
        """Pick a backend, optionally skipping ones already tried and failed.

        `exclude` is how failover asks for "another one": a backend that just
        refused the connection is still marked healthy for two more strikes, so
        without it the retry would land on the same dead machine.
        """
        compatible = [
            e
            for e in model.spec.endpoints
            if e.name not in exclude and endpoint_supports(e, profile, protocol)
        ]
        if not compatible:
            raise GatewayError(
                ErrorCode.NO_HEALTHY_ENDPOINT,
                f"No backend for '{model.alias}' can serve this request "
                f"({profile.request_modality} over {protocol}).",
                details={"model": model.alias, "modality": profile.request_modality},
            )

        healthy = [
            e for e in compatible if self._state.get(endpoint_key(model.alias, e)).healthy
        ]
        # Degraded mode: if health checks marked everything down we still try,
        # because a stale probe must not take the whole model offline.
        candidates = healthy or compatible
        if not healthy:
            log.warning(
                "all endpoints for %s marked unhealthy; attempting anyway", model.alias
            )

        with_capacity = [
            e for e in candidates if self.in_flight(model.alias, e) < e.max_concurrency
        ]
        if not with_capacity:
            raise GatewayError(
                ErrorCode.CONCURRENCY_LIMIT_EXCEEDED,
                f"All backends for '{model.alias}' are at capacity. Please retry shortly.",
                retry_after=5,
                details={"model": model.alias},
            )

        top_priority = max(e.priority for e in with_capacity)
        tier = [e for e in with_capacity if e.priority == top_priority]
        return self._weighted_pick(model.alias, tier)

    def _weighted_pick(self, alias: str, tier: list[Endpoint]) -> Endpoint:
        if len(tier) == 1:
            return tier[0]
        # Prefer the least-loaded endpoint; break ties by weight.
        least = min(self.in_flight(alias, e) for e in tier)
        least_loaded = [e for e in tier if self.in_flight(alias, e) == least]
        if len(least_loaded) == 1:
            return least_loaded[0]
        population = [e for e in least_loaded for _ in range(e.weight)]
        return random.choice(population)

    # -- outcome reporting -------------------------------------------------
    def set_limiter(self, limiter: InFlightLimiter) -> None:
        """สลับไปใช้ตัวนับที่แชร์ข้าม worker — เรียกตอน start ถ้าตั้ง Redis ไว้"""
        self._limiter = limiter

    def _slot(self, alias: str, endpoint: Endpoint) -> str:
        """คีย์ของช่องบน backend ที่ `alias` ใช้ผ่าน endpoint นี้ (ดู store.slot_key)

        `alias` ต้องเป็นตัวที่ *เสิร์ฟจริง* ไม่ใช่ตัวที่สมาชิกขอ — endpoint เป็นของโมเดลนั้น
        alias ที่ทะเบียนไม่รู้จักนับแยกตามชื่อเหมือนเดิม ดีกว่าเดาว่ามันแชร์กับใคร
        """
        model = self._registry.snapshot.models.get(alias)
        if model is None or not getattr(endpoint, "base_url", ""):
            return endpoint_key(alias, endpoint)
        return slot_key(endpoint, upstream_model_for(model, endpoint))

    def in_flight(self, alias: str, endpoint: Endpoint) -> int:
        """คำขอที่กำลังวิ่งอยู่บน backend ของ endpoint นี้ — นับรวมทุก alias ที่ใช้เครื่องเดียวกัน

        ตัวเลขใน process นี้เท่านั้น: ใช้เป็นคำใบ้ตอนเลือกทางและแสดงผล · ด่านจริงคือ acquire()
        """
        return self._in_flight.get(self._slot(alias, endpoint), 0)

    async def acquire(self, alias: str, endpoint: Endpoint, lease: str) -> None:
        """จองช่องหนึ่งช่องบน endpoint นี้ — โยน CONCURRENCY_LIMIT_EXCEEDED เมื่อเต็ม

        `alias` คือโมเดลที่เสิร์ฟจริง และ `lease` ต้องเป็นค่าที่เกตเวย์สร้างเองต่อคำขอ
        (ดู _RequestContext.lease) ไม่ใช่ `x-request-id` ที่ client เลือกส่งมา

        **นี่คือด่านจริง** ส่วนการกรองใน select() เป็นแค่คำใบ้ตอนเลือกทาง
        เพราะระหว่าง select() กับตรงนี้มี `await` คั่นหลายจุด (อ่าน body · resolve
        โมเดล · สร้าง payload) coroutine หลายตัวจึงผ่านด่านของ select() พร้อมกันได้
        ก่อนที่ใครจะเพิ่มค่าเป็นตัวแรก · เช็คกับจองต้องเป็นก้อนเดียวที่แบ่งไม่ได้
        และก้อนนั้นต้องอยู่ตรงจุดที่กำลังจะยิง upstream จริง ๆ
        """
        slot = self._slot(alias, endpoint)
        try:
            granted = await self._limiter.acquire(slot, endpoint.max_concurrency, lease)
        except BaseException:
            # ถูกยกเลิก (หรือพัง) ระหว่างรอคำตอบจากที่เก็บร่วม — ใบจองอาจถูกเขียนไปแล้วทั้งที่
            # เราไม่มีวันได้รู้ · คืนไว้ก่อนเสมอ: คืนใบที่ไม่มีอยู่ไม่เสียอะไร ไม่คืนใบที่มีอยู่
            # คือช่องหายไป 15 นาทีสำหรับทุก worker
            self._release_lease(slot, lease)
            raise
        if not granted:
            raise GatewayError(
                ErrorCode.CONCURRENCY_LIMIT_EXCEEDED,
                f"All backends for '{alias}' are at capacity. Please retry shortly.",
                retry_after=5,
                details={"model": alias},
            )
        key = endpoint_key(alias, endpoint)
        self._held[(lease, key)] = slot
        self._in_flight[slot] = self._in_flight.get(slot, 0) + 1
        self._state.get(key).total_requests += 1

    async def release(self, alias: str, endpoint: Endpoint, lease: str) -> None:
        """คืนช่อง — เรียกซ้ำได้ และคืนที่คีย์เดิมที่ใช้ตอนจองเสมอ

        **บัญชีในเครื่องต้องเสร็จก่อน `await` ตัวแรก และการคืนใบจองต้องไม่ตายไปกับผู้เรียก**

        เดิมลำดับคือ pop `_held` → `await limiter.release()` → ลด `_in_flight` · จุดเรียกอยู่ใน
        `finally` ของ generator ซึ่งตอนผู้ใช้ตัดสาย (Esc ใน Claude Code) รันใต้ cancel scope ของ
        Starlette: `await` ตัวแรกที่แตะเครือข่ายโยน CancelledError ทันที บรรทัดที่ลด `_in_flight`
        จึงไม่เคยรัน และเพราะ `_held` ถูก pop ไปแล้ว ก็ไม่มีใครคืนซ้ำได้อีก · กับ Redis จริง
        (2026-10-06): hint ในเครื่อง = 1 ทั้งที่ตัวนับร่วม = 0 → โมเดล 1 ช่องตอบ 429
        "at capacity" ทุกคำขอบน worker นั้นจน restart · ถ้า ZREM ยังไม่ทันออกจากเครื่อง ใบจอง
        ก็ค้างใน Redis ครบ 15 นาทีให้ทุก worker เห็น

        ตัวนับในเครื่องไม่มีเหตุผลต้องรอเครือข่าย จึงลดก่อน · ส่วนการคืนใบจองย้ายไปเป็น task
        ของตัวเองที่ shield ไว้ แบบเดียวกับ finalize ของแถว usage (app/api/openai.py)
        """
        slot = self._held.pop((lease, endpoint_key(alias, endpoint)), None)
        if slot is None:
            # ไม่เคยจองสำเร็จ หรือคืนไปแล้ว — ห้ามลดตัวนับ ไม่งั้นเป็นการคืนช่องของคนอื่น
            await self._settle(self._release_lease(self._slot(alias, endpoint), lease))
            return
        remaining = self._in_flight.get(slot, 0) - 1
        if remaining > 0:
            self._in_flight[slot] = remaining
        else:
            self._in_flight.pop(slot, None)
        await self._settle(self._release_lease(slot, lease))

    def _release_lease(self, slot: str, lease: str) -> asyncio.Task:
        """คืนใบจองในที่เก็บ (ร่วม) เป็น task ที่ไม่ขึ้นกับอายุของผู้เรียก"""

        async def run() -> None:
            try:
                await self._limiter.release(slot, lease)
            except Exception as exc:  # noqa: BLE001 - ใบจองหมดอายุเองได้ อย่าให้คำขอพังเพราะคืนไม่ได้
                log.warning("could not return in-flight lease for %s: %s", slot, describe(exc))

        task = asyncio.get_running_loop().create_task(run())
        # asyncio ถือ task ไว้แค่ weak reference — ไม่เก็บเอง GC เก็บทิ้งกลางทางได้
        self._releases.add(task)
        task.add_done_callback(self._releases.discard)
        return task

    @staticmethod
    async def _settle(task: asyncio.Task) -> None:
        """รอ task ถ้ารอได้ · ถ้าผู้เรียกถูกยกเลิก task ยังวิ่งต่อจนจบ"""
        await asyncio.shield(task)

    async def drain_releases(self, timeout: float = 5.0) -> None:
        """รอใบจองที่กำลังคืนให้คืนครบ — เรียกตอนปิดแอป ก่อนปิดการเชื่อมต่อ Redis"""
        pending = set(self._releases)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def report_success(self, alias: str, endpoint: Endpoint) -> None:
        gateway = self._registry.snapshot.gateway
        state = self._state.get(endpoint_key(alias, endpoint))
        state.consecutive_failures = 0
        state.consecutive_successes += 1
        if not state.healthy and state.consecutive_successes >= gateway.healthy_threshold:
            state.healthy = True
            state.last_error = ""
            log.info("endpoint %s:%s recovered", alias, endpoint.name)

    def report_http_error(self, alias: str, endpoint: Endpoint, status: int) -> None:
        """backend ตอบ HTTP >= 400 — นับเป็นความล้มเหลวของเครื่องเฉพาะเมื่อมันเป็นเรื่องของเครื่อง"""
        if is_request_fault(status):
            return
        self.report_failure(alias, endpoint, f"HTTP {status}")

    def report_failure(self, alias: str, endpoint: Endpoint, error: str) -> None:
        gateway = self._registry.snapshot.gateway
        state = self._state.get(endpoint_key(alias, endpoint))
        state.consecutive_successes = 0
        state.consecutive_failures += 1
        state.total_failures += 1
        state.last_error = error[:500]
        if state.healthy and state.consecutive_failures >= gateway.unhealthy_threshold:
            state.healthy = False
            log.error(
                "endpoint %s:%s marked unhealthy after %d failures: %s",
                alias,
                endpoint.name,
                state.consecutive_failures,
                error[:200],
            )

    def health_report(self) -> dict[str, dict]:
        report: dict[str, dict] = {}
        # ใครใช้ช่องชุดเดียวกันบ้าง — `in_flight` เป็นยอดของ backend ทั้งตัว สอง alias ที่ชี้
        # เครื่องเดียวกันจึงขึ้นเลขเดียวกันพร้อมกัน ถ้าไม่บอกว่าแชร์กัน คนอ่านจะนับเป็นสองคำขอ
        sharing: dict[str, list[str]] = {}
        for alias, model in self._registry.snapshot.models.items():
            for endpoint in model.spec.endpoints:
                sharing.setdefault(self._slot(alias, endpoint), []).append(
                    endpoint_key(alias, endpoint)
                )
        for alias, model in self._registry.snapshot.models.items():
            for endpoint in model.spec.endpoints:
                key = endpoint_key(alias, endpoint)
                state = self._state.get(key)
                slot = self._slot(alias, endpoint)
                report[key] = {
                    "model": alias,
                    "endpoint": endpoint.name,
                    "server_type": endpoint.server_type.value,
                    "base_url": endpoint.normalized_base_url,
                    "healthy": state.healthy,
                    "in_flight": self._in_flight.get(slot, 0),
                    "shares_slots_with": sorted(k for k in sharing[slot] if k != key),
                    "max_concurrency": endpoint.max_concurrency,
                    "total_requests": state.total_requests,
                    "total_failures": state.total_failures,
                    "last_error": state.last_error,
                    "last_probe": state.last_probe,
                }
        return report

    # -- active probing ----------------------------------------------------
    async def start_health_checks(self) -> None:
        # client ตัวเดียวใช้ยาวตลอดอายุ router — เดิมสร้าง AsyncClient ใหม่ "ทุก probe
        # ทุก endpoint ทุกรอบ" คือ TCP handshake (+ TLS ถ้า https) ใหม่หมดทุก 15 วินาที
        # ต่อทุกปลายทาง · กับ 4 worker ยิ่งคูณสี่ · ไม่มีอะไรได้ประโยชน์จากการทิ้ง
        # connection ทุกครั้ง เพราะปลายทางชุดเดิมถูกถามซ้ำตลอด
        self._probe_client = httpx.AsyncClient(
            timeout=self._registry.snapshot.gateway.health_check_timeout_seconds,
            limits=httpx.Limits(max_keepalive_connections=32, max_connections=64),
        )
        self._health_task = asyncio.create_task(self._health_loop(), name="health-check")

    async def stop_health_checks(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        if self._probe_client is not None:
            await self._probe_client.aclose()
            self._probe_client = None

    async def _health_loop(self) -> None:
        while True:
            gateway = self._registry.snapshot.gateway
            await asyncio.sleep(gateway.health_check_interval_seconds)
            try:
                await self.probe_all()
            except Exception:
                log.exception("health probe cycle failed")

    async def probe_all(self) -> None:
        snapshot = self._registry.snapshot
        timeout = snapshot.gateway.health_check_timeout_seconds
        # โมเดลที่ถูกปิดไว้ไม่ควรถูกยิงหา · เดิมกรองแค่ระดับ endpoint ทำให้ alias ที่
        # ปิดทั้งตัวยังส่ง health probe ออกไปทุกรอบ แล้วขึ้น ERROR เมื่อปลายทางไม่มีจริง
        #
        # เจอตอนติดตั้งใหม่บนเครื่องสะอาด: ปิดโมเดลตัวอย่างแล้ว log ยังเต็มไปด้วย
        # "Name or service not known" ของเครื่องที่ไม่ใช่ของผู้ติดตั้ง · ปิดที่ระดับ
        # endpoint แทนก็ไม่ได้ เพราะสคีมาบังคับว่าต้องเปิดอย่างน้อยหนึ่งอัน
        tasks = [
            self._probe(alias, endpoint, timeout)
            for alias, model in snapshot.models.items()
            if model.spec.enabled
            for endpoint in model.spec.endpoints
            if endpoint.enabled
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, alias: str, endpoint: Endpoint, timeout: float) -> None:
        url = endpoint.normalized_base_url + endpoint.health_path
        try:
            client = self._probe_client
            if client is None:
                # probe_all() ถูกเรียกตรง ๆ ได้ (เทส · หน้า admin) โดยยังไม่ได้ start
                # ทางนี้จึงต้องยังทำงานได้ แค่ไม่ได้ประโยชน์จาก pool
                async with httpx.AsyncClient(timeout=timeout) as throwaway:
                    response = await throwaway.get(url)
            else:
                response = await client.get(url, timeout=timeout)
            self._note_probe(alias, endpoint, response.status_code)
            if response.status_code < 500:
                self.report_success(alias, endpoint)
            else:
                self.report_failure(alias, endpoint, f"health HTTP {response.status_code}")
        except Exception as exc:
            # ชนิดของ exception เสมอ — ConnectTimeout/ReadTimeout ของ httpx ไม่มีข้อความ และ
            # log เดิมจบที่ "health probe failed:" เฉย ๆ (เครื่องจริง 2026-10-06 18:58)
            self.report_failure(alias, endpoint, f"health probe failed: {describe(exc)}")

    def _note_probe(self, alias: str, endpoint: Endpoint, status: int) -> None:
        """จำว่า health path ตอบอะไร และเตือนเมื่อมันตอบแบบที่ model server ไม่ตอบ

        กติกา "ต่ำกว่า 500 = ถึงแล้ว" ยังอยู่ เพราะผู้ให้บริการออนไลน์พึ่งมัน (GET /models
        ไม่ใส่คีย์ตอบ 401 — ดู core/providers.py) และ vLLM ที่ตั้ง base_url ลงท้าย /v1 ตอบ 404
        ให้ /v1/health ทั้งที่ทำงานปกติ · แต่ 401/404 จาก health path ก็เป็นหน้าตาของ "มีของ
        อย่างอื่นฟังพอร์ตนี้อยู่" ด้วย (ทีมเคยโดน portainer บน :8000) จึงต้องมีที่ให้เห็น
        แทนที่จะเป็นเขียวเงียบ ๆ
        """
        state = self._state.get(endpoint_key(alias, endpoint))
        seen = f"HTTP {status}"
        if seen != state.last_probe and not 200 <= status < 300 and status < 500:
            log.warning(
                "endpoint %s:%s answers its health path %s with %s — reachable, but this is "
                "not what a model server's health route returns; check that the port is the "
                "model server and not something else",
                alias, endpoint.name, endpoint.health_path, seen,
            )
        state.last_probe = seen
