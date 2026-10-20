# all_total_vue — 운영 인시던트 대응 에이전트 (Vue 프론트엔드 버전)

`all_total`과 백엔드(server.py, agent/, mcp_server.py 등)는 완전히 동일하고, 브라우저
채팅 화면(`web_chat.html`)만 바닐라 JS 대신 **Vue 3**(CDN 글로벌 빌드, 별도 빌드 단계
없음)로 재작성한 버전이다. 나머지 설명은 원본 `all_total`과 같다.

Wave 1~15가 각각 증명한 패턴(싱글턴·MCP·SSE·라우팅·게이트·재계획·검증·재시도·병렬·비용분리·
세션·주입·버전 토글·가드)을 전부 조립한 **실제 동작하는 에이전트 서비스**다. LLM 자리는
`fake`(결정론)/`ollama`(로컬 실모델)/`openai`(실제 API) 세 백엔드 중 아무거나 꽂을 수 있고,
요청 한 건이 처리되는 전 과정은 LangSmith에 계층 트리로 기록·평가·피드백된다.

## 주요 실행 방법

**맨 처음 한 번만**: 이 폴더를 처음 받았다면(학생 배포 등) `.venv`부터 만든다.

```bash
bash sh/setup_venv.sh
```

| 방법 | 명령 | 비용 | 용도 |
|---|---|---|---|
| Python 가상환경 준비 | `bash sh/setup_venv.sh` | 없음 | `.venv` 생성 + `requirements.txt` 설치(최초 1회, 이미 있으면 설치만 재확인) |
| 로컬 Ollama 환경 준비 | `bash sh/setup_ollama.sh` | 없음(로컬 추론) | 모델(`qwen3:0.6b`/`8b`)이 이미 있으면 재사용, 없으면 새로 pull — `OLLAMA_FORCE_REFRESH=1`을 주면 지우고 재구성 |
| 에이전트만 기동 (로컬) | `bash sh/run_agent_ollma.sh` | 없음 | 서버만 띄워두고 직접 curl/웹으로 테스트 |
| 에이전트만 기동 (OpenAI) | `bash sh/run_agent_openai.sh` | **과금** | 위와 동일하되 실제 API 사용 (y/N 확인) |
| 브라우저 채팅 화면 | `bash sh/run_frontend.sh` (서버 기동 후) | 백엔드에 따름 | `http://localhost:8600`에서 실제 채팅 UI로 사용 |

`run_agent_*.sh`/`run_frontend.sh`는 `.venv`가 이미 있다는 가정 하에 PATH에
꽂아 쓰기만 한다(`.venv`가 없으면 시스템 `python3`로 조용히 떨어져 패키지
누락 오류가 날 수 있다) — `setup_venv.sh`를 먼저 돌려야 하는 이유다.

모든 스크립트는 `.env`가 없으면 `cp .env.example .env` 후 본인 API 키를 채우라는 안내를
출력하고 종료하며(외부 경로에 의존하지 않음 — 이 폴더만 떼어가도 그대로 동작),
LangSmith 프로젝트를 백엔드별로 분리해 기록한다(`capstone-all-total[-ollama|-openai]`).

> 더 쉬운 설명과 실제 캡처 화면이 필요하면 [manual_ollma.md](manual_ollma.md)(로컬
> 실모델) · [manual_openai.md](manual_openai.md)(OpenAI API)를 참고.

## 아키텍처 구성도

```mermaid
flowchart TB
    User["사용자 / curl"] -->|"POST /chat, GET /chat/stream"| API
    Browser["브라우저 (web_chat.html)"] -->|"same-origin"| Proxy["web_demo_server.py<br/>(프록시, :8600)"]
    Proxy -->|"POST /chat, GET /chat/stream"| API

    subgraph Server["server.py — FastAPI (:8100)"]
        API["/health · /version<br/>/chat · /chat/stream"]
        Lifespan["lifespan: 서버 수명 동안<br/>Agent 번들 1회 생성"]
    end

    API --> Graph

    subgraph Graph["agent/graph.py — LangGraph StateGraph (v2)"]
        Route["route"] -->|chat| ChatAnswer["chat_answer"]
        Route -->|incident| Plan["plan"]
        Plan --> Gate["gate"]
        Gate --> Research["research"]
        Research --> ReplanCheck["replan_check"]
        ReplanCheck -->|부족| Research
        ReplanCheck -->|충분| Analysis["analysis<br/>(task 없으면 내부 스킵)"]
        Analysis --> Write["write"]
        Write --> Validate["validate"]
        Validate -->|불합격| Write
        Validate -->|합격| Finalize["finalize"]
    end

    API -. "SUPERVISOR_VERSION=v1" .-> V1["agent/supervisor_v1.py<br/>(legacy 선형 파이프라인)"]

    Plan -. "find_prompt로 서버 탐색 후<br/>MCP Prompt 요청" .-> MCPClient
    Research -->|"find_resource_template로 서버 탐색 후<br/>stdio subprocess 요청"| MCPClient

    subgraph MCPClient["agent/mcp_client.py — 다중 MCP 세션"]
        subgraph OpsServer["mcp_server.py (server=&quot;ops&quot;, 실사용)"]
            Tool["Tool: get_monitoring_status"]
            Resource["Resource: runbook://incident-response/{type}"]
            Prompt["Prompt: incident_analysis_prompt"]
        end
        subgraph DocsServer["mcp_server_docs.py (server=&quot;docs&quot;, decoy)"]
            DocsResource["Resource: docs://api-reference/{section}<br/>(agent는 사용 안 함)"]
        end
    end

    Lifespan --> Deps["SupervisorDeps"]
    Deps --> LLM["fast_llm / quality_llm<br/>(fake · ollama · openai 중 택1)"]
    Deps --> Session["SqliteSessionStore<br/>(session.db)"]

    Graph --> Trace["LangSmith<br/>(계층 트리 trace · Dataset 평가 · Feedback)"]
```

