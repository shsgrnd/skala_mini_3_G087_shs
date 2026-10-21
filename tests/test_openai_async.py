"""서비스 단위 테스트: OpenAI 응답 대기 취소가 HTTP 요청과 callback 경계까지 전달된다."""
import asyncio
import json

import httpx
import pytest

from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM


def invoke(llm, operation, callback):
    if operation == 'chat':
        return llm.chat('안녕하세요', [], on_token=callback)
    return llm.write('payments-service 장애 진단', [], 1, None, on_token=callback)


@pytest.mark.parametrize('operation', ['chat', 'write'])
def test_cancelled_answer_closes_inflight_request_without_callback(operation, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    async def scenario():
        entered, cancelled, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        callbacks = []
        class WaitingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            async def aclose(self):
                closed.set()
        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=WaitingTransport(), **kwargs))
        # 이전 동기 경로는 즉시 끝나므로 대기 진입 검사가 실제 결함을 잡는다.
        monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(200,
            json={'choices':[{'message':{'content':'완성 답변'}}], 'usage':{'total_tokens':27}},
            request=httpx.Request('POST', 'https://api.openai.com/v1/chat/completions')))
        cls = OpenAIFastLLM if operation == 'chat' else OpenAIQualityLLM
        task = asyncio.create_task(invoke(cls('unit-model', 1), operation, callbacks.append))
        waiting = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait({task, waiting}, timeout=.5, return_when=asyncio.FIRST_COMPLETED)
            assert entered.is_set(), 'Answer must await asynchronous HTTP instead of finishing through sync HTTP'
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set() and closed.is_set()
            assert callbacks == []
        finally:
            task.cancel(); waiting.cancel()
            await asyncio.gather(task, waiting, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['chat', 'write'])
@pytest.mark.parametrize('status', [200, 401])
def test_async_answer_preserves_usage_and_http_errors(operation, status, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    def forbidden_sync(*args, **kwargs):
        pytest.fail('Answer path must not call synchronous HTTP')
    monkeypatch.setattr(httpx, 'post', forbidden_sync)
    async def scenario():
        observed = []
        async def respond(request):
            payload = json.loads(request.content)
            assert payload['model'] == 'unit-model' and payload['temperature'] == 0
            return httpx.Response(status, json={'choices':[{'message':{'content':'완성 답변'}}], 'usage':{'total_tokens':27}})
        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
        cls = OpenAIFastLLM if operation == 'chat' else OpenAIQualityLLM
        coroutine = invoke(cls('unit-model', 1), operation, observed.append)
        if status == 401:
            with pytest.raises(httpx.HTTPStatusError):
                await coroutine
            assert observed == []
        else:
            reply, usage = await coroutine
            assert reply == '완성 답변' and observed == ['완성 답변']
            assert usage['tokens'] == 27 and usage['usd'] == .027
            assert '[SYSTEM]' in usage['prompt']
    asyncio.run(scenario())
