# Project Wiki

{{PROJECT_NAME}} 프로젝트의 모든 비-코드 산출물 (작업·결정·사양·상태)이
모이는 곳. 코드는 상위 디렉토리에 그대로 있고, 여기는 그 코드에 대한 **작업
운영·결정 근거·로드맵**.

`project_wiki/` 는 정적 지식베이스가 아니라 **진행 중인 엔지니어링 프로젝트의
운영 계층**이다 — ticket 주도로 자란다.

## 빠른 길찾기

| 알고 싶은 것 | 가야 할 곳 |
|---|---|
| **PM 세션이라면** (보드 운영 / 분할 / 위임) | [`pm_role.md`](pm_role.md) ← 인계 문서 |
| **지금 무슨 ticket 잡을까?** (구현 세션) | [`board.md`](board.md) (자동 갱신) + [`tickets/README.md`](tickets/README.md) (워크플로) |
| 지금 어디까지 됐는가? (모듈 상태·테스트 수) | [`status.md`](status.md) |
| 전체 아키텍처 + 모듈 의존성 | [`architecture.md`](architecture.md) |
| 사양 (포맷·한도·인터페이스) 단일 진실 | [`specs/`](specs/) |
| 왜 이렇게 결정했는가? | [`decisions/`](decisions/) (ADR) |
| 아직 결정 안 된 후보 아이디어 | [`ideas/`](ideas/) |
| 작업 일지 | [`log.md`](log.md) |

## 디렉토리 의미

> **이 절이 "디렉토리 의미" 의 단일 정의처다.** 다른 문서는 여기를 가리킨다 —
> 정의를 복제하지 않는다 (drift 방지).

```
project_wiki/
├── README.md         # ← 이 파일. 길찾기 + 디렉토리 의미 단일 정의처
├── status.md         # 현재 상태 단일 진실 — 모듈 매트릭스 + 테스트 수 + 외부 의존성
├── board.md          # ticket 현황 대시보드 (tools/board.py 자동 생성 — 수동 편집 금지)
├── pm_role.md        # PM 세션 인계 문서 (책임·결정 권한·핸드오프 절차)
├── architecture.md   # 구조 + 모듈 간 의존성 (상태 정보는 status.md 위임)
├── log.md            # 작업 일지 (append-only)
│
├── tickets/          # 작업 단위. open/ claimed/ blocked/ done/ 하위 + _template.md
├── specs/            # current 사양 단일 진실 (포맷·한도·인터페이스·API)
├── decisions/        # ADR — 결정과 그 근거 (NNNN-slug.md + README 색인)
└── ideas/            # pre-ADR 후보 (open/ promoted/ killed/ — tools/board.py idea 명령)
```

| 디렉토리 / 파일 | 의미 |
|---|---|
| `status.md` | 모듈 진행 상태·테스트 수의 **단일 진실 소스**. 새 모듈/테스트 추가 시 먼저 갱신 |
| `board.md` | ticket 발행 현황. `tools/board.py` 가 자동 생성 — 수동 편집 금지 |
| `architecture.md` | 구조와 모듈 의존성(imports/imported-by). 변동 정보(테스트 수·구현 상태)는 두지 않는다 — `status.md` 에 위임 |
| `tickets/` | 한 작업 = 한 ticket. `board.py` 가 `open/claimed/blocked/done/` 디렉토리로 관리 |
| `specs/` | 자주 변하는 사양(포맷·한도·endpoint)의 단일 진실. 설계 문서 본문에 두지 않고 추출 |
| `decisions/` | ADR — "왜 이렇게 결정했나". `NNNN-slug.md` + `README.md` 색인 표 |
| `ideas/` | pre-ADR 후보. 익히는 중인 아이디어. `board.py idea` 명령군이 관리 |

> **선택 — `raw/`:** immutable 시간 스냅샷 (설계 문서 버전, 외부 평가, 벤치마크
> 등) 을 보존하려면 `raw/` 디렉토리를 추가한다. 절대 수정하지 않고 새 사실은
> 새 파일로 추가하는 규칙. 프로젝트에 그런 스냅샷이 없으면 만들지 않아도 된다.

## 규칙

- **결정은 ADR 로 명시화** — 대화나 코드 주석에 묻히지 않게.
- **`status.md` 가 모듈 상태의 단일 진실** — 진행 상태·테스트 수는 여기를 본다.
- **`board.md` 는 자동 생성** — `tools/board.py` 명령으로만 바꾼다.
- 새 페이지마다 frontmatter (`title` / `created` / `updated` / `type`).
- 페이지 간 참조는 markdown link 를 기본으로 한다. `[[wikilink]]` 표기도
  쓸 수 있다 — 이름 기반이라 파일 이동에 깨지지 않는다 (ADR·idea 참조에 편리).

새 ticket / ADR / spec / idea 발행 절차는 각 디렉토리의 `README.md` 참조
([`tickets/README.md`](tickets/README.md), [`decisions/README.md`](decisions/README.md),
[`specs/README.md`](specs/README.md), [`ideas/README.md`](ideas/README.md)).
