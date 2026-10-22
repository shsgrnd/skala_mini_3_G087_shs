"""all_total 통합 실습 — FastAPI 백엔드 (Wave 1·3·12·14 축 + 전체 조립).

- Wave 1  : lifespan에서 Agent 번들(MCP 연결·LLM·그래프)을 프로세스당 정확히 1회 생성,
            app.state로 모든 요청이 공유 (creation_count/instance_id로 증명)
- Wave 2  : mcp_server.py를 subprocess(stdio)로 기동해 연결 유지
- Wave 3  : GET /chat/stream — 그래프 실행 과정을 SSE 이벤트로 스트리밍
- Wave 12 : 대화 이력을 SQLite(session.db)에 저장 — 재시작해도 유지
- Wave 14 : SUPERVISOR_VERSION(v1/v2) 환경변수로 legacy/신형 파이프라인 즉시 전환
- LangSmith: 요청당 chat_request 루트 run 아래로 노드·LLM·MCP 호출이 계층 기록됨


팀 유지보수 메모:
lifespan에서 설정·retriever·MCP·그래프를 조립하고 요청 간 재사용한다. v1에서 지원하지 않는
strict/rag/multi 조합은 자원을 열기 전에 거부한다. HTTP metadata와 SSE 이벤트의 출처·실패
정보를 함께 유지하고, 두 endpoint의 대화 저장 동작을 변경할 때 함께 검증한다.
"""
import json
import os
import re
from contextlib import aclosing, asynccontextmanager

from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / API_KEY / PROJECT — import보다 먼저

from fastapi import FastAPI
from agent.streaming import DisconnectAwareStreamingResponse
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel

from agent.deps import SupervisorDeps
from agent.config import load_config
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.memory import ConversationMemory, PreparedMemory
from agent.supervisor_v1 import run_v1

CREATION_COUNT = 0   # Wave 1 — 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


class ChatRequest(BaseModel):   # Wave 1 — Pydantic 요청 검증
    message: str
    thread_id: str = "default"


