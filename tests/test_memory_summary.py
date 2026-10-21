"""S4 서비스 단위: 원문 보존·증분 범위·실패 복구를 외부 모델 없이 검증한다."""
import asyncio
import json
import sqlite3

import pytest

from agent.session_store import SqliteSessionStore


def test_old_database_migration_and_atomic_turn(tmp_path):
    path=tmp_path/'old.db'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE messages (thread_id TEXT, role TEXT, content TEXT, created TEXT)')
    conn.execute("INSERT INTO messages VALUES ('a','user','기존 원문','old')")
    conn.commit(); conn.close()
    store=SqliteSessionStore(path)
    assert hasattr(store, 'save_turn'), 'S4 requires atomic turn persistence'
    store.save_turn('a','새 질문','새 답변')
    rows, summary=store.memory_snapshot('a')
    assert [r['content'] for r in rows]==['기존 원문','새 질문','새 답변']
    assert summary is None
    store.close()


def test_summary_cas_restart_and_thread_isolation(tmp_path):
    path=tmp_path/'sessions.db'
    store=SqliteSessionStore(path)
    assert hasattr(store, 'save_turn'), 'S4 summary transaction missing'
    store.save_turn('a','처음','응답')
    rows,_=store.memory_snapshot('a')
    point=rows[-1]['rowid']
    store.save_turn('a','둘째','답',summary={'facts':['first']},through_rowid=point,expected_version=0)
    # stale writer는 메시지 쌍은 보존하고 더 최신 요약을 덮어쓰지 않아야 한다.
    other=SqliteSessionStore(path)
    other.save_turn('a','셋째','답',summary={'facts':['stale']},through_rowid=point,expected_version=0)
    other.save_turn('b','다른 대화','답')
    store.close(); other.close()
    reopened=SqliteSessionStore(path)
    rows,saved=reopened.memory_snapshot('a')
    assert len(rows)==6 and saved['version']==1
    assert saved['summary']=={'facts':['first']}
    assert saved['last_compacted_rowid']==point
    assert reopened.memory_snapshot('b')[1] is None
    reopened.close()


def test_incremental_summary_excludes_recent_and_is_committed_only_on_success(tmp_path):
    from agent.memory import ConversationMemory
    from agent.config import MemoryConfig
    from agent.fake_llm import FastLLM
    async def scenario():
        store=SqliteSessionStore(tmp_path/'memory.db')
        llm=FastLLM('fake',0)
        memory=ConversationMemory(store,llm,MemoryConfig(trigger_turns=2,recent_messages=2))
        for i in range(2): store.save_turn('a',f'질문{i}',f'답{i}')
        prepared=await memory.prepare('a')
        assert len(prepared.history)==2
        assert prepared.summary['facts']==['질문0']
        assert store.memory_snapshot('a')[1] is None
        memory.commit('a','새 질문','응답',prepared)
        saved=store.memory_snapshot('a')[1]
        assert saved['last_compacted_rowid']==2 and saved['version']==1
        prepared2=await memory.prepare('a')
        assert prepared2.summary['facts']==['질문0','질문1']
        assert prepared2.info['summary_calls']==1
        assert len(store.load('a'))==6
        store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['error','invalid','timeout'])
def test_summary_failure_falls_back_without_advancing_checkpoint(tmp_path,kind):
    from agent.memory import ConversationMemory
    from agent.config import MemoryConfig
    class Broken:
        async def summarize_history(self,previous,messages):
            if kind=='error': raise RuntimeError('private contents must not be exposed')
            if kind=='timeout': await asyncio.sleep(1)
            return {'facts':'wrong type'}, {'tokens':7,'usd':0}
    async def scenario():
        store=SqliteSessionStore(tmp_path/'fallback.db')
        for i in range(6): store.save_turn('a',f'q{i}',f'a{i}')
        memory=ConversationMemory(store,Broken(),MemoryConfig(trigger_turns=2,recent_messages=2,timeout_seconds=.01))
        prepared=await memory.prepare('a')
        assert prepared.info['status']=='fallback'
        assert len(prepared.history)==10 and prepared.summary is None
        memory.commit('a','새 질문','완료',prepared)
        assert store.memory_snapshot('a')[1] is None
        assert len(store.load('a'))==14
        assert 'private contents' not in json.dumps(prepared.info)
        store.close()
    asyncio.run(scenario())


