# tests/test_memory_router.py
from unittest.mock import MagicMock, patch  # noqa: F401  (kept verbatim from brief)

import pytest
from fastapi.testclient import TestClient


class TestKnowledgeEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        from fastapi import FastAPI

        from live_edit.config import KnowledgeConfig, MemoryConfig
        from live_edit.memory import MemoryManager
        from live_edit.router import setup_live_edit

        # Write a minimal config
        config_path = tmp_path / ".live-edit.toml"
        config_path.write_text("""
[project]
name = "TestApp"
language = "python"
root = "."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.example.com/v1/messages"
api_key_env = "FAKE_KEY"
model = "test-model"

[safety]
allowed_dirs = ["."]

[timeouts]
api_request = 180
shell_command = 30

[sessions]
max_active = 10

[hooks]

[ui]
default_mode = "quick"

[modes.quick]
label = "快速修改"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]

[modes.quick.prompt]
base = "You are a helpful AI."
user_persona = "Non-technical user."
communication_rules = "Use Chinese."

[modes.deep]
label = "深度开发"
approval = "final"
tools = "all"

[modes.deep.prompt]
base = "You are a dev assistant."
user_persona = "Developer."
communication_rules = "Use technical terms."

[errors.quick]
"old_string 在文件中未找到" = "文件内容已变化"
[errors.deep]
""")

        class FakeEmbedder:
            def embed(self, text):
                return [0.5] * 384

            def embed_batch(self, texts):
                return [[0.5] * 384 for _ in texts]

            @property
            def dimension(self):
                return 384

        class FakeStorage:
            def _get_conn(self):
                return self

            def store_knowledge_chunks(self, source_path, chunks):
                pass

            def upsert_knowledge_meta(self, source_path, source_type, file_hash, chunk_count):
                pass

            def delete_knowledge_chunks(self, source_path):
                pass

            def delete_knowledge_meta(self, source_path):
                pass

            def list_knowledge_meta(self):
                return [
                    {
                        "source_path": "api:test",
                        "source_type": "api",
                        "file_hash": None,
                        "chunk_count": 1,
                        "created_at": "2026-01-01",
                        "updated_at": "2026-01-01",
                    },
                ]

        storage = FakeStorage()
        embedder = FakeEmbedder()
        cfg = MemoryConfig(enabled=True, knowledge=KnowledgeConfig(enabled=True, api_enabled=True))
        mgr = MemoryManager(storage, embedder, cfg)

        router = setup_live_edit(project_root=str(tmp_path), config_path=str(config_path))

        app = FastAPI()
        app.include_router(router)
        app.state.memory_manager = mgr

        return TestClient(app)

    def test_upload_knowledge(self, client):
        resp = client.post(
            "/live-edit/knowledge",
            json={
                "source_path": "api:rules",
                "content": "All commits must be signed.",
                "metadata": '{"tag": "git"}',
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_upload_rejects_non_api_prefix(self, client):
        resp = client.post(
            "/live-edit/knowledge",
            json={
                "source_path": "myfile.md",
                "content": "test",
                "metadata": "{}",
            },
        )
        assert resp.status_code == 400

    def test_list_knowledge(self, client):
        resp = client.get("/live-edit/knowledge")
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data

    def test_delete_knowledge(self, client):
        resp = client.delete("/live-edit/knowledge/api:test")
        assert resp.status_code == 200
