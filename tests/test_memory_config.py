# tests/test_memory_config.py
import pytest

from live_edit.config import (
    KnowledgeConfig,
    LongTermConfig,
    MemoryConfig,
    SessionMemoryConfig,
    ShortTermConfig,
)


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
