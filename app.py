import json, math, time

_DEFAULT_SERVICE = ""
_STATUSES = ("open", "error")


def _service(service):
    if service is None:
        return _DEFAULT_SERVICE
    if not isinstance(service, str) or not service:
        raise ValueError("service must be a non-empty string")
    return service


def _labels(labels):
    pairs = []
    try:
        for item in labels:
            k, v = item
            pairs.append((k, v))
    except (TypeError, ValueError) as exc:
        raise ValueError("labels must be key/value pairs") from exc
    try:
        duplicate = len(set(k for k, _ in pairs)) != len(pairs)
    except TypeError as exc:
        raise ValueError("label keys must be hashable") from exc
    if duplicate:
        raise ValueError("duplicate label key")
    try:
        normalized = tuple(sorted(pairs))
    except TypeError as exc:
        raise ValueError("label keys must be mutually comparable") from exc
    try:
        json.dumps([list(p) for p in normalized])
    except (TypeError, ValueError) as exc:
        raise ValueError("labels must be JSON serializable") from exc
    return normalized


class Telemetry:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.counters = {}
        self.samples = {}
        self._spans = {}

    def inc(self, name, value=1, labels=(), service=None):
        service = _service(service)
        key = (service, name, _labels(labels))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name, value, labels=(), service=None):
        service = _service(service)
        key = (service, name, _labels(labels))
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("sample value must be numeric") from exc
        if math.isnan(f) or math.isinf(f):
            raise ValueError("sample value must be finite")
        rec = self.samples.get(key)
        if rec is None:
            rec = self.samples[key] = {"values": [], "count": 0, "sum": 0.0, "min": None, "max": None}
        rec["values"].append(value)
        rec["count"] += 1
        rec["sum"] += f
        rec["min"] = f if rec["min"] is None else min(rec["min"], f)
        rec["max"] = f if rec["max"] is None else max(rec["max"], f)

    def start(self, span, parent=None, service=None):
        service = _service(service)
        self._spans[(service, span)] = {"parent": parent, "start": self.clock(), "end": None, "error": None}

    def finish(self, span, error=None, service=None):
        service = _service(service)
        rec = self._spans[(service, span)]
        rec["end"] = self.clock()
        rec["error"] = error

    def spans(self, status):
        if status not in _STATUSES:
            raise ValueError("status must be 'open' or 'error'")
        if status == "open":
            match = lambda rec: rec["end"] is None
        else:
            match = lambda rec: rec["end"] is not None and bool(rec["error"])
        return self._span_entries(match)

    def spans_by_status(self, status):
        return self.spans(status)

    def query(self, status):
        return self.spans(status)

    def _span_entries(self, match=None):
        entries = [
            {"span": span, "service": service, "parent": rec["parent"],
             "start": rec["start"], "end": rec["end"], "error": rec["error"]}
            for (service, span), rec in self._spans.items()
            if match is None or match(rec)
        ]
        entries.sort(key=lambda e: (e["service"], e["start"], e["span"]))
        return entries

    @staticmethod
    def _sample_entry(service, name, labels, rec):
        entry = {"service": service, "name": name, "labels": list(labels),
                 "values": list(rec["values"])}
        if rec["count"]:
            entry.update({
                "count": rec["count"],
                "sum": rec["sum"],
                "minimum": rec["min"],
                "maximum": rec["max"],
                "mean": rec["sum"] / rec["count"],
            })
        return entry

    def snapshot(self):
        return {
            "counters": [
                {"service": service, "name": name, "labels": list(labels), "value": value}
                for (service, name, labels), value in sorted(self.counters.items())
            ],
            "samples": [
                self._sample_entry(service, name, labels, rec)
                for (service, name, labels), rec in sorted(self.samples.items())
            ],
            "spans": self._span_entries(),
        }

    def json(self):
        return json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