def test_thread_lock_serializes_and_cancellation_releases_it(tmp_path):
    from agent.memory import ConversationMemory
    from agent.config import MemoryConfig
    from agent.fake_llm import FastLLM
    async def scenario():
        memory=ConversationMemory(SqliteSessionStore(tmp_path/'locks.db'),FastLLM('fake',0),MemoryConfig())
        entered=asyncio.Event()
        async def hold():
            async with memory.turn('a'):
                entered.set()
                await asyncio.Event().wait()
        task=asyncio.create_task(hold()); await entered.wait()
        async with memory.turn('b'): pass
        waiter=asyncio.create_task(hold()); await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        async with memory.turn('a'): pass
        assert memory._locks=={}
        memory.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('backend',['ollama','openai'])
def test_real_adapter_sends_summary_without_dropping_uncompacted_messages(monkeypatch,backend):
    from agent.memory import empty_summary
    captured={}
    if backend=='ollama':
        from agent.ollama_llm import OllamaFastLLM
        llm=OllamaFastLLM('unit',0)
        async def send(system,user,on_token=None):
            captured['user']=user
            return '답', {'usd':0}
        monkeypatch.setattr(llm,'_chat_stream',send)
    else:
        from agent.openai_llm import OpenAIFastLLM
        llm=OpenAIFastLLM('unit',0)
        async def send(system,user,**kwargs):
            captured['user']=user
            return '답', {'usd':0}
        monkeypatch.setattr(llm,'_achat',send)
    summary=empty_summary(); summary['decisions']=['승인 후 재시작']
    history=[{'role':'user','content':f'old-{i}'} for i in range(15)]
    asyncio.run(llm.chat('지금 질문',history,history_context=summary))
    assert '승인 후 재시작' in captured['user']
    assert 'old-0' in captured['user'] and 'old-14' in captured['user']
    assert '지금 질문' in captured['user']


def test_memory_mode_and_limits_validate_before_startup(tmp_path,monkeypatch):
    from agent.config import load_config, DEFAULT_CONFIG_PATH
    monkeypatch.setenv('MEMORY_MODE','invalid')
    with pytest.raises(ValueError,match='MEMORY_MODE'): load_config()
    monkeypatch.setenv('MEMORY_MODE','summary')
    data=json.loads(DEFAULT_CONFIG_PATH.read_text()); data['memory']={'recent_messages':True}
    path=tmp_path/'config.json'; path.write_text(json.dumps(data))
    monkeypatch.setenv('AGENT_CONFIG_PATH',str(path))
    with pytest.raises(ValueError,match='memory'): load_config()


def test_summary_reuse_keeps_every_new_message_and_failure_rolls_back_turn(tmp_path):
    from agent.memory import ConversationMemory
    from agent.config import MemoryConfig
    from agent.fake_llm import FastLLM
    async def scenario():
        store=SqliteSessionStore(tmp_path/'reuse.db')
        memory=ConversationMemory(store,FastLLM('fake',0),MemoryConfig(trigger_turns=3,recent_messages=2))
        store.save_turn('a','first','reply')
        before=await memory.prepare('a')
        assert len(before.history)==2
        store.save_turn('a','second','reply'); store.save_turn('a','third','reply')
        p=await memory.prepare('a'); memory.commit('a','fourth','reply',p)
        reused=await memory.prepare('a')
        assert reused.info['summary_calls']==0
        assert [m['content'] for m in reused.history]==['third','reply','fourth','reply']
        with pytest.raises(ValueError):
            store.save_turn('a','rollback','reply',summary={},through_rowid=9999)
        assert len(store.load('a'))==8
        assert store.memory_snapshot('a')[1]['version']==1
        store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('backend',['ollama','openai'])
def test_summary_adapter_transport_and_usage(monkeypatch,backend):
    import httpx
    from agent.memory import empty_summary
    summary=empty_summary(); summary['decisions']=['승인 전 재시작 금지']
    def respond(request):
        payload=json.loads(request.content)
        assert payload['messages'][0]['content']=='unit summary policy'
        assert '승인 전 재시작 금지' in payload['messages'][1]['content']
        assert '오래된 사용자 진술' in payload['messages'][1]['content']
        if backend=='ollama':
            assert payload['think'] is False and payload['stream'] is False
            return httpx.Response(200,json={'message':{'content':json.dumps(summary)},'prompt_eval_count':100,'eval_count':20})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(summary)}}],
                                         'usage':{'total_tokens':120,'prompt_tokens':100,'completion_tokens':20}})
    cls=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:cls(transport=httpx.MockTransport(respond),**kwargs))
    if backend=='ollama':
        from agent.ollama_llm import OllamaFastLLM
        llm=OllamaFastLLM('unit',.001)
    else:
        from agent.openai_llm import OpenAIFastLLM
        llm=OpenAIFastLLM('unit',.001); llm.api_key='unit-key'
    from dataclasses import replace
    llm.config=replace(llm.config,prompts={**llm.config.prompts,'summarize_history':'unit summary policy'})
    result,usage=asyncio.run(llm.summarize_history(summary,[{'role':'user','content':'오래된 사용자 진술'}]))
    assert result['decisions']==['승인 전 재시작 금지']
    assert usage['tokens']==120 and usage['usd']==.00012