- **싱글턴**: `server.py`의 `lifespan`에서 `SupervisorDeps`(LLM·세션·MCP 연결)를 정확히
  1회 생성해 `app.state`로 공유한다 — 요청마다 재생성하지 않는다.
- **백엔드 교체**: `LLM_BACKEND` 환경변수 하나로 `fake_llm.py`/`ollama_llm.py`/
  `openai_llm.py`가 바뀌어 꽂히고, 그래프·MCP·세션 코드는 전혀 바뀌지 않는다.
- **버전 토글**: `SUPERVISOR_VERSION=v1`이면 라우팅·게이트·재계획·검증이 없는 legacy
  선형 파이프라인(`supervisor_v1.py`)으로, 재배포 없이 환경변수만으로 전환된다.
- **MCP는 별도 프로세스**: `mcp_server.py`(ops)가 stdio subprocess로 떠서 서버 수명 동안
  재사용되며, 모니터링 조회(Tool)·대응 매뉴얼(Resource)·진단 절차(Prompt) 세 가지
  capability를 모두 실사용한다.
- **MCP 멀티 서버 탐색**: `mcp_client.py`는 `ops`(mcp_server.py, 실사용)·`docs`
  (mcp_server_docs.py, 내부 API 문서 Resource만 제공하는 decoy) 두 서버에 동시
  연결한다. `plan`/`research` 노드는 어느 서버가 필요한 prompt/resource를 들고
  있는지 코드에 하드코딩하지 않고, `find_prompt()`/`find_resource_template()`로
  연결된 서버들을 먼저 조회한 뒤에야 그 서버로 요청을 보낸다.
- **분석 단계 분리**: `replan_check`가 조사 결과가 충분하다고 판단하면 항상
  `analysis` 노드로 넘어가지만, gate가 살려둔 `agent=="analysis"` task가 없으면
  `analysis_node`가 내부적으로 스킵하고 바로 `write`로 넘어간다 — `needs_analysis`가
  실제로 유효한 경우에만 수집된 모니터링 결과들을 비교 분석한다.

## 주요 유스케이스별 고수준 시퀀스 다이어그램

### 1. 가벼운 대화

```mermaid
sequenceDiagram
    participant U as 사용자
    participant S as server.py
    participant G as graph (route)

    U->>S: POST /chat ("안녕하세요, 제 이름은 ...")
    S->>G: route 노드 실행
    G-->>S: route=chat (무거운 파이프라인 건너뜀)
    S-->>U: 바로 답변, task_count=0
```

### 2. 장애 진단 요청

```mermaid
sequenceDiagram
    participant U as 사용자
    participant G as graph
    participant M as MCP 서버군 (ops+docs)

    U->>G: POST /chat ("OO 서비스가 이상해요")
    G->>M: plan — find_prompt로 서버 탐색 후 절차 프롬프트 요청, 할 일 목록 수립
    G->>G: gate — 불필요한 작업 제거
    G->>M: research — find_resource_template로 서버 탐색 후 모니터링 조회(Tool) + 매뉴얼 조회(Resource)
    M-->>G: 실측 수치 + runbook 본문
    G->>G: replan_check — 부족하면 조사 추가
    G->>G: analysis — needs_analysis일 때만 수집 결과 비교 분석
    G->>G: write → validate (형식 미달 시 반려·재작성)
    G-->>U: 근거 기반 진단 결과
```

### 3. 실시간 진행 상황 스트리밍

