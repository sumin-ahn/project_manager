# Project Wiki

{{PROJECT_NAME}} 프로젝트의 모든 비-코드 산출물 (작업·결정·사양·상태·계획)이
모이는 곳. 코드는 상위 디렉토리에 그대로 있고, 여기는 그 코드에 대한 **작업
운영·결정 근거·로드맵**.

`.project_manager/wiki/` 는 정적 지식베이스가 아니라 **진행 중인 엔지니어링
프로젝트의 운영 계층**이다 — ticket 주도로 자란다.

## 빠른 길찾기

| 알고 싶은 것 | 가야 할 곳 |
|---|---|
| **PM 세션이라면** (보드 운영 / 분할 / 위임) | [`pm_role.md`](pm_role.md) (정적 매뉴얼) + [`pm_state.md`](pm_state.md) (동적 상태) |
| **지금 무슨 ticket 잡을까?** (구현 세션) | `board.py list` (라이브) · `board.md` 는 파생 대시보드(git-untracked) + [`tickets/README.md`](tickets/README.md) (워크플로) |
| 현재 아키텍처는? (부트스트랩 #1) | [`architecture.md`](architecture.md) (현재-아키텍처 단일 진실 · ① live / ② target) |
| 지금 어디까지 됐는가? (활성 모듈 판정·비고) | [`status.md`](status.md) (활성) + [`status_done.md`](status_done.md) (완성 모듈) |
| 프로젝트 도메인 지식 (무엇·어떻게) | [`domain/`](domain/) (살아있는 concept·guide·research) |
| 사양 (포맷·한도·인터페이스) 단일 진실 | [`specs/`](specs/) |
| 왜 이렇게 결정했는가? (히스토리·근거) | [`decisions/`](decisions/) (ADR — 현재 구속력 없음) |
| 아직 결정 안 된 후보 아이디어 | [`ideas/`](ideas/) |
| 시간 스냅샷 (plan_vN·벤치마크·외부 평가) | [`raw/`](raw/) (immutable) |
| 작업 일지 | [`log/current.md`](log/current.md) (활성) + [`log/archive/`](log/archive/) (봉인) |

## 디렉토리 의미

> **이 절이 "디렉토리 의미" 의 단일 정의처다.** 다른 문서는 여기를 가리킨다 —
> 정의를 복제하지 않는다 (drift 방지).

```
.project_manager/wiki/
├── README.md         # ← 이 파일. 길찾기 + 디렉토리 의미 단일 정의처
├── architecture.md   # 현재-아키텍처 단일 진실 (① live=코드 실측 / ② target=확정·미구현 · 부트스트랩 #1)
├── status.md         # 진행 판정 — 활성 모듈 상태·비고 (judgment-only · 테스트 수는 박제 안 함)
├── status_done.md    # 완성·안정 모듈 상세 아카이브 (status.md 에서 분리 — 부트스트랩 비로드)
├── board.md          # ticket 현황 대시보드 (.project_manager/tools/board.py 자동 생성 — 수동 편집 금지)
├── pm_role.md        # PM 세션 인계 — 정적 핵심 (부트스트랩·결정 권한·안전 경계·skill 카탈로그)
├── pm_state.md       # PM 동적 상태 (세션 window / 진행 중 의사결정 / 남은 작업) — /pm-handoff 가 갱신
├── pm_playbook.md    # PM 활동별 레퍼런스 (위임·Wave·효율 규칙·메타 정책·인계 템플릿) — lazy, 부트스트랩 비로드
├── domain/           # 살아있는 프로젝트 지식 (concept·guide·research · covers: 코드 링크 · freshness)
├── log/              # 작업 일지. current.md(활성, append-only) + archive/(봉인, pm_log.py archive)
│
├── tickets/          # 작업 단위. open/ claimed/ blocked/ done/ 하위 + _template.md
├── specs/            # current 사양 단일 진실 (포맷·한도·인터페이스·API)
├── decisions/        # ADR — 결정의 근거·히스토리 (NNNN-slug.md + README 색인 · 현재 구속력 없음)
├── ideas/            # pre-ADR 후보 (open/ promoted/ killed/ — .project_manager/tools/board.py idea 명령)
└── raw/              # IMMUTABLE 시간 스냅샷 (plan_vN, 평가, 벤치마크) — 절대 수정 금지
```

| 디렉토리 / 파일 | 의미 |
|---|---|
| `architecture.md` | **현재-아키텍처 단일 진실** (부트스트랩 #1 · ADR-0022). 구조·모듈 의존성 + 구현 상태를 *담는다*: ① live = 코드 실측 / ② target = 확정·미구현. 옛 ADR 과 충돌하면 **이게 기준**. content-truth 는 architect 가 유지·PM 점검 |
| `status.md` | **활성 모듈 *판정*(상태·비고)의 단일 진실** (judgment-only · ADR-0023). **테스트 수는 박제하지 않는다** — `board.py regression`(pytest) 실측이 단일 진실, history 는 `log/`. 모듈 상태/비고는 architect 가 유지·PM 점검 |
| `status_done.md` | ✅ **완성·안정** 모듈 상세 아카이브. status.md 가 비대해지지 않게 분리 — 부트스트랩에 로드 안 됨. 모듈이 안정되면 행을 여기로 이동. `board.py lint` 가 ✅ 누적(`status-done-accum`)을 권고 |
| `board.md` | ticket 발행 현황. `.project_manager/tools/board.py` 가 자동 생성 — 수동 편집 금지 |
| `pm_role.md` / `pm_state.md` / `pm_playbook.md` | PM 세션 인계 3분할 — **정적 핵심**(role·매 부트스트랩 로드) / **동적 상태**(state·세션 window 등, `/pm-handoff` 자동 갱신) / **활동 레퍼런스**(playbook·위임·Wave·메타정책·인계 템플릿, 해당 활동 시 lazy Read). `pm_handoff.py` 가 인계 템플릿을 playbook 에서 추출 |
| `domain/` | **살아있는 프로젝트 지식** (ADR-0018) — *현재 무엇·어떻게*. `concept`(무엇·왜 이 모양) / `guide`(절차) / `research`(누적 조사). `covers:` 글롭으로 코드에 링크 — 코드가 바뀌면 freshness 점검. (대비: `decisions/` = *왜 결정했나*·동결) |
| `log/` | 작업 일지. `current.md` = 활성(append-only), `archive/NNNN-*.md` = 봉인. `pm_log.py archive` 로 잘라 보관. 읽기는 의미 단위(마지막 handoff entry) |
| `tickets/` | 한 작업 = 한 ticket. `board.py` 가 `open/claimed/blocked/done/` 디렉토리로 관리 |
| `specs/` | 자주 변하는 사양(포맷·한도·endpoint)의 단일 진실. 설계 문서 본문에 두지 않고 추출 |
| `decisions/` | ADR — "왜 이렇게 결정했나"의 **히스토리·근거** (`NNNN-slug.md` + `README.md` 색인). **현재 구속력 없음** — 현재-기준은 `architecture.md` (ADR-0022) |
| `ideas/` | pre-ADR 후보. 익히는 중인 아이디어. `board.py idea` 명령군이 관리 |
| `raw/` | immutable 시간 스냅샷 — plan_vN, 모델 평가, 벤치마크 등. **절대 수정 금지**, 새 사실은 새 파일 ([`raw/README.md`](raw/README.md) 참조) |

## 규칙

- **`raw/` 는 절대 수정하지 않는다.** 새 사실은 별도 파일로 추가.
- **결정은 ADR 로 명시화** — 대화나 코드 주석에 묻히지 않게 (단, ADR 은 *왜*의 히스토리·현재 구속력 없음 · 현재-기준은 `architecture.md`).
- **`architecture.md` 가 현재-아키텍처의 단일 진실** — 부트스트랩 #1·옛 ADR 과 충돌하면 이게 기준.
- **`status.md` 가 모듈 *판정*의 단일 진실** (judgment-only) — 진행 상태·비고는 여기, **테스트 수는 `board.py regression`(pytest) 실측**(박제 안 함).
- **`board.md` 는 자동 생성** — `.project_manager/tools/board.py` 명령으로만 바꾼다.
- 새 페이지마다 frontmatter (`title` / `created` / `updated` / `type`).
- 페이지 간 참조는 markdown link 를 기본으로 한다. `[[wikilink]]` 표기도
  쓸 수 있다 — 이름 기반이라 파일 이동에 깨지지 않는다 (ADR·idea 참조에 편리).

새 ticket / ADR / spec / idea 발행 절차는 각 디렉토리의 `README.md` 참조
([`tickets/README.md`](tickets/README.md), [`decisions/README.md`](decisions/README.md),
[`specs/README.md`](specs/README.md), [`ideas/README.md`](ideas/README.md)).
