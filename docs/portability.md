# 이식성 등급 — 무엇이 그대로고 무엇을 고쳐야 하나

> 루트 [README](../README.md) 의 네 기둥이 새 프로젝트로 얼마나 그대로 옮겨지나. ✅ = 무수정 재사용,
> 🟡 = 언어/형식 결합이 있어 손봐야 하는 지점.

| 구성요소 | 이식성 | 비고 |
|---|---|---|
| `.project_manager/tools/board.py` | ✅ 그대로 | 순수 ticket 도구. 하드코딩 경로 없음. Python 3 + `pyyaml` 만 필요. |
| `pm_bootstrap.py` | ✅ 그대로 | PM 세션 시작 dump. timezone (KST default) 만 맞춰. |
| `pm_handoff.py` | 🟡 pm_state.md / pm_playbook.md 형식 결합 | sliding window·인계 프롬프트 추출이 해당 절 형식에 정규식으로 묶임 — 형식 바꾸면 정규식도 같이. |
| `pm_log.py` | ✅ 그대로 | log 의미단위 읽기 + 아카이브. entry 경계(`## [YYYY-MM-DD]`)만 의존. |
| `.project_manager/wiki/` 골격 | ✅ 구조 재사용 | README·sub-README·`_template.md`(domain 포함) 는 도메인 무관. status·domain/ 페이지만 새로 채움. |
| 어댑터층 (`.claude/`·`.opencode/`) | ✅ 거의 그대로 | researcher·architect·developer·code-reviewer + PM workflow. operational 토큰 전용(free-form 0) — 고유 제약은 root doc, 보호 영역은 `pm_role.local.md` §보호 영역에서 채움. 세부는 각 타깃 README. |
| `pm_role.md` | ✅ 도메인 무관 | PM 정적 핵심. `{{USER_GATE_ITEMS}}` (→`pm_role.local.md`)만 채움. |
| `pm_state.md` | ✅ 구조 재사용 | PM 동적 상태. 세션 window 는 `/pm-handoff` 가 자동 갱신. |
| `pm_playbook.md` | ✅ 도메인 무관 | PM 활동별 레퍼런스. 누적 학습은 `pm_playbook.local.md` 로 분리(ADR-0007). |
| 진입 문서 (`CLAUDE.md`·`AGENTS.md`) | 🟡 템플릿 | 부트스트랩 패턴 재사용, 프로젝트 한 줄·제약은 placeholder. |
| `ticket_finish.py` | 🟡 **Python+pytest 결합** | status.md 의 정확한 라인 형식에 정규식 앵커. **선택 도구** — 없어도 board.py 만으로 완결. Python 외 언어면 pytest 파싱 교체. |
| `additional_reviewer.py` | 🟡 **선택 · 외부 전송 · 기본 OFF** | 추가 리뷰어(additional reviewer) 어댑터(ADR-0004). opt-in 은 첫 1회 질문이고 `local.conf` 튜플 하나(`additional_reviewer.enabled=true` + `additional_reviewer.harness`/`.model`/`.reasoning`)로 기록된다. 켠 뒤 리뷰마다 비용 승인을 다시 받지 않는다. 없어도 내부 code-reviewer 로 완결. |
| `run_tests_hook.sh` | 🟡 러너는 설정으로, 발화는 `.py` 고정 | 러너 명령은 `local.conf` 의 `test.cmd` 로 지정한다(파일 자체는 엔진 소유·manifest 등재라 직접 고치면 다음 동기에 덮인다). 미지정이면 `pytest tests/` 폴백. 발화 게이트는 `.py` 편집 고정이라 다른 언어에선 훅이 발화하지 않는다. |
