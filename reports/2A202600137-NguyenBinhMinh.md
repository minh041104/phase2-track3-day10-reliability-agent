# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks a shared cache first, then routes through provider-specific circuit breakers. If the primary provider fails or its circuit is open, traffic moves to the backup provider; if every provider is unavailable, the gateway returns a static degraded-service response.

```text
User Request
    |
    v
[Gateway] -> [Redis/Memory Cache] -> cache hit
    | miss
    v
[Circuit Breaker: Primary] -> Provider A
    | open/failure
    v
[Circuit Breaker: Backup] -> Provider B
    | all fail
    v
[Static fallback message]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects repeated provider failure quickly while avoiding a single jitter failure opening the circuit. |
| reset_timeout_seconds | 2 | Short lab-friendly timeout; long enough to prevent retry storms and short enough to show recovery. |
| success_threshold | 1 | One successful half-open probe is enough for this fake provider workload. |
| cache backend | redis | Redis is used for shared cache evidence across gateway instances. |
| cache TTL | 300 | Five minutes keeps FAQ/policy answers fresh while still making repeated queries cheap. |
| similarity_threshold | 0.92 | High threshold plus false-hit guardrails prevents date-sensitive cache mistakes. |
| load_test requests | 100 | Enough repeated traffic to show cache hit rate and circuit transitions quickly. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 100% | yes |
| Latency P95 | < 2500 ms | 305.63 | yes |
| Fallback success rate | >= 95% | 100% | yes |
| Cache hit rate | >= 10% | 77.25% | yes |
| Recovery time | < 5000 ms | 2932.1887 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 1 |
| error_rate | 0 |
| latency_p50_ms | 3.06 |
| latency_p95_ms | 305.63 |
| latency_p99_ms | 524.08 |
| fallback_success_rate | 1 |
| cache_hit_rate | 0.7725 |
| circuit_open_count | 6 |
| recovery_time_ms | 2932.1887 |
| estimated_cost | 0.0426 |
| estimated_cost_saved | 0.309 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 209.4 | 0.5 | -208.9 |
| latency_p95_ms | 236.85 | 229.65 | -7.2 |
| estimated_cost | 0.0577 | 0.0151 | -0.0426 |
| cache_hit_rate | 0 | 0.73 | 0.73 |

## 6. Redis shared cache

In-memory cache is per-process, so horizontally scaled gateways would miss entries warmed by other instances. `SharedRedisCache` stores query/response hashes in Redis with TTL, so two gateway instances can reuse the same safe cached answer.

### Evidence of shared state

```text
two SharedRedisCache instances -> ('shared response', 1.0)
```

### Redis CLI output

```bash
docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:095946136fea
rl:cache:8baa2cfa11fa
rl:cache:9e413fd814eb
rl:cache:b2a52f7dc795
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | primary circuit opens and backup serves all non-cache traffic | availability=1, cache_hit_rate=0.73, circuit_open_count=4 | pass |
| primary_flaky_50 | traffic remains mostly available while primary is flaky | availability=1, cache_hit_rate=0.8, circuit_open_count=2 | pass |
| all_healthy | healthy providers should keep availability near 100% | availability=1, cache_hit_rate=0.82, circuit_open_count=0 | pass |
| cache_stale_candidate | similar but date-conflicting queries must not cache-hit | availability=1, cache_hit_rate=0.74, circuit_open_count=0, false_hit_score=0.7571, logged=True | pass |
| cache_comparison | Cache should reduce cost and latency on repeated queries | See cache comparison table | pass |

## 8. Failure analysis

Remaining weakness: circuit breaker state is still process-local. In a real multi-instance deployment, one gateway could learn that a provider is unhealthy while another keeps sending traffic until it independently trips its own breaker. I would move breaker counters and state transitions into Redis or another strongly consistent shared store, then add jittered half-open probes to avoid many instances probing at the same time.

## 9. Next steps

1. Store circuit breaker state in Redis with atomic `INCR`/TTL operations.
2. Export Prometheus counters for request totals, latency buckets, cache hits, and circuit state.
3. Add per-user rate limiting and cache-key namespacing for stronger production privacy controls.
