"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
"""
import json
from dataclasses import replace

import pytest

from agent.config import load_config
from agent.rag.indexer import build_index, read_runbooks
from agent.rag.retriever import IncidentRetriever


class TinyEncoder:
    def encode(self, texts, **kwargs):
        return [[1.0, 0.0] if 'cpu' in text.lower() else [0.0, 1.0] for text in texts]


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / 'runbooks'
    path.mkdir()
    (path / 'cpu-spike.md').write_text('# CPU\nCPU overload')
    (path / 'disk-full.md').write_text('# Disk\nStorage full')
    return path


@pytest.fixture
def settings(tmp_path):
    return replace(load_config().rag, index_path=tmp_path / 'index', dimension=2,
                   threshold=0.8, margin=0.1)


def test_index_is_repeatable_and_preserves_sources(corpus, settings):
    docs = read_runbooks(corpus)
    assert len(docs) == 2
    assert docs[0]['incident_type'] == 'cpu-spike'
    assert docs[0]['source'] == 'data/runbooks/cpu-spike.md'
    assert len(docs[0]['sha256']) == 64
    first = build_index(settings, corpus, encoder=TinyEncoder())
    second = build_index(settings, corpus, encoder=TinyEncoder())
    assert first['fingerprint'] == second['fingerprint']
    retriever = IncidentRetriever(settings, corpus, encoder=TinyEncoder())
    result = retriever.retrieve([{'source': 'monitoring', 'data': {'alerts': ['cpu overload']}}])
    assert result['accepted'] is True
    assert result['candidates'][0]['incident_type'] == 'cpu-spike'
    assert result['candidates'][0]['score'] == pytest.approx(1.0)
    assert result['candidates'][0]['retrieval_mode'] == 'rag'
    assert retriever.collection.count() == 2


def test_missing_and_stale_index_explain_rebuild(corpus, settings):
    with pytest.raises(ValueError, match='python -m agent.rag.indexer'):
        IncidentRetriever(settings, corpus, encoder=TinyEncoder())
    build_index(settings, corpus, encoder=TinyEncoder())
    (corpus / 'disk-full.md').write_text('# Disk\nChanged')
    with pytest.raises(ValueError, match='stale'):
        IncidentRetriever(settings, corpus, encoder=TinyEncoder())


def test_document_changes_after_startup_are_rejected(corpus, settings):
    build_index(settings, corpus, encoder=TinyEncoder())
    retriever = IncidentRetriever(settings, corpus, encoder=TinyEncoder())
    (corpus / 'cpu-spike.md').write_text('# CPU\nChanged')
    with pytest.raises(ValueError, match='stale'):
        retriever.retrieve([{'source': 'monitoring', 'data': {'alerts': ['cpu overload']}}])


def test_model_settings_invalidate_index(corpus, settings):
    build_index(settings, corpus, encoder=TinyEncoder())
    with pytest.raises(ValueError, match='stale'):
        IncidentRetriever(replace(settings, revision='different'), corpus, encoder=TinyEncoder())


def test_empty_corpus_is_invalid(tmp_path):
    with pytest.raises(ValueError, match='runbooks'):
        read_runbooks(tmp_path)


def test_empty_evidence_abstains_and_corrupt_content_fails(corpus, settings):
    build_index(settings, corpus, encoder=TinyEncoder())
    retriever = IncidentRetriever(settings, corpus, encoder=TinyEncoder())
    assert retriever.retrieve([])['accepted'] is False
    retriever.collection.update(ids=['cpu-spike'], documents=['Fabricated source'], embeddings=[[1.0, 0.0]])
    with pytest.raises(ValueError, match='Unverified'):
        retriever.retrieve([{'source': 'monitoring', 'data': {'alerts': ['cpu']}}])
