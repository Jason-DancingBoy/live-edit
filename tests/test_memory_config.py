# tests/test_memory_config.py
import pytest

from live_edit.config import (
    Config,
    KnowledgeConfig,
    LongTermConfig,
    MemoryConfig,
    SessionMemoryConfig,
    ShortTermConfig,
    parse_config,
)


def _base_toml(extra: str = "") -> str:
    """A minimal parseable .live-edit.toml with optional extra sections appended."""
    return f"""
[project]
name = "TestApp"
language = "python"

[llm]
api_url = "https://api.example.com"
api_key_env = "KEY"
model = "m1"

[modes.quick]
label = "Quick"
{extra}
"""


class TestShortTermConfig:
    def test_defaults(self):
        cfg = ShortTermConfig()
        assert cfg.enabled is True
        assert cfg.max_full_rounds == 3
        assert cfg.max_stripped_rounds == 7
        assert cfg.max_summary_rounds == 20
        assert cfg.summary_model == ""

    def test_raises_when_stripped_lt_full(self):
        with pytest.raises(ValueError, match="max_stripped_rounds"):
            ShortTermConfig(max_full_rounds=5, max_stripped_rounds=3)

    def test_raises_when_summary_lt_stripped(self):
        with pytest.raises(ValueError, match="max_summary_rounds"):
            ShortTermConfig(max_full_rounds=3, max_stripped_rounds=5, max_summary_rounds=4)

    def test_equal_values_ok(self):
        cfg = ShortTermConfig(max_full_rounds=3, max_stripped_rounds=3, max_summary_rounds=3)
        assert cfg.max_full_rounds == 3


class TestLongTermConfig:
    def test_defaults(self):
        cfg = LongTermConfig()
        assert cfg.enabled is False
        assert cfg.max_entries == 10
        assert cfg.similarity_threshold == 0.6
        assert cfg.max_stored_entries == 5000
        assert cfg.recency_decay_rate == 0.01
        assert cfg.hit_count_weight == 0.05
        assert cfg.coarse_recall_limit == 200

    def test_default_embedder(self):
        cfg = LongTermConfig()
        assert cfg.embedder.type == "local"
        assert cfg.embedder.model == "thenlper/gte-small"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("similarity_threshold", -0.1),
            ("similarity_threshold", 1.1),
            ("recency_decay_rate", -0.1),
            ("recency_decay_rate", 1.1),
            ("hit_count_weight", -0.1),
            ("hit_count_weight", 1.1),
        ],
    )
    def test_raises_on_out_of_range_float(self, field, value):
        with pytest.raises(ValueError, match=field):
            LongTermConfig(**{field: value})

    def test_raises_on_non_positive_int(self):
        with pytest.raises(ValueError, match="coarse_recall_limit"):
            LongTermConfig(coarse_recall_limit=0)
        with pytest.raises(ValueError, match="max_stored_entries"):
            LongTermConfig(max_stored_entries=0)


class TestKnowledgeConfig:
    def test_defaults(self):
        cfg = KnowledgeConfig()
        assert cfg.enabled is False
        assert cfg.api_enabled is False
        assert cfg.knowledge_dir == ".live-edit/knowledge"
        assert cfg.chunk_size == 500
        assert cfg.chunk_overlap == 50
        assert cfg.max_entries == 20

    def test_raises_when_overlap_gte_chunk_size(self):
        with pytest.raises(ValueError, match="chunk_overlap"):
            KnowledgeConfig(chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError, match="chunk_overlap"):
            KnowledgeConfig(chunk_size=100, chunk_overlap=150)


class TestMemoryConfig:
    def test_defaults(self):
        cfg = MemoryConfig()
        assert cfg.enabled is False
        assert isinstance(cfg.short_term, ShortTermConfig)
        assert isinstance(cfg.long_term, LongTermConfig)
        assert isinstance(cfg.knowledge, KnowledgeConfig)

    def test_nested_default_factories_isolated(self):
        m1 = MemoryConfig()
        m2 = MemoryConfig()
        m1.short_term.max_full_rounds = 99
        assert m2.short_term.max_full_rounds == 3


class TestSessionMemoryConfigAlias:
    def test_alias_is_long_term_config(self):
        assert SessionMemoryConfig is LongTermConfig

    def test_alias_constructs(self):
        cfg = SessionMemoryConfig(enabled=True, max_entries=5)
        assert cfg.enabled is True
        assert cfg.max_entries == 5


class TestParseMemoryConfig:
    def test_session_memory_only_config(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            _base_toml(
                """
[session_memory]
enabled = true
max_entries = 42
similarity_threshold = 0.75
max_stored_entries = 999
memory_prompt_template = "custom template"

[session_memory.embedder]
type = "huggingface"
model = "sentence-transformers/all-MiniLM-L6-v2"
"""
            )
        )
        config = parse_config(str(toml_path))
        lt = config.memory.long_term
        assert lt.enabled is True
        assert lt.max_entries == 42
        assert lt.similarity_threshold == 0.75
        assert lt.max_stored_entries == 999
        assert lt.memory_prompt_template == "custom template"
        assert lt.embedder.type == "huggingface"
        assert lt.embedder.model == "sentence-transformers/all-MiniLM-L6-v2"
        assert config.session_memory is lt

    def test_memory_long_term_takes_priority_over_session_memory(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            _base_toml(
                """
[session_memory]
enabled = true
max_entries = 10

[memory.long_term]
enabled = false
max_entries = 77
"""
            )
        )
        config = parse_config(str(toml_path))
        lt = config.memory.long_term
        assert lt.enabled is False
        assert lt.max_entries == 77

    def test_embedder_prefers_memory_long_term_over_session_memory(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            _base_toml(
                """
[session_memory.embedder]
type = "session-type"
model = "session-model"

[memory.long_term.embedder]
type = "openai"
model = "memory-model"
"""
            )
        )
        config = parse_config(str(toml_path))
        lt = config.memory.long_term
        assert lt.embedder.type == "openai"
        assert lt.embedder.model == "memory-model"

    def test_embedder_falls_back_to_session_memory(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(
            _base_toml(
                """
[session_memory.embedder]
type = "session-type"
model = "session-model"
"""
            )
        )
        config = parse_config(str(toml_path))
        lt = config.memory.long_term
        assert lt.embedder.type == "session-type"
        assert lt.embedder.model == "session-model"

    def test_embedder_default_when_neither_section(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(_base_toml())
        config = parse_config(str(toml_path))
        lt = config.memory.long_term
        assert lt.embedder.type == "local"
        assert lt.embedder.model == "thenlper/gte-small"

    def test_empty_memory_section_uses_defaults(self, tmp_path):
        toml_path = tmp_path / ".live-edit.toml"
        toml_path.write_text(_base_toml("\n[memory]\n"))
        config = parse_config(str(toml_path))
        memory = config.memory
        assert memory.enabled is False
        assert memory.long_term.enabled is False
        assert memory.short_term.enabled is True
        assert memory.knowledge.enabled is False

    def test_session_memory_property_setter(self):
        config = Config()
        replacement = LongTermConfig(enabled=True, max_entries=5)
        config.session_memory = replacement
        assert config.memory.long_term is replacement
