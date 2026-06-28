"""Autograder for the M11 Drill — Toy FastAPI Service Instrumentation.

Maps to the test plan rows documented in build-packet Section C:
  - test_metrics_endpoint_returns_200
  - test_three_metric_families_declared
  - test_request_counter_increments
  - test_latency_histogram_present
  - test_inflight_gauge_present
  - test_request_id_header_set
  - test_structured_log_emitted
  - test_learner_self_test_exists_and_passes  (AST + invocation check)

Imports are deferred inside test functions so that an unmodified starter
(missing prometheus imports, missing middlewares) still yields collectable
tests that FAIL — not a hard collection error.
"""
import ast
import io
import json
import logging
import os
import re
import sys

import pytest


LEARNER_TEST_FILE = os.path.join(os.path.dirname(__file__), "test_drill.py")
MIN_LEARNER_TESTS = 4


def _get_client():
    """Import the learner's app and wrap it in a TestClient.

    Raises pytest.fail with a clear message if the import surfaces the kind of
    error an unmodified starter produces.
    """
    from fastapi.testclient import TestClient

    # Clear the prometheus_client default REGISTRY before re-importing app.
    # The learner declares Counter/Histogram/Gauge at module scope (correct
    # per the reading); re-importing app.py without unregistering them first
    # raises ValueError: Duplicated timeseries in CollectorRegistry on every
    # call past the first. Tests in this file each call _get_client(), so
    # without this clear the second test onward fails on import — not on
    # the learner's instrumentation.
    try:
        from prometheus_client import REGISTRY

        for collector in list(REGISTRY._collector_to_names.keys()):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    except ImportError:
        pass

    try:
        if "app" in sys.modules:
            del sys.modules["app"]
        from app import app as fastapi_app
    except Exception as exc:
        pytest.fail(
            f"Could not import `app` from app.py: {exc.__class__.__name__}: {exc}. "
            "Did you leave any TODO imports unresolved?"
        )
    return TestClient(fastapi_app)


def _scrape_metrics(client):
    return client.get("/metrics").text


def _counter_sample_for(body: str, path: str) -> float:
    """Return requests_total sample value for the given path label, or 0.0.

    Requires BOTH `path=` and `status=` labels to be present -- this
    matches the contract the guide specifies (Counter labels=["path",
    "status"]). A learner who declares the Counter without the `status`
    label will not match this pattern, and the test will fail with a
    helpful message rather than passing on the under-labeled metric.
    """
    pattern = re.compile(
        r'^requests_total\{(?=[^}]*path="' + re.escape(path) + r'")'
        r'(?=[^}]*status=)[^}]*\}\s+([0-9.eE+-]+)',
        re.MULTILINE,
    )
    m = pattern.search(body)
    return float(m.group(1)) if m else 0.0


def _drive_traffic(client):
    """Issue 3 POST /echo and 2 GET /sum calls."""
    for _ in range(3):
        client.post("/echo", json={"message": "hi"})
    for _ in range(2):
        client.get("/sum", params={"a": 1, "b": 2})


# --- Metric-surface tests ----------------------------------------------------

def test_metrics_endpoint_returns_200():
    """GET /metrics returns 200.

    Catches buggy variant: learner forgot to mount /metrics -> 404.
    """
    client = _get_client()
    resp = client.get("/metrics")
    assert resp.status_code == 200, (
        f"GET /metrics returned {resp.status_code}; expected 200. "
        "Did you mount /metrics with make_asgi_app()?"
    )


def test_three_metric_families_declared():
    """Body of /metrics contains all three metric names.

    Catches buggy variant: learner declared only two metrics.
    """
    client = _get_client()
    body = _scrape_metrics(client)
    missing = [
        name for name in ("requests_total", "request_latency_seconds", "inflight_requests")
        if name not in body
    ]
    assert not missing, (
        f"Missing metric families in /metrics body: {missing}. "
        "Declare all three at module scope."
    )