def test_reused_summary_survives_corruption_and_no_original_deleted(tmp_path):
    from agent.memory import ConversationMemory
    from agent.config import MemoryConfig
    from agent.fake_llm import FastLLM
    async def scenario():
        store=SqliteSessionStore(tmp_path/'corrupt.db')
        store.save_turn('a','제 이름은 민수입니다','안녕하세요')
        store.conn.execute("INSERT INTO thread_summaries VALUES ('a','invalid json',2,'now',1)")
        store.conn.commit()
        p=await ConversationMemory(store,FastLLM('fake',0),MemoryConfig()).prepare('a')
        assert p.info['status']=='fallback' and p.summary is None
        assert p.history[0]['content']=='제 이름은 민수입니다'
        assert len(store.load('a'))==2
        store.close()
    asyncio.run(scenario())


def test_configured_recent_window_before_first_compaction_reaches_model(tmp_path):
    from agent.memory import ConversationMemory, render_history
    from agent.config import MemoryConfig
    from agent.fake_llm import FastLLM
    async def scenario():
        store=SqliteSessionStore(tmp_path/'window.db')
        for i in range(8): store.save_turn('a',f'q{i}',f'a{i}')
        p=await ConversationMemory(store,FastLLM('fake',0),MemoryConfig(recent_messages=16)).prepare('a')
        text=render_history(p.history,p.summary)
        assert len(p.history)==16
        assert 'q0' in text and 'a7' in text
        assert p.info['summary_calls']==0
        store.close()
    asyncio.run(scenario())


def test_empty_summary_does_not_advance_watermark(tmp_path):
    from agent.memory import ConversationMemory, empty_summary
    from agent.config import MemoryConfig
    class Empty:
        async def summarize_history(self,previous,messages):
            return empty_summary(), {'tokens':55,'usd':.001}
    async def scenario():
        store=SqliteSessionStore(tmp_path/'empty.db')
        for i in range(10): store.save_turn('a',f'중요 메모 {i}','확인')
        memory=ConversationMemory(store,Empty(),MemoryConfig())
        p=await memory.prepare('a')
        assert p.info['status']=='fallback'
        assert p.info['summary_tokens']==55
        memory.commit('a','다음 질문','응답',p)
        assert store.memory_snapshot('a')[1] is None
        assert len(store.load('a'))==22
        store.close()
    asyncio.run(scenario())


def test_fake_name_correction_prefers_recent_original_over_summary():
    from agent.memory import empty_summary
    from agent.fake_llm import FastLLM
    summary=empty_summary(); summary['facts']=['제 이름은 민수입니다']
    reply,_=asyncio.run(FastLLM('fake',0).chat('제 이름이 뭐였죠?',
        [{'role':'user','content':'이름을 정정합니다. 제 이름은 영희입니다'}],history_context=summary))
    assert '영희' in reply and '민수' not in reply


@pytest.mark.parametrize('invented',[True,False])
def test_summary_cannot_introduce_unseen_evidence_ids(tmp_path,invented):
    from agent.memory import ConversationMemory, empty_summary
    from agent.config import MemoryConfig
    class Summary:
        async def summarize_history(self,previous,messages):
            value=empty_summary()
            value['facts']=['사용자는 민수라고 말했다']
            value['observed_evidence_ids']=['1','2'] if invented else ['monitoring:abc123']
            return value,{'tokens':20,'usd':0}
    async def scenario():
        store=SqliteSessionStore(tmp_path/'ids.db')
        store.save_turn('a','제 이름은 민수입니다','합성 관측 [monitoring:abc123]')
        store.save_turn('a','다음','확인')
        memory=ConversationMemory(store,Summary(),MemoryConfig(trigger_turns=2,recent_messages=2))
        p=await memory.prepare('a')
        assert p.info['status']==('fallback' if invented else 'compacted')
        memory.commit('a','질문','응답',p)
        assert (store.memory_snapshot('a')[1] is None)==invented
        store.close()
    asyncio.run(scenario())
