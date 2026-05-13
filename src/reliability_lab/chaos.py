from __future__ import annotations

import json
import random
import time
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open":
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def summarize_metrics(metrics: RunMetrics) -> dict[str, float]:
    """Small numeric summary used for scenario details and cache comparison."""
    return {
        "availability": round(metrics.availability, 4),
        "error_rate": round(metrics.error_rate, 4),
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "fallback_success_rate": round(metrics.fallback_success_rate, 4),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "estimated_cost_saved": round(metrics.estimated_cost_saved, 6),
        "circuit_open_count": float(metrics.circuit_open_count),
    }


def evaluate_scenario(
    scenario: ScenarioConfig, metrics: RunMetrics, config: LabConfig
) -> tuple[bool, dict[str, object]]:
    """Return pass/fail plus observed evidence for a named scenario."""
    detail: dict[str, object] = {
        "description": scenario.description,
        "observed": summarize_metrics(metrics),
        "fallback_successes": metrics.fallback_successes,
        "static_fallbacks": metrics.static_fallbacks,
        "cache_hits": metrics.cache_hits,
    }

    if scenario.name == "primary_timeout_100":
        passed = (
            metrics.circuit_open_count > 0
            and metrics.fallback_successes > 0
            and metrics.static_fallbacks == 0
        )
        detail["expected"] = "primary circuit opens and backup serves all non-cache traffic"
        return passed, detail

    if scenario.name == "primary_flaky_50":
        passed = metrics.availability >= 0.8 and (
            metrics.fallback_successes > 0 or metrics.circuit_open_count > 0 or metrics.cache_hits > 0
        )
        detail["expected"] = "traffic remains mostly available while primary is flaky"
        return passed, detail

    if scenario.name == "cache_stale_candidate":
        guardrail_cache = ResponseCache(
            ttl_seconds=config.cache.ttl_seconds,
            similarity_threshold=min(config.cache.similarity_threshold, 0.3),
        )
        guardrail_cache.set("Summarize refund policy for 2024 deadline", "old policy")
        cached, score = guardrail_cache.get("Summarize refund policy for 2026 deadline")
        passed = cached is None and bool(guardrail_cache.false_hit_log)
        detail["expected"] = "similar but date-conflicting queries must not cache-hit"
        detail["false_hit_score"] = round(score, 4)
        detail["false_hit_logged"] = bool(guardrail_cache.false_hit_log)
        return passed, detail

    if scenario.name == "all_healthy":
        passed = metrics.availability >= 0.99 and metrics.static_fallbacks == 0
        detail["expected"] = "healthy providers should keep availability near 100%"
        return passed, detail

    passed = metrics.successful_requests > 0
    detail["expected"] = "scenario should produce at least one successful response"
    return passed, detail


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.flush()
    metrics = RunMetrics()
    request_count = config.load_test.requests
    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        route = str(result.route)
        if route.startswith("fallback"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    open_breakers = [
        breaker for breaker in gateway.breakers.values() if breaker.state == CircuitState.OPEN
    ]
    if open_breakers:
        for provider in gateway.providers:
            provider.fail_rate = 0.0
        time.sleep(config.circuit_breaker.reset_timeout_seconds)
        gateway.complete("recovery probe for account balance of user 1000")

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, float]]:
    """Run the same healthy workload with cache disabled and enabled."""
    healthy_overrides = {provider.name: 0.0 for provider in config.providers}
    comparison_scenario = ScenarioConfig(
        name="cache_comparison",
        description="Healthy providers; compare repeated-query workload with cache on/off",
        provider_overrides=healthy_overrides,
    )

    without_cache_config = config.model_copy(deep=True)
    without_cache_config.cache.enabled = False
    without_cache = run_scenario(without_cache_config, queries, comparison_scenario)

    with_cache_config = config.model_copy(deep=True)
    with_cache_config.cache.enabled = True
    with_cache_config.cache.backend = "memory"
    with_cache = run_scenario(with_cache_config, queries, comparison_scenario)

    without_summary = summarize_metrics(without_cache)
    with_summary = summarize_metrics(with_cache)
    delta = {
        key: round(with_summary[key] - without_summary[key], 6)
        for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]
    }
    return {
        "without_cache": without_summary,
        "with_cache": with_summary,
        "delta": delta,
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    TODO(student): Add a cache vs no-cache comparison scenario.
    Extend with your own custom scenarios (e.g., cost cap near limit).
    """
    random.seed(2026)
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed, detail = evaluate_scenario(scenario, result, config)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details[scenario.name] = detail

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.cache_comparison = run_cache_comparison(config, queries)
    cache_comparison = combined.cache_comparison
    combined.scenarios["cache_comparison"] = (
        "pass"
        if cache_comparison["with_cache"]["cache_hit_rate"] > 0
        and cache_comparison["with_cache"]["estimated_cost"]
        <= cache_comparison["without_cache"]["estimated_cost"]
        else "fail"
    )

    return combined
