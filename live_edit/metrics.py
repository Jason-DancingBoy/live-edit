"""Process-local metrics registry with Prometheus text exposition rendering."""

import threading

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


def _label_str(labels: dict) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Metrics:
    """Thread-safe counters, gauges, and histograms rendered as Prometheus text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, int] = {}
        self._gauges: dict[tuple, float] = {}
        self._histograms: dict[tuple, dict] = {}

    @staticmethod
    def _key(name: str, labels: dict) -> tuple:
        return (name, tuple(sorted(labels.items())))

    def inc(self, name: str, labels: dict | None = None, value: int = 1) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            self._counters[key] = self._counters.get(key, 0) + value

    def set(self, name: str, labels: dict | None = None, value: float = 0.0) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        labels = labels or {}
        with self._lock:
            key = self._key(name, labels)
            h = self._histograms.get(key)
            if h is None:
                h = {"count": 0, "sum": 0.0, "buckets": [0] * len(BUCKETS)}
                self._histograms[key] = h
            h["count"] += 1
            h["sum"] += float(value)
            for i, bucket in enumerate(BUCKETS):
                if value <= bucket:
                    h["buckets"][i] += 1

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_label_str(dict(labels))} {value}")
            for (name, labels), gvalue in sorted(self._gauges.items()):
                lines.append(f"{name}{_label_str(dict(labels))} {gvalue}")
            for (name, labels), h in sorted(self._histograms.items()):
                lab = dict(labels)
                lines.append(f"{name}_count{_label_str(lab)} {h['count']}")
                lines.append(f"{name}_sum{_label_str(lab)} {h['sum']}")
                for i, bucket in enumerate(BUCKETS):
                    lines.append(
                        f"{name}_bucket{_label_str({**lab, 'le': str(bucket)})} {h['buckets'][i]}"
                    )
                lines.append(f"{name}_bucket{_label_str({**lab, 'le': '+Inf'})} {h['count']}")
            return "\n".join(lines) + "\n"
