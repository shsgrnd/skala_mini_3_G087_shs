import importlib.util
import pytest


def test_resolver_is_available():
    assert importlib.util.find_spec('agent.target_resolver'), 'S1 resolver is not implemented'


@pytest.mark.parametrize('query,targets,unknown,status', [
    ('payments-service CPU 급증', ['payments-service'], [], 'resolved'),
    ('gateway-service → payments-service timeout', ['gateway-service', 'payments-service'], [], 'resolved'),
    ('payments-service랑 cache-service 둘 다 장애', ['payments-service', 'cache-service'], [], 'resolved'),
    ('payments-service payments-service 장애', ['payments-service'], [], 'resolved'),
    ('로그인 응답이 느려요', [], [], 'missing'),
    ('gateway-service → billing-api 장애', ['gateway-service'], ['billing-api'], 'unknown'),
    ('payments-service cpu-spike 장애', ['payments-service'], [], 'resolved'),
    ('billing-service 응답이 느려요', [], ['billing-service'], 'unknown'),
    ('not-payments-service 장애', [], ['not-payments-service'], 'unknown'),
    ('payments-service-backup 장애', [], ['payments-service-backup'], 'unknown'),
    ('payments-service.prod 장애', [], ['payments-service.prod'], 'unknown'),
    ('payments-service.example.com 장애', [], ['payments-service.example.com'], 'unknown'),
    ('payments-service. CPU 급증', ['payments-service'], [], 'resolved'),
    ('payments_service 장애', [], ['payments_service'], 'unknown'),
    ('payments-service → billing-service 장애', ['payments-service'], ['billing-service'], 'unknown'),
])
def test_exact_names_order_and_no_unknown_fallback(query, targets, unknown, status):
    from agent.target_resolver import resolve_targets
    result = resolve_targets(query)
    assert result == {'status': status, 'targets': targets, 'unknown': unknown}


def test_monitoring_guard_drops_unrequested_targets_and_restores_missing_targets():
    from agent.target_resolver import guard_monitoring_tasks
    tasks = [{'id': 'bad', 'agent': 'research', 'kind': 'monitoring', 'target': 'auth-service', 'status': 'pending'},
             {'id': 'analysis', 'agent': 'analysis', 'kind': 'pattern-analysis', 'status': 'pending'}]
    result = guard_monitoring_tasks(tasks, ['gateway-service', 'payments-service'])
    assert [t['target'] for t in result if t['kind'] == 'monitoring'] == ['gateway-service', 'payments-service']
    assert result[-1] == tasks[-1]
    assert tasks[0]['target'] == 'auth-service'


@pytest.mark.parametrize('module,klass', [('agent.fake_llm','FastLLM'),('agent.openai_llm','OpenAIFastLLM'),('agent.ollama_llm','OllamaFastLLM')])
def test_every_planner_strictly_preserves_explicit_targets(module, klass, monkeypatch):
    from importlib import import_module
    monkeypatch.setenv('TARGET_RESOLUTION_MODE', 'strict')
    llm = getattr(import_module(module), klass)('unit-model', 0.001)
    if hasattr(llm, '_chat'):
        monkeypatch.setattr(llm, '_chat', lambda *a, **kw: ('{"needs_analysis": false, "tasks": [{"agent":"research","target":"auth-service"}]}', {'tokens':1,'usd':0}))
    result, _ = llm.plan('gateway-service → payments-service timeout', 'procedure')
    assert [t['target'] for t in result['tasks'] if t.get('kind') == 'monitoring'] == ['gateway-service', 'payments-service']


@pytest.mark.parametrize('module,klass', [('agent.fake_llm','FastLLM'),('agent.openai_llm','OpenAIFastLLM'),('agent.ollama_llm','OllamaFastLLM')])
def test_unresolved_plan_never_calls_a_model_or_adds_a_target(module, klass, monkeypatch):
    from importlib import import_module
    monkeypatch.setenv('TARGET_RESOLUTION_MODE', 'strict')
    llm = getattr(import_module(module), klass)('unit-model', 0.001)
    if hasattr(llm, '_chat'):
        def unexpected(*args, **kwargs):
            pytest.fail('Unresolved target must not invoke the model')
        monkeypatch.setattr(llm, '_chat', unexpected)
    result, usage = llm.plan('billing-service 장애', 'procedure')
    assert result['tasks'] == []
    assert usage['usd'] == 0


def test_research_rejects_unapproved_task_before_touching_mcp(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from agent.config import load_config
    from agent.graph import research_node
    monkeypatch.setenv('TARGET_RESOLUTION_MODE','strict')
    deps=SimpleNamespace(config=load_config(),mcp=object())
    state={'tasks':[{'agent':'research','kind':'monitoring','target':'auth-service','status':'pending'}],
           'target_resolution':{'targets':['payments-service']},'events':[]}
    with pytest.raises(ValueError,match='Unapproved'):
        asyncio.run(research_node(state,deps))


@pytest.mark.parametrize('query',['CPU가 뭐야?', '메모리와 디스크의 차이를 알려줘'])
def test_technical_chat_does_not_force_target_clarification(monkeypatch, query):
    import asyncio
    from types import SimpleNamespace
    from agent.config import load_config
    from agent.graph import route_node
    monkeypatch.setenv('TARGET_RESOLUTION_MODE','strict')
    router=SimpleNamespace(route=lambda q: ({'route':'chat','needs_analysis':False},{'usd':0.0}))
    deps=SimpleNamespace(config=load_config(),fast_llm=router)
    result=asyncio.run(route_node({'query':query,'events':[]},deps))
    assert result['route']=='chat'
    assert result['target_issue'] is False
