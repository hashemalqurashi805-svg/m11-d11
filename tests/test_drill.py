import pytest
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_metrics_endpoint_returns_200_after_traffic():
    """GET /metrics returns 200 after 3 calls to /echo and 2 calls to /sum."""
    for _ in range(3):
        client.post("/echo", json={"message": "test"})
    for _ in range(2):
        client.get("/sum?a=1&b=2")
    
    response = client.get("/metrics")
    assert response.status_code == 200

def test_metrics_body_contains_three_metric_families():
    """The /metrics body contains requests_total, request_latency_seconds, inflight_requests."""
    response = client.get("/metrics")
    assert "requests_total" in response.text
    assert "request_latency_seconds" in response.text
    assert "inflight_requests" in response.text

def test_echo_counter_has_expected_value():
    """After 3 calls to /echo, requests_total{path="/echo",status="200"} >= 3."""
    response = client.get("/metrics")
    metrics_text = response.text
    
    # Check for the specific metric line and verify the value is >= 3.0
    for line in metrics_text.splitlines():
        if 'requests_total{path="/echo",status="200"}' in line:
            value = float(line.split()[-1])
            assert value >= 3.0
            return
            
    pytest.fail("Could not find requests_total for /echo in metrics output")

def test_x_request_id_header_set_on_every_non_metrics_response():
    """Every non-/metrics response carries a non-empty X-Request-ID header."""
    res1 = client.post("/echo", json={"message": "hi"})
    res2 = client.get("/sum?a=1&b=1")
    
    assert "X-Request-ID" in res1.headers
    assert len(res1.headers["X-Request-ID"]) > 0
    assert "X-Request-ID" in res2.headers
    assert len(res2.headers["X-Request-ID"]) > 0