def test_request_counter_increments():
    """After traffic, the relevant counter samples >= the call counts.

    Catches buggy variant: counter middleware never wired, or wired after
    response sent.
    """
    client = _get_client()
    _drive_traffic(client)
    body = _scrape_metrics(client)
    echo_value = _counter_sample_for(body, "/echo")
    sum_value = _counter_sample_for(body, "/sum")
    assert echo_value >= 3, (
        f'requests_total{{path="/echo",status="200"}} was {echo_value}; '
        "expected >= 3 after 3 POST /echo calls."
    )
    assert sum_value >= 2, (
        f'requests_total{{path="/sum",status="200"}} was {sum_value}; '
        "expected >= 2 after 2 GET /sum calls."
    )


def test_latency_histogram_present():
    """After traffic, request_latency_seconds_bucket samples exist.

    Catches buggy variant: histogram declared but .observe() never called.
    """
    client = _get_client()
    _drive_traffic(client)
    body = _scrape_metrics(client)
    pattern = re.compile(
        r'^request_latency_seconds_bucket\{[^}]+\}\s+([0-9.eE+-]+)',
        re.MULTILINE,
    )
    samples = [float(m) for m in pattern.findall(body)]
    assert samples, (
        "request_latency_seconds_bucket has no samples. Is the histogram "
        "declared at module scope and is MetricsMiddleware calling .observe(elapsed)?"
    )
    assert max(samples) > 0, (
        "All request_latency_seconds_bucket samples are 0. The histogram is "
        "declared but .observe() is not being called."
    )


def test_middleware_order_request_id_outermost():
    """RequestIdMiddleware must be the outermost wired middleware.

    Catches buggy variant: learner reversed the add_middleware order, which
    breaks request-id propagation (the structured-logging middleware would
    try to read request_id_var before the request-id middleware has set
    it). The guide (Task 2) explicitly specifies request-id outer,
    structured-logging middle, metrics inner.
    """
    client = _get_client()
    # Starlette's add_middleware does user_middleware.insert(0, ...), so the
    # LAST add_middleware call ends up at INDEX 0 of user_middleware — which
    # is the OUTERMOST layer of the runtime stack. Read user_middleware as
    # outermost -> innermost.
    from app import app as fastapi_app

    names = [m.cls.__name__ for m in fastapi_app.user_middleware]
    assert "RequestIdMiddleware" in names, (
        "RequestIdMiddleware is not wired onto the app at all. "
        "Did you call app.add_middleware(RequestIdMiddleware)?"
    )
    assert names[0] == "RequestIdMiddleware", (
        "RequestIdMiddleware must be the outermost middleware (the LAST "
        f"app.add_middleware call). Observed order (outermost->innermost): {names}. "
        "Expected MetricsMiddleware first, then StructuredLoggingMiddleware, "
        "then RequestIdMiddleware."
    )


def test_inflight_gauge_present():
    """inflight_requests metric line is present in /metrics output.

    Catches buggy variant: gauge declared inside a function (wrong scope).
    """
    client = _get_client()
    body = _scrape_metrics(client)
    assert "inflight_requests" in body, (
        "inflight_requests is missing from /metrics. Declare the Gauge at "
        "module scope so it is registered exactly once."
    )


def test_inflight_gauge_returns_to_zero_after_traffic():
    """After synchronous traffic completes, inflight_requests reads exactly 0.

    Catches buggy variant: learner declared the Gauge at module scope (so
    it appears in /metrics, which the prior test accepts) but never wired
    inc() / dec() into MetricsMiddleware, or wired them without
    try/finally so an exception leaks an in-flight count. In both cases
    the gauge drifts -- the value after a clean batch of completed
    requests should be 0.
    """
    import re

    client = _get_client()
    _drive_traffic(client)
    body = _scrape_metrics(client)
    # Sample lines for an unlabelled Gauge look like:
    #   inflight_requests 0.0
    pattern = re.compile(r"^inflight_requests\s+([0-9.eE+-]+)\s*$", re.MULTILINE)
    m = pattern.search(body)
    assert m, (
        "Could not find an `inflight_requests <value>` sample line in /metrics. "
        "Is the Gauge declared at module scope?"
    )
    value = float(m.group(1))
    assert value == 0.0, (
        f"After {3 + 2} completed requests, inflight_requests is {value}, not 0. "
        "Wire MetricsMiddleware to call inflight_requests.inc() on entry and "
        "inflight_requests.dec() in a try/finally so an unhandled exception "
        "still decrements the gauge."
    )


