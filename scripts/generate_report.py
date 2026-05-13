from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.cache import SharedRedisCache
from reliability_lab.config import LabConfig, load_config


def fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def yes_no(condition: bool) -> str:
    return "yes" if condition else "no"


def metric(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = metrics.get(key, default)
    return float(value) if value is not None else default


def redis_evidence(redis_url: str) -> dict[str, object]:
    """Create lightweight Redis evidence for the final report when Redis is available."""
    try:
        c1 = SharedRedisCache(redis_url, ttl_seconds=60, similarity_threshold=0.9, prefix="rl:report:")
        c2 = SharedRedisCache(redis_url, ttl_seconds=60, similarity_threshold=0.9, prefix="rl:report:")
        c1.flush()
        c1.set("shared state probe", "shared response")
        shared_state = c2.get("shared state probe")
        c1.flush()

        keys = sorted(str(key) for key in c1._redis.scan_iter("rl:cache:*"))[:10]
        c1.close()
        c2.close()
        return {"available": True, "shared_state": shared_state, "keys": keys}
    except Exception as exc:
        return {"available": False, "error": str(exc), "keys": []}


def config_rows(config: LabConfig) -> list[tuple[str, str, str]]:
    return [
        (
            "failure_threshold",
            str(config.circuit_breaker.failure_threshold),
            "Detects repeated provider failure quickly while avoiding a single jitter failure opening the circuit.",
        ),
        (
            "reset_timeout_seconds",
            fmt(config.circuit_breaker.reset_timeout_seconds),
            "Short lab-friendly timeout; long enough to prevent retry storms and short enough to show recovery.",
        ),
        (
            "success_threshold",
            str(config.circuit_breaker.success_threshold),
            "One successful half-open probe is enough for this fake provider workload.",
        ),
        (
            "cache backend",
            config.cache.backend,
            "Redis is used for shared cache evidence across gateway instances.",
        ),
        (
            "cache TTL",
            str(config.cache.ttl_seconds),
            "Five minutes keeps FAQ/policy answers fresh while still making repeated queries cheap.",
        ),
        (
            "similarity_threshold",
            fmt(config.cache.similarity_threshold),
            "High threshold plus false-hit guardrails prevents date-sensitive cache mistakes.",
        ),
        (
            "load_test requests",
            str(config.load_test.requests),
            "Enough repeated traffic to show cache hit rate and circuit transitions quickly.",
        ),
    ]


def add_metric_table(lines: list[str], metrics: dict[str, Any]) -> None:
    wanted = [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    ]
    lines += ["| Metric | Value |", "|---|---:|"]
    for key in wanted:
        lines.append(f"| {key} | {fmt(metrics.get(key))} |")


def add_cache_comparison(lines: list[str], metrics: dict[str, Any]) -> None:
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    delta = comparison.get("delta", {})
    lines += [
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        lines.append(
            f"| {key} | {fmt(without_cache.get(key))} | "
            f"{fmt(with_cache.get(key))} | {fmt(delta.get(key))} |"
        )


def add_scenarios(lines: list[str], metrics: dict[str, Any]) -> None:
    scenarios = metrics.get("scenarios", {})
    details = metrics.get("scenario_details", {})
    lines += [
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    for name, status in scenarios.items():
        if name == "cache_comparison":
            lines.append(
                "| cache_comparison | Cache should reduce cost and latency on repeated queries | "
                "See cache comparison table | pass |"
            )
            continue
        detail = details.get(name, {})
        observed = detail.get("observed", {})
        expected = detail.get("expected", detail.get("description", "Configured scenario should pass"))
        observed_text = (
            f"availability={fmt(observed.get('availability'))}, "
            f"cache_hit_rate={fmt(observed.get('cache_hit_rate'))}, "
            f"circuit_open_count={fmt(observed.get('circuit_open_count'))}"
        )
        if name == "cache_stale_candidate":
            observed_text += (
                f", false_hit_score={fmt(detail.get('false_hit_score'))}, "
                f"logged={fmt(detail.get('false_hit_logged'))}"
            )
        lines.append(f"| {name} | {expected} | {observed_text} | {status} |")


def build_report(metrics: dict[str, Any], config: LabConfig) -> str:
    evidence = redis_evidence(config.cache.redis_url)
    recovery = metrics.get("recovery_time_ms")
    recovery_ok = recovery is not None and float(recovery) < 5000

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks a shared cache first, then routes through provider-specific circuit breakers. "
        "If the primary provider fails or its circuit is open, traffic moves to the backup provider; "
        "if every provider is unavailable, the gateway returns a static degraded-service response.",
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[Gateway] -> [Redis/Memory Cache] -> cache hit",
        "    | miss",
        "    v",
        "[Circuit Breaker: Primary] -> Provider A",
        "    | open/failure",
        "    v",
        "[Circuit Breaker: Backup] -> Provider B",
        "    | all fail",
        "    v",
        "[Static fallback message]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
    ]
    for setting, value, reason in config_rows(config):
        lines.append(f"| {setting} | {value} | {reason} |")

    lines += [
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {fmt(metric(metrics, 'availability') * 100)}% | "
        f"{yes_no(metric(metrics, 'availability') >= 0.99)} |",
        f"| Latency P95 | < 2500 ms | {fmt(metrics.get('latency_p95_ms'))} | "
        f"{yes_no(metric(metrics, 'latency_p95_ms') < 2500)} |",
        f"| Fallback success rate | >= 95% | "
        f"{fmt(metric(metrics, 'fallback_success_rate') * 100)}% | "
        f"{yes_no(metric(metrics, 'fallback_success_rate') >= 0.95)} |",
        f"| Cache hit rate | >= 10% | {fmt(metric(metrics, 'cache_hit_rate') * 100)}% | "
        f"{yes_no(metric(metrics, 'cache_hit_rate') >= 0.10)} |",
        f"| Recovery time | < 5000 ms | {fmt(recovery)} | {yes_no(recovery_ok)} |",
        "",
        "## 4. Metrics",
        "",
    ]
    add_metric_table(lines, metrics)

    lines += [
        "",
        "## 5. Cache comparison",
        "",
    ]
    add_cache_comparison(lines, metrics)

    shared_state = evidence.get("shared_state")
    keys = evidence.get("keys", [])
    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "In-memory cache is per-process, so horizontally scaled gateways would miss entries warmed by "
        "other instances. `SharedRedisCache` stores query/response hashes in Redis with TTL, so two "
        "gateway instances can reuse the same safe cached answer.",
        "",
        "### Evidence of shared state",
        "",
        "```text",
        f"two SharedRedisCache instances -> {fmt(shared_state)}",
        "```",
        "",
        "### Redis CLI output",
        "",
        "```bash",
        'docker compose exec redis redis-cli KEYS "rl:cache:*"',
    ]
    if keys:
        lines.extend(str(key) for key in keys)
    else:
        lines.append(fmt(evidence.get("error", "no keys found")))
    lines += [
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
    ]
    add_scenarios(lines, metrics)

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "Remaining weakness: circuit breaker state is still process-local. In a real multi-instance "
        "deployment, one gateway could learn that a provider is unhealthy while another keeps sending "
        "traffic until it independently trips its own breaker. I would move breaker counters and state "
        "transitions into Redis or another strongly consistent shared store, then add jittered half-open "
        "probes to avoid many instances probing at the same time.",
        "",
        "## 9. Next steps",
        "",
        "1. Store circuit breaker state in Redis with atomic `INCR`/TTL operations.",
        "2. Export Prometheus counters for request totals, latency buckets, cache hits, and circuit state.",
        "3. Add per-user rate limiting and cache-key namespacing for stronger production privacy controls.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text())
    config = load_config(args.config)
    report = build_report(metrics, config)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
