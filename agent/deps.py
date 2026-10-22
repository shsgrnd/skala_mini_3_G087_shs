"""Wave 13 — 의존성 주입.

노드 함수가 closure의 지역 변수에 암묵적으로 기대지 않도록, supervisor가 쓰는 모든
의존성을 SupervisorDeps 하나에 명시적으로 담아 functools.partial로 각 노드에 주입한다.
파일을 어디로 옮겨도, 단위 테스트에서 mock deps로 갈아 끼워도 동작이 같다.


팀 유지보수 메모:
서버 수명 동안 공유할 의존성만 보관한다. 연구 그래프는 v2 multi 모드에서 한 번 컴파일하고,
요청별 근거·작업 상태는 AgentState에만 둔다. 전역 공유 객체에 요청 결과를 저장하지 않는다.
"""
from dataclasses import dataclass, field
from typing import Any
from .config import AgentConfig, load_config


@dataclass
class SupervisorDeps:
    fast_llm: Any        # Wave 10a — 분류/계획 등 짧은 단계용 저비용 모델
    quality_llm: Any     # Wave 10a — 최종 답변 작성용 고품질 모델
    mcp: Any             # Wave 2  — MCP stdio 클라이언트 (Tool/Resource/Prompt)
    session_store: Any   # Wave 12 — SQLite 세션 저장소
    version: str         # Wave 14 — SUPERVISOR_VERSION (v1/v2)

    config: AgentConfig = field(default_factory=load_config)

    # 선택 기능은 비활성일 때 None이다. 새 요청의 결과를 이 공유 객체에 누적하면 대화 간 정보가 섞인다.
    incident_retriever: Any = None

    memory: Any = None

    research_graphs: dict = field(default_factory=dict)