```mermaid
sequenceDiagram
    participant U as 사용자
    participant S as server.py (/chat/stream)
    participant G as graph

    U->>S: GET /chat/stream (SSE)
    loop 노드가 끝날 때마다
        G-->>S: 노드 이벤트 (RouteDecided, PlanCreated, ...)
        S-->>U: data: {event}
    end
    G-->>S: FinalAnswer
    S-->>U: data: {reply} → data: [DONE]
```

### 4. 서버 재시작 후 대화 기억

```mermaid
sequenceDiagram
    participant U as 사용자
    participant S1 as server.py (프로세스 A)
    participant DB as session.db (SQLite)
    participant S2 as server.py (프로세스 B, 재시작 후)

    U->>S1: "제 이름은 OOO입니다"
    S1->>DB: 대화 이력 저장
    S1--xS1: 프로세스 종료
    S2->>S2: lifespan — 완전히 새로 기동
    U->>S2: "제 이름이 뭐였죠?"
    S2->>DB: 이력 조회
    DB-->>S2: 이전 대화 그대로
    S2-->>U: 이름을 정확히 기억해 답변
```

### 5. 웹 채팅 화면 사용

```mermaid
sequenceDiagram
    participant B as 브라우저 (web_chat.html)
    participant P as web_demo_server.py (:8600)
    participant S as server.py (:8100)

    B->>P: GET / (정적 페이지)
    B->>P: GET /chat/stream?message=... (same-origin, CORS 불필요)
    P->>S: GET /chat/stream (그대로 중계)
    S-->>P: SSE 이벤트
    P-->>B: SSE 이벤트 (그대로 relay)
    B->>B: 말풍선 + 진행 상태(⏳) 렌더링
```

## 소스 디렉토리 구조

```
all_total_vue/
├── server.py              # FastAPI 진입점 — lifespan 싱글턴, /health·/version·/chat·/chat/stream
├── mcp_server.py           # fastmcp 서버("ops") — Tool/Resource/Prompt (실사용, stdio subprocess)
├── mcp_server_docs.py       # fastmcp 서버("docs") — API 문서 Resource만 제공하는 decoy (멀티 서버 탐색 데모용)
├── web_chat.html            # 브라우저 채팅 화면 (정적 1파일)
├── web_demo_server.py       # web_chat.html 서빙 + /chat·/chat/stream·/health 프록시(:8600)
├── requirements.txt
├── .env.example
│
├── agent/
│   ├── deps.py              # SupervisorDeps 의존성 주입
│   ├── graph.py              # v2 LangGraph StateGraph (route/plan/gate/research/replan_check/analysis/write/validate/finalize)
│   ├── supervisor_v1.py      # legacy 선형 파이프라인 (버전 토글 대상)
│   ├── fake_llm.py           # LLM_BACKEND=fake — 결정론적 fast/quality 모델 (route/plan/analyze/chat/write)
│   ├── ollama_llm.py         # LLM_BACKEND=ollama — 로컬 실모델 백엔드
│   ├── openai_llm.py         # LLM_BACKEND=openai — 실제 OpenAI API 백엔드
│   ├── mcp_client.py          # MCP 멀티 서버(ops+docs) stdio 클라이언트 — find_resource_template/find_prompt로 서버 탐색 + 의도적 flaky 1회(재시도 데모)
│   ├── retry.py              # Transient 전용 backoff 재시도
│   └── session_store.py       # SQLite 세션 저장소
│
├── data/
│   ├── monitoring.json        # MCP Tool이 조회하는 모니터링 데이터
│   └── runbooks/               # MCP Resource가 조회하는 대응 매뉴얼 6종
│       ├── db-down.md
│       ├── cpu-spike.md
│       ├── disk-full.md
│       ├── high-latency.md
│       ├── memory-leak.md
│       └── network-timeout.md
│
├── sh/                        # 실행 스크립트 모음 (학생 배포용 — 내부 회귀 테스트 스크립트는 제외)
│   ├── setup_venv.sh            # .venv 생성 + requirements.txt 설치 (맨 처음 1회)
│   ├── setup_ollama.sh          # 로컬 Ollama 모델 준비 — 있으면 재사용, 없으면 pull (리셋은 OLLAMA_FORCE_REFRESH=1)
│   ├── run_agent_ollma.sh       # 에이전트만 기동 — 로컬 실모델
│   ├── run_agent_openai.sh      # 에이전트만 기동 — OpenAI API (과금)
│   ├── run_frontend.sh          # 브라우저 채팅 화면(web_demo_server.py) 기동
│   └── make_student_zip.sh      # .env 제외 + .env.example 포함한 학생 배포용 zip 생성
│
├── doc/                       # 문서
│   ├── README.md                # 이 문서
│   ├── manual_ollma.md          # 사용자 가이드 — 로컬 실모델 기준
│   ├── manual_openai.md         # 사용자 가이드 — OpenAI API 기준
│   ├── regression_test_queries.md  # 시스템명 하드코딩 수정 검증용 회귀 테스트 쿼리 목록
│   ├── image_usecase/           # manual_ollma.md 캡처 이미지
│   └── image_usecase_openai/    # manual_openai.md 캡처 이미지
```


