"""Read-only synthetic log samples, restricted to registered systems.

팀 유지보수 메모:
현재 source는 서비스 데모용 합성 JSONL뿐이다. limit는 반환 줄 수이고 데이터 파일은 수정하지 않는다.
등록된 시스템이라도 파일이 data/logs 밖을 가리키면 거부한다. 타 시스템 기록·중복 ID·잘못된
타임스탬프를 유효한 근거로 넘기지 않는다.
"""
import json
from datetime import datetime

from .config import ROOT, load_known_systems

LOG_DIR = ROOT / 'data' / 'logs'


def read_recent_logs(system_name: str, limit: int = 20) -> dict:
    if system_name not in load_known_systems():
        raise ValueError('Unknown log system')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer between 1 and 100')
    path = LOG_DIR / f'{system_name}.jsonl'
    if path.resolve().parent != LOG_DIR.resolve():
        raise ValueError('Log path outside data/logs')
    entries, seen = [], set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if (not isinstance(item, dict) or item.get('system') != system_name
                or item.get('synthetic') is not True or item.get('level') not in ('DEBUG','INFO','WARN','ERROR')
                or not isinstance(item.get('message'), str) or not 1 <= len(item['message']) <= 1000
                or not isinstance(item.get('evidence_id'), str) or not item['evidence_id']
                or item['evidence_id'] in seen or not isinstance(item.get('timestamp'), str)):
            raise ValueError('Invalid synthetic log record')
        datetime.fromisoformat(item['timestamp'])
        seen.add(item['evidence_id'])
        entries.append(item)
    entries.sort(key=lambda item: datetime.fromisoformat(item['timestamp']))
    return {'system_name': system_name, 'synthetic': True, 'entries': entries[-limit:]}
