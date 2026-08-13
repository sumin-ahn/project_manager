# spike-new 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 섹션

- **0 요약 + 권고** — 합의된 결론. 대화가 끝난 뒤 채운다.
- **1 실측** — 직접 확인한 사실. 추정 vs 실측 구분 · `file:line` 근거. 도메인 경계·계약(모듈/레이어 경계 등)이 걸리면 명시. Claude 가 먼저 채워 보고한다.
- **2 옵션 비교 + 권고** — 옵션 N개 → 장점/단점/비용 → 권고. 사용자와 합의하며 채운다.
- **3 DRAFT 설계** — 스키마/DDL·인터페이스·의사코드, 전부 "DRAFT" 표시. 계약 변경 시 마이그레이션도.
- **4 ADR 후보 (DRAFT)** — 굳히려는 결정의 ADR 본문 초안을 **이 spike 파일 안에** 적고 scope·기각 대안을 포함한다.
  ⚠️ `decisions/` 에 ADR 파일을 만들지 않는다 · 번호 부여·실제 발행은 스킬 밖.
- **5 ticket 분할안 (DRAFT)** — 후속 구현을 ticket 단위로 **이 spike 파일 안에** 적고 각 항목에 `touches`·`depends_on`·DoD 가설을 둔다.
  ⚠️ `tickets/` 에 파일을 만들지 않는다 · `board.py` 도 건드리지 않는다 · 발행은 스킬 밖.
- **6 위험 / 열린 질문** — 스스로 못 정해 사용자 결정이 필요한 것. 비용·외부송신·키발급·안전경계가 걸리면 여기.
- **7** — 발행·후속 메모. 비워 둬도 된다.

## 마무리 제약

- 이 스킬은 **raw/spikes 파일 박제까지만** 한다. ADR/ticket 실제 발행(번호 부여, `decisions/`·`tickets/` 파일 생성, `board.py`)은 **PM(orchestrator)이 raw 초안을 참고해 진행**한다. 사용자 게이트 결정(비가역·비용·외부송신·안전)은 PM 이 사용자 비준을 거쳐 발행한다.
- 파일 저장 + 사용자 사인오프 시 이 spike *자신의* frontmatter `status:` 전환만 한다. **`status.md`·`log/`·`board.py` 는 건드리지 않는다.**
- git: 이번 spike 의 `raw/spikes/` 하위 산출만 `add`하며 다른 변경과 섞지 않는다.

마지막 응답:
```
spike 종료 — raw/spikes/<파일>
권고: <한 줄>
PM 수렴 권장안:
- ADR    : <발행 권장 N건 | 불필요> · 초안 · scope: PM-자율(internal·reversible) | 사용자-게이트(비가역·비용·외부송신·안전)
- ticket : <N개> · 분할 · touches·depends_on
- 사용자 게이트 : <없음 | 있음: 비용·외부송신·키발급·안전경계·비가역>
- 열린 질문 : <핵심 N개 — 사용자/PM 결정>
권장 시퀀스: <예: 사용자 scope 결정 → ADR 발행 → ticket 병렬 dev 위임>
```
PM 은 이 블록 + spike 본문만으로 수렴을 시작할 수 있어야 한다.

참고: `.project_manager/wiki/raw/spikes/_template.md`(섹션 골격), `.project_manager/wiki/raw/README.md`(`raw/` immutable 컨벤션 + spike draft→sealed 예외), `.claude/agents/architect.md`(무거운 설계 노동 위임; 설계 노동 ≠ 결정).
