"""모델 응답 대기 중 연결 종료도 취소하고 정상 SSE 프레임은 보존한다."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('endpoint', ['api', 'proxy'])
@pytest.mark.parametrize('spec', ['2.3', '2.4'])
def test_disconnect_cancels_waiting_body(endpoint, spec, monkeypatch):
    # 응답 대기 중 disconnect 수신을 생략하면 타임아웃으로 실패한다.
    async def scenario():
        if endpoint == 'api':
            import server
            monkeypatch.setattr(server.app.state, 'deps', SimpleNamespace(), raising=False)
            response = await server.chat_stream('hello', 'test')
        else:
            from web_demo_server import proxy_stream
            response = await proxy_stream('hello', 'test')
        entered, cancelled = asyncio.Event(), asyncio.Event()
        saved = []

        async def body():
            try:
                entered.set()
                await asyncio.Event().wait()
                saved.append('incomplete turn')
                yield b'data: [DONE]\n\n'
            finally:
                cancelled.set()

        async def receive():
            await entered.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            pass

        response.body_iterator = body()
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': spec}}, receive, send), timeout=.2)
        assert cancelled.is_set()
        assert saved == []
    asyncio.run(scenario())


def test_normal_stream_keeps_frames_and_runs_background():
    from agent.streaming import DisconnectAwareStreamingResponse
    from starlette.background import BackgroundTask

    async def scenario():
        sent, completed = [], []

        async def body():
            yield 'data: hello\n\n'
            yield 'data: [DONE]\n\n'

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        response = DisconnectAwareStreamingResponse(body(), background=BackgroundTask(completed.append, True))
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send), timeout=.2)
        assert b''.join(m.get('body', b'') for m in sent) == b'data: hello\n\ndata: [DONE]\n\n'
        assert sent[-1]['more_body'] is False
        assert completed == [True]
    asyncio.run(scenario())


def test_disconnect_closes_body_suspended_after_yield():
    from agent.streaming import DisconnectAwareStreamingResponse

    async def scenario():
        sending, closed = asyncio.Event(), asyncio.Event()
        async def body():
            try:
                yield b'first frame'
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def receive():
            await sending.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body':
                sending.set()
                await asyncio.Event().wait()
        response = DisconnectAwareStreamingResponse(body())
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send), timeout=.2)
        assert closed.is_set(), 'Body iterator resources must close before the response returns'
    asyncio.run(scenario())


def test_api_disconnect_cancels_langgraph_child_before_return(tmp_path, monkeypatch):
    # 단순 응답 취소만으로는 그래프가 만든 asyncio 하위 작업이 남을 수 있다.
    for key, value in {'TARGET_RESOLUTION_MODE':'strict', 'INCIDENT_CLASSIFIER':'keyword',
                       'RESEARCH_STRATEGY':'legacy', 'MEMORY_MODE':'recent',
                       'LANGCHAIN_TRACING_V2':'false', 'LANGSMITH_TRACING':'false'}.items():
        monkeypatch.setenv(key, value)
    import server
    from agent.deps import SupervisorDeps
    from agent.fake_llm import FastLLM, QualityLLM
    from agent.graph import build_graph
    from agent.session_store import SqliteSessionStore

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        fast = FastLLM('fake', 0)
        async def waiting_chat(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        fast.chat = waiting_chat
        store = SqliteSessionStore(tmp_path / 'cancel.db')
        deps = SupervisorDeps(fast, QualityLLM('fake', 0), None, store, 'v2')
        monkeypatch.setattr(server.app.state, 'deps', deps, raising=False)
        monkeypatch.setattr(server.app.state, 'graph', build_graph(deps), raising=False)
        async def receive():
            await entered.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            pass
        try:
            response = await server.chat_stream('안녕하세요', 'cancel')
            await asyncio.wait_for(response({'type':'http', 'asgi':{'spec_version':'2.4'}}, receive, send), timeout=1)
            assert cancelled.is_set()
            assert store.load('cancel') == []
        finally:
            store.close()
    asyncio.run(scenario())


def test_body_io_failure_propagates_instead_of_looking_disconnected():
    from agent.streaming import DisconnectAwareStreamingResponse
    async def scenario():
        async def body():
            yield b'first frame'
            raise OSError('upstream failed')
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            pass
        with pytest.raises(OSError, match='upstream failed'):
            await DisconnectAwareStreamingResponse(body())({'type':'http'}, receive, send)
    asyncio.run(scenario())


def test_socket_send_failure_closes_iterator():
    from agent.streaming import DisconnectAwareStreamingResponse
    async def scenario():
        closed = []
        async def body():
            try:
                yield b'first frame'
            finally:
                closed.append(True)
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            if message['type'] == 'http.response.body':
                raise OSError('socket closed')
        await DisconnectAwareStreamingResponse(body())({'type':'http'}, receive, send)
        assert closed == [True]
    asyncio.run(scenario())
