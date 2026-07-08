"""refresh_model_lookups: serve-time lag refresh glue (never-raise contract)."""
import trainer


class _StubModel:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def refresh_lookups(self, rows):
        self.calls.append(rows)
        return self.result


def test_refresh_calls_model_with_loaded_rows(monkeypatch):
    rows = [{"hour_ts": "2026-07-04T00:00:00+00:00", "house_load_mean": 500.0}]
    monkeypatch.setattr(trainer, "load_rows", lambda db_path, *, since_iso=None: rows)
    m = _StubModel()
    assert trainer.refresh_model_lookups(m, "/config/x.db") is True
    assert m.calls == [rows]


def test_refresh_no_rows_skips(monkeypatch):
    monkeypatch.setattr(trainer, "load_rows", lambda db_path, *, since_iso=None: None)
    m = _StubModel()
    assert trainer.refresh_model_lookups(m, "/config/x.db") is False
    assert m.calls == []


def test_refresh_model_without_method_is_false(monkeypatch):
    monkeypatch.setattr(trainer, "load_rows", lambda db_path, *, since_iso=None: [{"hour_ts": "t"}])
    assert trainer.refresh_model_lookups(object(), "/config/x.db") is False


def test_refresh_load_rows_exception_never_raises(monkeypatch):
    def _boom(db_path, *, since_iso=None):
        raise RuntimeError("db gone")
    monkeypatch.setattr(trainer, "load_rows", _boom)
    assert trainer.refresh_model_lookups(_StubModel(), "/config/x.db") is False


def test_refresh_none_model_is_false():
    assert trainer.refresh_model_lookups(None, "/config/x.db") is False
