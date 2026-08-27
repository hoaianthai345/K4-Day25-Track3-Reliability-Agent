from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


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
        cost_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost = 0.0
        self._cost_lock = Lock()

    def _allowed_providers(self) -> list[tuple[int, FakeLLMProvider]]:
        """Return providers allowed by the optional cumulative cost budget."""
        indexed = list(enumerate(self.providers))
        with self._cost_lock:
            cumulative_cost = self.cumulative_cost
        if self.cost_budget is None:
            return indexed
        if cumulative_cost >= self.cost_budget:
            return []
        if cumulative_cost < self.cost_budget * 0.8:
            return indexed
        cheapest_cost = min((provider.cost_per_1k_tokens for _, provider in indexed), default=0.0)
        return [
            (index, provider)
            for index, provider in indexed
            if provider.cost_per_1k_tokens <= cheapest_cost
        ]

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        TODO(student): Implement the full request routing pipeline:

        1. CACHE CHECK — if self.cache is not None:
           - Call self.cache.get(prompt) → (cached_text, score)
           - If cached_text is not None, return GatewayResponse with:
             route=f"cache_hit:{score:.2f}", cache_hit=True, latency=0, cost=0

        2. PROVIDER FALLBACK CHAIN — iterate self.providers in order:
           - Get the circuit breaker: self.breakers[provider.name]
           - Try breaker.call(provider.complete, prompt)
           - On success:
             a. Store in cache: self.cache.set(prompt, response.text, {"provider": provider.name})
             b. Determine route: "primary" if first provider, else "fallback"
             c. Return GatewayResponse with provider info, latency, cost
           - On ProviderError or CircuitOpenError: save error, continue to next provider

        3. STATIC FALLBACK — if all providers fail:
           - Return GatewayResponse with:
             text="The service is temporarily degraded. Please try again soon."
             route="static_fallback", error=last_error

        BONUS TODO: Add cost budget tracking — if cumulative cost exceeds a threshold,
        skip expensive providers and route to cache or cheaper fallback.
        """
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(cached_text, f"cache_hit:{score:.2f}", None, True, 0.0, 0.0)

        last_error: str | None = None
        with self._cost_lock:
            budget_exhausted = self.cost_budget is not None and self.cumulative_cost >= self.cost_budget
        if budget_exhausted:
            return GatewayResponse(
                "The service is temporarily degraded. Please try again soon.",
                "static_fallback",
                None,
                False,
                0.0,
                0.0,
                "cost budget exhausted",
            )

        for index, provider in self._allowed_providers():
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue
            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})
            with self._cost_lock:
                self.cumulative_cost += response.estimated_cost
            route = "primary" if index == 0 else "fallback"
            return GatewayResponse(
                response.text,
                route,
                response.provider,
                False,
                response.latency_ms,
                response.estimated_cost,
            )

        return GatewayResponse(
            "The service is temporarily degraded. Please try again soon.",
            "static_fallback",
            None,
            False,
            0.0,
            0.0,
            last_error,
        )
