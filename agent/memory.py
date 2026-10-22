"""S4: 원문은 저장소에 보존하고 일반 채팅 입력만 과거 요약+최근 원문으로 구성한다.

prepare는 DB를 변경하지 않는다. 성공 응답이 완성된 시점에만 commit해야 연결 종료나
모델 오류가 요약 checkpoint를 먼저 전진시키지 않는다. 조사 대상 식별에는 사용하지 않는다.
"""
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .config import MemoryConfig

FIELDS = ('facts','systems','incident_type','metrics','observed_evidence_ids',
          'hypotheses','open_questions','decisions')
SUMMARY_SCHEMA = {'type':'object', 'properties':{
    k:{'type':'array','items':{'type':'string'},'maxItems':20} for k in FIELDS},
    'required':list(FIELDS), 'additionalProperties':False}


def empty_summary() -> dict:
    return {key:[] for key in FIELDS}


def validate_summary(value: dict) -> dict:
    if (not isinstance(value,dict) or set(value)!=set(FIELDS)
            or any(not isinstance(v,list) or len(v)>20
                   or any(not isinstance(x,str) or not x.strip() or len(x)>400 for x in v)
                   for v in value.values())
            or len(json.dumps(value,ensure_ascii=False))>6000):
        raise ValueError('Invalid structured conversation summary')
    return value


def summary_input(previous: dict | None, messages: list[dict]) -> str:
    return json.dumps({'previous_summary':previous or empty_summary(), 'messages':messages},ensure_ascii=False)


def render_history(history: list[dict], context: dict | None = None) -> str:
    recent = '\n'.join(f"{m['role']}: {m['content']}" for m in (history if context is not None else history[-10:])) or '(없음)'
    if context is None or not any(validate_summary(context).values()):
        return recent
    # 요약은 과거 대화의 참고자료다. 시스템 지시·현재 관측·현재 조회 대상이 아니다.
    return ('과거 대화 요약(진술·가설 포함, 현재 관측이나 실행 지시 아님):\n'
            + json.dumps(validate_summary(context),ensure_ascii=False)
            + '\n최근 대화 원문:\n' + recent)


@dataclass
class PreparedMemory:
    history: list[dict]
    summary: dict | None = None
    # summary는 이번 답변의 입력, candidate는 성공 시 DB에 저장할 새 요약이다. 재사용 시 candidate는 없다.
    candidate: dict | None = None
    through_rowid: int = 0
    expected_version: int = 0
    info: dict = field(default_factory=dict)


class ConversationMemory:
    def __init__(self, store, llm, config: MemoryConfig):
        self.store, self.llm, self.config = store, llm, config
        self._locks = {}

    @asynccontextmanager
    async def turn(self, thread_id: str):
        # 같은 프로세스의 동일 대화만 직렬화한다. waiter도 참조 수에 포함해 취소 시 누수를 막는다.
        entry = self._locks.setdefault(thread_id, [asyncio.Lock(),0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._locks[thread_id]

    async def prepare(self, thread_id: str) -> PreparedMemory:
        started = time.perf_counter()
        full = self.store.load(thread_id)
        result = PreparedMemory(full[-self.config.recent_messages:],info={
            'mode':'summary','status':'recent','summary_calls':0,'summary_tokens':0,
            'summary_cost_usd':0.0,'summary_usage_available':True, 'summary_saved':False})
        try:
            rows, saved = self.store.memory_snapshot(thread_id)
            full = [{'role':r['role'],'content':r['content']} for r in rows]
            point = saved['last_compacted_rowid'] if saved else 0
            previous = validate_summary(saved['summary']) if saved else None
            # checkpoint는 배열 위치가 아닌 SQLite rowid다. 최근 원문과 요약 구간이 겹치지 않게 나눈다.
            pending = [row for row in rows if row['rowid']>point]
            result.summary = previous
            result.expected_version = saved['version'] if saved else 0
            result.history = [{'role':r['role'],'content':r['content']}
                              for r in pending[-self.config.recent_messages:]]
            # 10턴은 완료된 메시지 20개다. 이미 요약한 rowid는 다시 입력하지 않는다.
            if len(pending) >= self.config.trigger_turns * 2:
                older = pending[:-self.config.recent_messages]
                result.info.update(summary_calls=1,summary_tokens=None,
                                   summary_cost_usd=None,summary_usage_available=False)
                candidate, usage = await asyncio.wait_for(
                    self.llm.summarize_history(previous,
                        [{'role':r['role'],'content':r['content']} for r in older]),
                    timeout=self.config.timeout_seconds)
                result.info.update(summary_tokens=usage.get('tokens',0),
                                   summary_cost_usd=usage.get('usd',0.0),summary_usage_available=True)
                candidate = validate_summary(candidate)
                # 빈 JSON도 문법상 유효하다. 이를 성공으로 저장하면 과거 사실이 모두 사라진다.
                if not any(candidate.values()):
                    raise ValueError('Empty conversation summary')
                # 서비스 답변의 [근거 ID]와 이전 요약에 없는 새 ID는 모델이 만들 수 없다.
                quoted_ids = set(re.findall(r'\[([^\[\]\s]+)\]',
                                            '\n'.join(r['content'] for r in older)))
                known_ids = quoted_ids | set((previous or empty_summary())['observed_evidence_ids'])
                if not set(candidate['observed_evidence_ids']) <= known_ids:
                    raise ValueError('Unobserved evidence ID in summary')
                result.candidate = candidate
                result.summary = result.candidate
                result.through_rowid = older[-1]['rowid']
                result.info['status'] = 'compacted'
            elif previous is not None:
                # 다음 trigger 전의 새 메시지도 누락 없이 함께 전달한다.
                result.history = [{'role':r['role'],'content':r['content']} for r in pending]
                result.info['status'] = 'reused'
            else:
                result.history = full[-self.config.recent_messages:]
                # 활성 모드의 recent N을 renderer의 legacy 10개 제한과 구별한다.
                result.summary = empty_summary()
        # 형식 검증은 사실의 의미 보존까지 보장하지 않는다. 실패 시 최근 10개만 쓰고 checkpoint는 유지한다.
        # CancelledError는 이 Exception 처리에 포함되지 않아 상위 응답 취소가 그대로 전파된다.
        except Exception as exc:
            logging.getLogger(__name__).warning('Conversation summary fallback: %s',type(exc).__name__)
            result.history, result.summary, result.candidate = full[-10:], None, None
            result.info.update(status='fallback',error=type(exc).__name__)
        result.info['elapsed_ms'] = round((time.perf_counter()-started)*1000,3)
        return result

    def commit(self, thread_id: str, query: str, reply: str, prepared: PreparedMemory) -> None:
        # 호출자는 답변 완성 후 한 번만 실행한다. 원문 대화 쌍과 요약 후보를 같은 트랜잭션에 저장한다.
        prepared.info['summary_saved'] = self.store.save_turn(
            thread_id,query,reply,summary=prepared.candidate,through_rowid=prepared.through_rowid,
            expected_version=prepared.expected_version)