def test_request_id_header_set():
    """Every non-/metrics response has X-Request-ID header set non-empty.

    Catches buggy variant: request-id middleware missing or header on
    request.headers instead of response.headers.
    """
    client = _get_client()
    resp_echo = client.post("/echo", json={"message": "hi"})
    resp_sum = client.get("/sum", params={"a": 1, "b": 2})
    for resp, label in [(resp_echo, "POST /echo"), (resp_sum, "GET /sum")]:
        header = resp.headers.get("x-request-id") or resp.headers.get("X-Request-ID")
        assert header, (
            f"{label} response missing X-Request-ID header. "
            "Is RequestIdMiddleware wired and setting response headers?"
        )


def test_structured_log_emitted(caplog):
    """After a request, one JSON log line is captured with the required keys.

    Catches buggy variant: structured-logging middleware not wired; used
    print(...) instead of the logging module.
    """
    client = _get_client()
    with caplog.at_level(logging.INFO):
        client.get("/sum", params={"a": 1, "b": 2})

    parsed = []
    for record in caplog.records:
        try:
            parsed.append(json.loads(record.getMessage()))
        except (ValueError, TypeError):
            continue

    assert parsed, (
        "No JSON-parseable log line was captured. Is StructuredLoggingMiddleware "
        "emitting one JSON line per response via the logging module (not print)?"
    )
    required = {"request_id", "path", "status", "latency_ms"}
    matched = [p for p in parsed if required.issubset(set(p.keys()))]
    assert matched, (
        f"No JSON log line contained all required keys {sorted(required)}. "
        f"Found JSON lines with keys: {[sorted(p.keys()) for p in parsed]}."
    )


# --- Learner-written-test AST + invocation gate ------------------------------

def _function_has_assertion(node: ast.FunctionDef) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
    return False


def _function_is_bare_pass(node: ast.FunctionDef) -> bool:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def _function_has_not_implemented_placeholder(node: ast.FunctionDef) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            is_pytest_fail = (
                isinstance(func, ast.Attribute)
                and func.attr == "fail"
                and isinstance(func.value, ast.Name)
                and func.value.id == "pytest"
            )
            if is_pytest_fail and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if "not implemented" in first.value.lower():
                        return True
    return False


def test_learner_self_test_exists_and_passes():
    """tests/test_drill.py has >= 4 substantive test functions.

    Per Learner-Written Test Rule + build-packet Section C Drill Autograder Spec.
    Catches buggy variant: learner skipped writing the self-test or left
    `pytest.fail("Not implemented")` placeholders.
    """
    assert os.path.exists(LEARNER_TEST_FILE), (
        f"Missing learner test file: {LEARNER_TEST_FILE}"
    )
    with open(LEARNER_TEST_FILE) as fh:
        tree = ast.parse(fh.read(), filename=LEARNER_TEST_FILE)

    test_funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    assert len(test_funcs) >= MIN_LEARNER_TESTS, (
        f"tests/test_drill.py has {len(test_funcs)} test functions; "
        f"required at least {MIN_LEARNER_TESTS}."
    )

    problems = []
    for fn in test_funcs:
        if _function_is_bare_pass(fn):
            problems.append(
                f"{fn.name}: body is bare `pass` (pytest counts this as PASSING; write an actual test)"
            )
            continue
        if _function_has_not_implemented_placeholder(fn):
            problems.append(
                f'{fn.name}: still contains `pytest.fail("Not implemented...")` placeholder'
            )
            continue
        if not _function_has_assertion(fn):
            problems.append(f"{fn.name}: has no `assert` statement")

    assert not problems, "Learner test issues:\n  - " + "\n  - ".join(problems)
