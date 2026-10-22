"""all_total 통합 실습 — MCP Server (Wave 2 축).

Tool / Resource / Prompt 3대 capability를 모두 노출하는 실제 fastmcp 서버.
FastAPI 백엔드(server.py)가 lifespan에서 이 파일을 subprocess(stdio)로 1회 기동해
프로세스가 살아 있는 동안 재사용한다 (Wave 1 싱글턴 + Wave 2 표준화의 결합).

- Tool    get_monitoring_status : 호출마다 값이 달라지는 동적 조회 → Tool이 맞다
- Resource runbook://incident-response/{incident_type} : URI로 식별되는 정적 문서
- Prompt  incident_analysis_prompt : 여러 Tool을 조합하는 표준 진단 절차를 강제


팀 유지보수 메모:
monitoring은 JSON+무작위 jitter로 만든 데모 지표이고 logs는 명시적인 합성 예시다.
실제 모니터링/로그 시스템을 연결할 때 synthetic 출처 표시도 함께 갱신해야 한다.
새 읽기 Tool을 추가할 때 known-system 검증과 경로 제한을 우회하지 않는다.
"""
import json
import random
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from agent.log_source import read_recent_logs

mcp = FastMCP(name="capstone-all-total-ops")

DATA_DIR = Path(__file__).parent / "data"


class SystemStatus(BaseModel):
    synthetic: bool = Field(default=True, description="Demo monitoring.json with simulated jitter, not live telemetry")
    system_name: str = Field(description="시스템 이름")
    status: str = Field(description="상태: healthy / degraded / down")
    cpu_percent: float = Field(description="CPU 사용률 (%)")
    memory_percent: float = Field(description="메모리 사용률 (%)")
    last_checked: str = Field(description="마지막 체크 시각 (ISO 8601)")
    alerts: list[str] = Field(default_factory=list, description="활성 알림 목록")


@mcp.tool()
def get_monitoring_status(system_name: str) -> SystemStatus:
    """지정된 시스템의 현재 모니터링 상태를 조회합니다 (호출마다 값이 달라지는 동적 Tool)."""
    monitoring_data = json.loads((DATA_DIR / "monitoring.json").read_text())
    if system_name not in monitoring_data:
        return SystemStatus(
            system_name=system_name, status="unknown",
            cpu_percent=0.0, memory_percent=0.0,
            last_checked=datetime.now().isoformat(),
            alerts=[f"System '{system_name}' not found in monitoring"],
        )
    data = monitoring_data[system_name]
    jitter = random.uniform(-3.0, 8.0)
    return SystemStatus(
        system_name=system_name,
        status=data["status"],
        cpu_percent=round(data["cpu_base"] + jitter, 1),
        memory_percent=round(data["memory_base"] + jitter / 2, 1),
        last_checked=datetime.now().isoformat(timespec="microseconds"),
        alerts=data.get("alerts", []),
    )


@mcp.tool()
def get_recent_logs(system_name: str, limit: int = 20) -> dict:
    """Read synthetic log examples for a registered system; never reads arbitrary paths."""
    # 파일·레코드 검증은 log_source 한 곳에서 수행한다. Tool별 별도 읽기 구현을 두면 정책이 달라진다.
    return read_recent_logs(system_name, limit)


@mcp.resource("runbook://incident-response/{incident_type}")
def get_incident_runbook(incident_type: str) -> str:
    """특정 incident 유형의 runbook(정적 문서)을 반환합니다."""
    runbook_path = DATA_DIR / "runbooks" / f"{incident_type}.md"
    # URI 인자도 외부 입력이다. 경로 이동 및 심볼릭 링크로 런북 디렉터리 밖을 읽지 못하게 한다.
    if runbook_path.resolve().parent != (DATA_DIR / "runbooks").resolve():
        raise ValueError("Invalid runbook path")
    if not runbook_path.exists():
        return f"# Runbook Not Found\n\nNo runbook for incident type: {incident_type}"
    return runbook_path.read_text()


@mcp.prompt()
def incident_analysis_prompt(system_name: str, symptom: str) -> str:
    """장애 진단 표준 절차 프롬프트 — planning 노드가 계획을 세울 때 이 절차를 따른다."""
    return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