## 설정과 대상 식별

`config/agent.json`에서 LLM 시스템 프롬프트, legacy 기본 대상과 확인 질문 정책을 관리한다.
`AGENT_CONFIG_PATH`로 다른 JSON을 지정할 수 있다. 설정 변경은 서버 재시작 후 적용한다.
시스템 목록은 기존 `data/monitoring.json`을 사용한다.

- `TARGET_RESOLUTION_MODE=legacy` (기본): 기존 대상 폴백·모델 프롬프트 유지.
- `TARGET_RESOLUTION_MODE=strict`: 미식별·미등록 운영 대상은 확인 질문, 명시한 여러 시스템만 조회. v2 전용.
- `/health`와 HTTP metadata에서 mode/config SHA를 확인한다. strict SSE는 `TargetResolution` 이벤트를 추가하고 `FinalAnswer.prompt`는 null로 반환한다.

기존 서버를 종료한 후 아래처럼 직접 재시작하면 세션 DB를 지우는 실행 스크립트의 기존 동작을 피할 수 있다.

```bash
TARGET_RESOLUTION_MODE=strict LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 .venv/bin/python server.py
```

롤백은 같은 명령에서 `TARGET_RESOLUTION_MODE=legacy`로 변경 후 재시작한다.
기존 `web_demo_server.py` 프론트는 그대로 사용한다.

서비스 단위 테스트: `pytest`를 별도 설치한 뒤 `.venv/bin/python -m pytest -q`.
현재 알려진 한계: 일부 fake 질의의 라우팅 문제가 남아 있다.
Ollama는 동기·스트리밍 요청에 `think=false`를 명시한다. Qwen3가 분석 중 추론을 계속 생성하면서 최종 답변을 반환하지 않던 문제를 방지한다. 코드 변경 적용에는 서버 재시작이 필요하다.

최종 발표 보고서: [G087_신형섭.html](ppt/G087_신형섭.html)


## 의미 기반 런북 검색

기본값 `INCIDENT_CLASSIFIER=keyword`는 기존 규칙을 유지한다. v2에서만 `rag`를 사용할 수 있다.
프로젝트 루트에서 의존성을 설치하고 인덱스를 먼저 구축한다.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m agent.rag.indexer
```

첫 구축은 고정 버전의 [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)을 다운로드한다.
CPU에서 정규화한 384차원 임베딩을 생성하며 `query: `/`passage: ` prefix, 최대 512토큰,
Chroma cosine 거리의 `1-distance`를 유사도로 사용한다. 점수는 확률이 아니다.
짧은 런북 6개를 문서별로 인덱싱하며, 검색 후보를 선택한 뒤 본문은 기존 MCP Resource로 읽는다.

`.env`에 `INCIDENT_CLASSIFIER=rag`를 설정한 뒤 기존 서버를 종료하고 직접 재시작한다.

```bash
TARGET_RESOLUTION_MODE=strict LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 INCIDENT_CLASSIFIER=rag .venv/bin/python server.py
```

`/health`의 `incident_classifier`와 `rag_index_fingerprint`로 적용 여부를 확인한다.
HTTP metadata와 SSE `RunbookRetrieval` 이벤트에 후보·출처·점수·모델 버전·검색 시간이 포함된다.
처음 계획에 이미 런북이 있으면 추가 검색은 실행하지 않는다.

`config/agent.json`의 `rag`에서 모델 commit, 차원, Top-K, threshold와 margin을 관리한다.
기본 Top-K는 3, threshold는 0.8, 1·2위 최소 차이는 0.01이다.
점수가 낮거나 후보가 비슷하면 런북을 추측하지 않고 추가 증상을 요청한다.
작은 합성 사례로 정한 기준이므로 운영 데이터에 맞춘 별도 평가가 필요하다.

인덱스는 Git 제외 경로 `data/chroma/runbooks/`에 저장하며 `RAG_INDEX_PATH`로 변경할 수 있다.
원본 런북·모델·차원·prefix가 바뀌거나 인덱스가 없으면 위 구축 명령을 다시 실행한다.
서버는 모델을 자동 다운로드하지 않으므로 구축 때 사용한 모델 캐시가 필요하다.
재구축은 새 corpus fingerprint의 collection을 만든 뒤 manifest를 교체하며, 이전 collection은 보존한다.
자주 문서를 변경하는 환경에서는 사용 중이지 않은 세대의 정리 정책이 추가로 필요하다.

롤백은 `INCIDENT_CLASSIFIER=keyword`로 변경 후 재시작한다. 원본 런북과 세션 DB는 유지된다.
