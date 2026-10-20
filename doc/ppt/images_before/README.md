# S0 개선 전 캡처

2026-10-20 재시도에서 Chrome 브라우저 연결에 성공하여 실제 화면 JPEG 3장을 확보했다.
모두 원본 UI, fake backend, v2, fixed monitoring, 임시 SQLite 조건이며 1512×682 크기다. 상세 입력과 조건은 manifest.json.

- s0_regression_route.jpg: 운영 질의가 chat으로 잘못 분류되어 인사만 반환되는 원본 실패.
- s0_incident_cpu.jpg: monitoring → cpu-spike runbook 추가 조사 → 초안 반려 → 최종 응답.
- s0_unknown_target.jpg: billing-service unknown과 불필요한 web-01 조회, db-down fallback.

스크린샷은 실제 브라우저 렌더링을 그대로 저장했다. 기존 UI 상단의 “fake 실모델” 문구는 원본 표시 오류이며, 실제 모델 결과로 해석하면 안 된다.
재현 서버 명령: scripts/s0_demo.py + 기존 web_demo_server. doc/README.md 참조.
이 폴더는 보고서 전용으로 Git에서 제외한다.
