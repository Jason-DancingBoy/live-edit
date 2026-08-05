from live_edit.metrics import Metrics


def test_counter_inc_and_render():
    m = Metrics()
    m.inc("live_edit_sessions_total", {"outcome": "started"})
    m.inc("live_edit_sessions_total", {"outcome": "started"})
    out = m.render()
    assert 'live_edit_sessions_total{outcome="started"} 2' in out


def test_gauge_set():
    m = Metrics()
    m.set("live_edit_active_sessions", value=3)
    assert "live_edit_active_sessions 3" in m.render()


def test_histogram_observe_renders_count_sum_buckets():
    m = Metrics()
    m.observe("live_edit_llm_duration_seconds", 0.5)
    m.observe("live_edit_llm_duration_seconds", 2.0)
    out = m.render()
    assert "live_edit_llm_duration_seconds_count 2" in out
    assert "live_edit_llm_duration_seconds_sum 2.5" in out
    # 0.5 <= 0.5 bucket, 2.0 <= 2.5 bucket
    assert 'live_edit_llm_duration_seconds_bucket{le="0.5"} 1' in out
    assert 'live_edit_llm_duration_seconds_bucket{le="2.5"} 2' in out
    assert 'live_edit_llm_duration_seconds_bucket{le="+Inf"} 2' in out


def test_histogram_labeled():
    m = Metrics()
    m.observe("live_edit_tool_duration_ms", 3, {"tool": "edit_file"})
    out = m.render()
    assert 'live_edit_tool_duration_ms_count{tool="edit_file"} 1' in out


def test_concurrent_increments_are_thread_safe():
    import threading

    m = Metrics()
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(200):
                m.inc("live_edit_sessions_total", {"outcome": "started"})
                m.render()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert "live_edit_sessions_total{outcome=\"started\"} 800" in m.render()
