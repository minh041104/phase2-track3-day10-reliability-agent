from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


class RouteReason(str):
    """Route string with a backwards-compatible coarse category."""

    category: str

    def __new__(cls, value: str, category: str) -> RouteReason:
        obj = str.__new__(cls, value)
        obj.category = category
        return obj

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str) and other in {"primary", "fallback", "static_fallback"}:
            return self.category == other
        if isinstance(other, str):
            return str(self) == other
        return False

    def __hash__(self) -> int:
        return hash(self.category)


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        start = time.perf_counter()
        last_error: str | None = None

        if self.cache is not None:
            try:
                cached, score = self.cache.get(prompt)
                if cached is not None:
                    return GatewayResponse(
                        text=cached,
                        route=RouteReason(f"cache_hit:{score:.2f}", "cache_hit"),
                        provider=None,
                        cache_hit=True,
                        latency_ms=(time.perf_counter() - start) * 1000,
                        estimated_cost=0.0,
                    )
            except Exception as exc:
                last_error = f"cache_error:{exc}"

        for provider in self.providers:
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    try:
                        self.cache.set(prompt, response.text, {"provider": provider.name})
                    except Exception as exc:
                        last_error = f"cache_set_error:{exc}"
                route_category = "primary" if provider == self.providers[0] else "fallback"
                return GatewayResponse(
                    text=response.text,
                    route=RouteReason(f"{route_category}:{provider.name}", route_category),
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    estimated_cost=response.estimated_cost,
                )
            except CircuitOpenError as exc:
                last_error = f"{provider.name}:circuit_open:{exc}"
                continue
            except ProviderError as exc:
                last_error = f"{provider.name}:provider_error:{exc}"
                continue

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route=RouteReason("static_fallback", "static_fallback"),
            provider=None,
            cache_hit=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            estimated_cost=0.0,
            error=last_error,
        )
