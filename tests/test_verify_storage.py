# tests/test_verify_storage.py
import json

from live_edit.storage import SQLiteStorage, Storage


def test_sqlite_save_get_roundtrip(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    ev = json.dumps({"session_id": "s1", "decision": "auto_approve"})
    st.save_evidence("s1", ev)
    assert st.get_evidence("s1") == ev


def test_get_missing_returns_none(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    assert st.get_evidence("nope") is None


def test_save_overwrites(tmp_path):
    st = SQLiteStorage(str(tmp_path / "s.db"))
    st.save_evidence("s1", "one")
    st.save_evidence("s1", "two")
    assert st.get_evidence("s1") == "two"


def test_abstract_default_is_noop():
    class Noop(Storage):
        def save_session(self, *a, **k): ...
        def get_sessions(self, *a, **k):
            return []

        def get_session_detail(self, *a, **k):
            return None

        def store_embedding(self, *a, **k): ...
        def query_embeddings(self, *a, **k):
            return []

        def delete_old_embeddings(self, *a, **k): ...

    st = Noop()
    st.save_evidence("s1", "{}")  # 不应抛
    assert st.get_evidence("s1") is None
