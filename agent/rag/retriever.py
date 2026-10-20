"""Rank runbooks; callers still fetch selected content through MCP Resource."""
import json
import time

from .indexer import (RUNBOOKS, QUERY_PREFIX, REBUILD, chroma_client, index_identity,
                      load_encoder, read_runbooks)


def monitoring_query(collected: list[dict]) -> str:
    return ' '.join(' '.join([str(c['data'].get('status', '')),
                             *c['data'].get('alerts', [])]).strip()
                    for c in collected if c.get('source') == 'monitoring').strip()


def decide(candidates: list[dict], threshold: float, margin: float) -> bool:
    return bool(candidates and candidates[0]['score'] >= threshold
                and (len(candidates) == 1 or candidates[0]['score'] - candidates[1]['score'] >= margin))


class IncidentRetriever:
    def __init__(self, settings, path=RUNBOOKS, *, encoder=None):
        started = time.perf_counter()
        self.settings, self.path = settings, path
        self.identity = index_identity(settings, read_runbooks(path))
        try:
            manifest = json.loads((settings.index_path / 'manifest.json').read_text())
            if manifest['fingerprint'] != self.identity['fingerprint']:
                raise ValueError('stale index')
            self.client = chroma_client(settings.index_path)
            self.collection = self.client.get_collection(self.identity['collection'], embedding_function=None)
            if self.collection.count() != len(self.identity['documents']):
                raise ValueError('incomplete index')
            self.encoder = encoder if encoder is not None else load_encoder(settings, local_only=True)
        except Exception as exc:
            raise ValueError(f'Runbook index missing, stale or unavailable. {REBUILD}') from exc
        self.load_ms = round((time.perf_counter() - started) * 1000, 3)

    def retrieve(self, collected: list[dict]) -> dict:
        started = time.perf_counter()
        docs = read_runbooks(self.path)
        if index_identity(self.settings, docs) != self.identity:
            raise ValueError(f'Runbook index stale. {REBUILD}')
        query = monitoring_query(collected)
        candidates = []
        if query:
            vector = self.encoder.encode([QUERY_PREFIX + query], normalize_embeddings=True)
            result = self.collection.query(query_embeddings=vector,
                                           n_results=min(self.settings.top_k, self.collection.count()),
                                           include=['metadatas', 'documents', 'distances'])
            sources = {d['incident_type']: d for d in docs}
            for meta, body, distance in zip(result['metadatas'][0], result['documents'][0], result['distances'][0]):
                expected = sources.get(meta['incident_type'])
                if (expected is None or body != expected['body']
                        or any(meta.get(k) != expected[k] for k in ('source', 'sha256'))):
                    raise ValueError(f'Unverified runbook source. {REBUILD}')
                candidates.append({'incident_type': meta['incident_type'], 'score': 1 - distance,
                                   'source': meta['source'], 'excerpt': body[:240], 'retrieval_mode': 'rag'})
        accepted = decide(candidates, self.settings.threshold, self.settings.margin)
        return {'retrieval_mode': 'rag', 'accepted': accepted,
                'reason': 'accepted' if accepted else 'insufficient_confidence',
                'candidates': candidates, 'threshold': self.settings.threshold, 'margin': self.settings.margin,
                'model': self.settings.model, 'revision': self.settings.revision,
                'index_fingerprint': self.identity['fingerprint'],
                'search_ms': round((time.perf_counter() - started) * 1000, 3)}
