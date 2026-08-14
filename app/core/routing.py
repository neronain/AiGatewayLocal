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

from app.core.capability import endpoint_supports
from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import RequestProfile
from app.registry.schema import Endpoint, ModelDefinition
from app.registry.store import RegistryStore, endpoint_key

log = logging.getLogger(__name__)


# Failures where sending the same request to a different machine is a fair bet.
# A 4xx is the backend's verdict on the request itself, and every other backend
# will reach the same one - retrying that is just a slower way to fail twice.
RETRYABLE_ERRORS = frozenset({ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.UPSTREAM_UNAVAILABLE})


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
    in_flight: int = 0
    total_requests: int = 0
    total_failures: int = 0


@dataclass
class EndpointState:
    """Runtime state keyed by '<alias>:<endpoint name>'."""

    health: dict[str, EndpointHealth] = field(default_factory=dict)

    def get(self, key: str) -> EndpointHealth:
        return self.health.setdefault(key, EndpointHealth())


class Router:
    def __init__(self, registry: RegistryStore) -> None:
        self._registry = registry
        self._state = EndpointState()
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
            e
            for e in candidates
            if self._state.get(endpoint_key(model.alias, e)).in_flight < e.max_concurrency
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
        least = min(
            self._state.get(endpoint_key(alias, e)).in_flight for e in tier
        )
        least_loaded = [
            e for e in tier if self._state.get(endpoint_key(alias, e)).in_flight == least
        ]
        if len(least_loaded) == 1:
            return least_loaded[0]
        population = [e for e in least_loaded for _ in range(e.weight)]
        return random.choice(population)

    # -- outcome reporting -------------------------------------------------
    def acquire(self, alias: str, endpoint: Endpoint) -> None:
        state = self._state.get(endpoint_key(alias, endpoint))
        state.in_flight += 1
        state.total_requests += 1

    def release(self, alias: str, endpoint: Endpoint) -> None:
        state = self._state.get(endpoint_key(alias, endpoint))
        state.in_flight = max(state.in_flight - 1, 0)

    def report_success(self, alias: str, endpoint: Endpoint) -> None:
        gateway = self._registry.snapshot.gateway
        state = self._state.get(endpoint_key(alias, endpoint))
        state.consecutive_failures = 0
        state.consecutive_successes += 1
        if not state.healthy and state.consecutive_successes >= gateway.healthy_threshold:
            state.healthy = True
            state.last_error = ""
            log.info("endpoint %s:%s recovered", alias, endpoint.name)

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
        for alias, model in self._registry.snapshot.models.items():
            for endpoint in model.spec.endpoints:
                key = endpoint_key(alias, endpoint)
                state = self._state.get(key)
                report[key] = {
                    "model": alias,
                    "endpoint": endpoint.name,
                    "server_type": endpoint.server_type.value,
                    "base_url": endpoint.normalized_base_url,
                    "healthy": state.healthy,
                    "in_flight": state.in_flight,
                    "max_concurrency": endpoint.max_concurrency,
                    "total_requests": state.total_requests,
                    "total_failures": state.total_failures,
                    "last_error": state.last_error,
                }
        return report

    # -- active probing ----------------------------------------------------
    async def start_health_checks(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop(), name="health-check")

    async def stop_health_checks(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

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
        tasks = [
            self._probe(alias, endpoint, timeout)
            for alias, model in snapshot.models.items()
            for endpoint in model.spec.endpoints
            if endpoint.enabled
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self, alias: str, endpoint: Endpoint, timeout: float) -> None:
        url = endpoint.normalized_base_url + endpoint.health_path
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
            if response.status_code < 500:
                self.report_success(alias, endpoint)
            else:
                self.report_failure(alias, endpoint, f"health HTTP {response.status_code}")
        except Exception as exc:
            self.report_failure(alias, endpoint, f"health probe failed: {exc}")
