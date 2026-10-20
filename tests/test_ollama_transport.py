"""서비스 단위 테스트: 외부 계정·실모델·ignored scripts 없이 실행되어야 한다.
"""
import asyncio
import json

import httpx
import pytest

from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM, ROUTE_SCHEMA


@pytest.mark.parametrize('schema', [None, ROUTE_SCHEMA])
def test_sync_requests_disable_thinking_and_preserve_schema(monkeypatch, schema):
    def respond(request):
        payload = json.loads(request.content)
        assert payload.get('think') is False, 'Ollama default thinking can prevent a final answer'
        assert payload['stream'] is False
        assert payload.get('format') == schema
        return httpx.Response(200, json={'message': {'content': '완료'},
                                         'prompt_eval_count': 12, 'eval_count': 8})

    def post(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, 'post', post)
    reply, usage = OllamaFastLLM('qwen3:0.6b', 0.001)._chat('system', 'facts', schema)
    assert reply == '완료'
    assert usage['tokens'] == 20


@pytest.mark.parametrize('method', ['chat', 'write'])
def test_stream_requests_disable_thinking_and_keep_final_tokens(monkeypatch, method):
    def respond(request):
        payload = json.loads(request.content)
        assert payload.get('think') is False, 'Ollama default thinking can delay SSE answer tokens'
        assert payload['stream'] is True
        frames = [{'message': {'content': '진단 '}}, {'message': {'content': '완료'}},
                  {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 12, 'eval_count': 8}]
        return httpx.Response(200, text='\n'.join(json.dumps(f) for f in frames))

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
                        client_class(transport=httpx.MockTransport(respond), **kwargs))
    pieces = []
    if method == 'chat':
        request = OllamaFastLLM('qwen3:0.6b', 0.001).chat('안녕', [], on_token=pieces.append)
    else:
        request = OllamaQualityLLM('qwen3:8b', 0.001).write('진단', [], 1, None, on_token=pieces.append)
    reply, usage = asyncio.run(request)
    assert reply == ''.join(pieces) == '진단 완료'
    assert usage['tokens'] == 20
