"""Build the local runbook index: python -m agent.rag.indexer."""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from agent.config import ROOT, load_config

RUNBOOKS = ROOT / 'data' / 'runbooks'
QUERY_PREFIX = 'query: '
PASSAGE_PREFIX = 'passage: '
REBUILD = 'Run: python -m agent.rag.indexer'


def read_runbooks(path: Path = RUNBOOKS) -> list[dict]:
    docs = []
    for file in sorted(path.glob('*.md')):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', file.stem):
            raise ValueError(f'Invalid runbook name: {file.name}')
        content = file.read_bytes()
        body = content.decode('utf-8').strip()
        if not body:
            raise ValueError(f'Empty runbook: {file.name}')
        docs.append({'incident_type': file.stem, 'title': body.splitlines()[0].lstrip('# ').strip(),
                     'body': body, 'source': f'data/runbooks/{file.name}',
                     'sha256': hashlib.sha256(content).hexdigest()})
    if not docs:
        raise ValueError(f'No runbooks in {path}')
    return docs


def index_identity(settings, docs: list[dict]) -> dict:
    identity = {'schema': 1, 'model': settings.model, 'revision': settings.revision,
                'dimension': settings.dimension, 'query_prefix': QUERY_PREFIX,
                'passage_prefix': PASSAGE_PREFIX, 'max_seq_length': 512,
                'distance': 'cosine', 'score': '1 - distance',
                'documents': [{k: d[k] for k in ('incident_type', 'source', 'sha256')} for d in docs]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {**identity, 'fingerprint': digest, 'collection': f'runbooks-{digest[:32]}'}


def load_encoder(settings, *, local_only: bool):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(settings.model, revision=settings.revision,
                                local_files_only=local_only, trust_remote_code=False, device='cpu')
    model.max_seq_length = 512
    if model.get_embedding_dimension() != settings.dimension:
        raise ValueError('Embedding dimension does not match config')
    return model


def chroma_client(path: Path):
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def build_index(settings, path: Path = RUNBOOKS, *, encoder=None) -> dict:
    started = time.perf_counter()
    docs = read_runbooks(path)
    manifest = index_identity(settings, docs)
    encoder = encoder if encoder is not None else load_encoder(settings, local_only=False)
    vectors = encoder.encode([PASSAGE_PREFIX + d['body'] for d in docs], normalize_embeddings=True)
    client = chroma_client(settings.index_path)
    # A new corpus gets a separate collection so a failed rebuild leaves the active index intact.
    collection = client.get_or_create_collection(manifest['collection'], embedding_function=None,
                                                configuration={'hnsw': {'space': 'cosine'}})
    collection.upsert(ids=[d['incident_type'] for d in docs], embeddings=vectors,
                      documents=[d['body'] for d in docs],
                      metadatas=[{k: v for k, v in d.items() if k != 'body'} for d in docs])
    manifest.update(top_k=settings.top_k, threshold=settings.threshold, margin=settings.margin,
                    build_ms=round((time.perf_counter() - started) * 1000, 3))
    target = settings.index_path / 'manifest.json'
    temp = target.with_name(f'manifest.{os.getpid()}.tmp')
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    temp.replace(target)
    return manifest


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    print(json.dumps(build_index(load_config().rag), ensure_ascii=False, indent=2))