def _build_llms():
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬 실모델) | openai(실제 OpenAI API)."""
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"),
                             usd_per_1k_tokens=0.00015)
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"),
                             usd_per_1k_tokens=0.00015)
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015)
    quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005)
    return backend, fast, quality


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    config = load_config()
    supervisor_version = os.environ.get('SUPERVISOR_VERSION', 'v2')
    if config.target_resolution_mode == 'strict' and supervisor_version != 'v2':
        raise ValueError('TARGET_RESOLUTION_MODE=strict requires SUPERVISOR_VERSION=v2')
    if config.research_strategy == 'multi' and supervisor_version != 'v2':
        raise ValueError('RESEARCH_STRATEGY=multi requires SUPERVISOR_VERSION=v2')
    if config.memory_mode == 'summary' and supervisor_version != 'v2':
        raise ValueError('MEMORY_MODE=summary requires SUPERVISOR_VERSION=v2')
    incident_retriever = None
    if config.incident_classifier == 'rag':
        if supervisor_version != 'v2':
            raise ValueError('INCIDENT_CLASSIFIER=rag requires SUPERVISOR_VERSION=v2')
        from agent.rag.retriever import IncidentRetriever
        incident_retriever = IncidentRetriever(config.rag)
    CREATION_COUNT += 1
    mcp = MCPClient()
    await mcp.start()   # Wave 2 — MCP subprocess는 서버 수명 동안 딱 1개
    backend, fast_llm, quality_llm = _build_llms()
    app.state.llm_backend = backend
    deps = SupervisorDeps(
        fast_llm=fast_llm,
        quality_llm=quality_llm,
        mcp=mcp,
        session_store=SqliteSessionStore(),
        version=supervisor_version,
        config=config,
        incident_retriever=incident_retriever,
    )
    if config.memory_mode == 'summary':
        deps.memory = ConversationMemory(deps.session_store, deps.fast_llm, config.memory)
    app.state.deps = deps
    app.state.graph = build_graph(deps) if deps.version == "v2" else None
    app.state.instance_id = id(deps)
    yield
    await mcp.stop()
    deps.session_store.close()
    try:   # 종료 전 LangSmith 배치 전송 플러시 — kill 시 run이 pending으로 남는 것 방지
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT,
            "supervisor_version": app.state.deps.version,
            "llm_backend": app.state.llm_backend,
            "target_resolution_mode": app.state.deps.config.target_resolution_mode,
            "config_sha256": app.state.deps.config.sha256,
            "incident_classifier": app.state.deps.config.incident_classifier,
            "research_strategy": app.state.deps.config.research_strategy,
            "memory_mode": app.state.deps.config.memory_mode,
            "rag_index_fingerprint": (app.state.deps.incident_retriever.identity['fingerprint']
                                      if app.state.deps.incident_retriever else None),
            "fast_llm": app.state.deps.fast_llm.name,
            "quality_llm": app.state.deps.quality_llm.name}


@app.get("/version")
async def version():
    return {"supervisor_version": app.state.deps.version}


# HTTP 응답의 진단 계약이다. 새 상태를 공개할 때는 SSE 이벤트에도 필요한 정보가 있는지 함께 확인한다.
def _metadata(result: dict, run_id: str | None) -> dict:
    return {
        "route": result.get("route"),
        "memory": result.get("memory", {"mode":"recent"}),
        "incident_classifier": app.state.deps.config.incident_classifier,
        "retrieval": result.get("retrieval"),
        "research_strategy": app.state.deps.config.research_strategy,
        "research_status": result.get("research_status"),
        "research_results": result.get("research_results", []),
        "evidence": result.get("evidence", []),
        "target_resolution_mode": app.state.deps.config.target_resolution_mode,
        "target_resolution": result.get('target_resolution'),
        "config_sha256": app.state.deps.config.sha256,
        "task_count": len(result.get("tasks", [])),
        "gate_removed": result.get("gate_removed", 0),
        "replan_rounds": result.get("replan_rounds", 0),
        "retry_count": result.get("retry_count", 0),
        "parallel_elapsed": result.get("parallel_elapsed"),
        "validation_attempts": result.get("validation_attempts", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "llm_backend": app.state.llm_backend,
        "supervisor_version": app.state.deps.version,
        "instance_id": app.state.instance_id,
        "langsmith_run_id": run_id,
    }


@asynccontextmanager
async def _conversation(deps, thread_id: str, message: str):
    """HTTP/SSE가 같은 준비·잠금 범위를 쓴다. yield 중 실패하면 후보는 저장하지 않는다."""
    if deps.memory is None:
        yield PreparedMemory(deps.session_store.load(thread_id),info={'mode':'recent'})
        return
    # 잠금은 요약 준비부터 생성·저장까지 유지한다. 준비만 잠그면 두 요청이 같은 이전 상태로 답할 수 있다.
    async with deps.memory.turn(thread_id):
        # 현재 운영 질문의 대상 가드 전에 요약 LLM을 호출하거나 과거 대상을 추론하지 않는다.
        if re.search(deps.config.target_required_pattern,message,re.IGNORECASE):
            prepared = PreparedMemory([],info={'mode':'summary','status':'not_used_incident',
                                               'summary_calls':0,'summary_cost_usd':0.0})
        else:
            prepared = await deps.memory.prepare(thread_id)
        yield prepared


def _conversation_state(message: str, thread_id: str, prepared: PreparedMemory) -> dict:
    return {'query':message,'thread_id':thread_id,'history':prepared.history,
            'history_context':prepared.summary,'events':[],
            'cost_usd':prepared.info.get('summary_cost_usd',0.0) or 0.0}


def _save_conversation(deps, thread_id: str, message: str, reply: str, prepared: PreparedMemory):
    # 완성된 응답만 저장한다. FinalAnswer 전 commit해 완료 직후 연결이 끊겨도 저장을 보장한다.
    if deps.memory is not None:
        deps.memory.commit(thread_id,message,reply,prepared)
    else:
        deps.session_store.save_turn(thread_id,message,reply)


@traceable(name="chat_request")
async def _handle_chat(message: str, thread_id: str) -> tuple[dict, str | None]:
    deps = app.state.deps
    async with _conversation(deps,thread_id,message) as prepared:
        if deps.version == "v2":
            result = await app.state.graph.ainvoke(_conversation_state(message,thread_id,prepared))
        else:
            result = await run_v1(deps,message,thread_id,prepared.history)
        _save_conversation(deps,thread_id,message,result['final'],prepared)
        result['memory'] = prepared.info
    rt = get_current_run_tree()
    return result, (str(rt.id) if rt else None)


@app.post("/chat")
async def chat(req: ChatRequest):
    result, run_id = await _handle_chat(req.message, req.thread_id)
    return {"reply": result["final"], "events": result.get("events", []),
            "metadata": _metadata(result, run_id)}


@app.get("/chat/stream")
async def chat_stream(message: str, thread_id: str = "default"):
    """Wave 3 — 그래프의 노드별 진행 상황을 SSE로 스트리밍.

    이벤트마다 'data: {json}\\n\\n' 형태로 내려보내고 마지막에 data: [DONE] 센티널을
    보낸다 — 프론트엔드는 \\n\\n 경계로 이벤트를 분리해 파싱해야 한다(Wave 3b).
    """
    deps = app.state.deps

    async def gen():
        async with _conversation(deps,thread_id,message) as prepared:
            if deps.version != "v2":
                # v1 recent는 비교 기준선의 기존 SSE 저장 동작을 유지한다. summary는 시작 시 거부한다.
                result = await run_v1(deps,message,thread_id,prepared.history)
                for ev in result['events']:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type':'FinalAnswer','reply':result['final']}, ensure_ascii=False)}\n\n"
            else:
                # events는 노드마다 누적 목록으로 반환된다. seen 이후만 전송해 진행 이벤트 중복을 막는다.
                seen, final, last_prompt = 0, '', None
                # custom은 실행 중 토큰, updates는 완료한 노드의 상태다. 미완료 응답은 저장하지 않는다.
                # 외부 SSE가 닫힐 때 내부 그래프의 대기 작업도 정리한다.
                async with aclosing(app.state.graph.astream(
                        _conversation_state(message,thread_id,prepared),stream_mode=['updates','custom'])) as stream:
                    async for mode,payload in stream:
                        if mode == 'custom':
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            continue
                        for node_name,delta in payload.items():
                            for ev in (delta.get('events') or [])[seen:]:
                                yield f"data: {json.dumps({**ev,'node':node_name}, ensure_ascii=False)}\n\n"
                            if delta.get('events'):
                                seen = len(delta['events'])
                            if delta.get('final'):
                                final = delta['final']
                            if delta.get('last_prompt'):
                                last_prompt = delta['last_prompt']
                if not final:
                    raise RuntimeError('Graph completed without a final answer')
                _save_conversation(deps,thread_id,message,final,prepared)
                if deps.memory is not None:
                    yield f"data: {json.dumps({'type':'ConversationMemory',**prepared.info}, ensure_ascii=False)}\n\n"
                # strict SSE에서는 모델 입력 원문을 최종 프레임에 싣지 않는다. reply와 진행 이벤트는 유지한다.
                if deps.config.target_resolution_mode == 'strict':
                    last_prompt = None
                yield f"data: {json.dumps({'type':'FinalAnswer','reply':final,'prompt':last_prompt}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return DisconnectAwareStreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
