# Placeholder 표

> 루트 [README](../README.md) 의 도입 경로에서 채워지는 토큰 참조. 파사드(`pm-import.sh`)를 쓰면
> `sed` 치환은 자동이다 — 이 표는 [수동 longhand](manual-import.md)나 직접 서술 항목 확인용이다.

`sed` 로 일괄 치환 가능한 토큰:

| 토큰 | 의미 | 예시 |
|---|---|---|
| `{{PROJECT_NAME}}` | 프로젝트 표시 이름 | `My Project` |
| `{{PROJECT_TAGLINE}}` | 한 줄 프로젝트 설명 | `한 줄 프로젝트 설명` |
| `{{PROJECT_ROOT}}` | 프로젝트 루트 절대경로 | `/home/user/workspace/myproject` |
| `{{PY}}` | Python 실행 prefix | `venv/bin/python` 또는 `python3` |
| `{{TEST_CMD}}` | 전체 회귀 명령 | `venv/bin/python -m pytest tests/ -q` |
| `{{DATE}}` | 초기화 날짜 (wiki frontmatter) | `2026-05-22` |

> ⚠️ `{{PROJECT_NAME}}` 은 **엔진 문서(`pm_role.md`)에선 치환하지 않는다** — `local.conf` 가 해소(`board.py init` 기록)하고 pm_update 동기화 대상이라 치환하면 되돌아간다. `{{PY}}`·`{{TEST_CMD}}` 는 엔진 문서·어댑터에서 폐기(T-0219) — 진입 문서 등 다른 파일에선 sed 로 채워도 됨.
> ⚠️ `{{DATE}}` 는 **스캐폴드 템플릿 2종(`wiki/pm_state.template.md`·`wiki/domain/_template.md`)에선 치환하지 않는다** — 그 둘의 날짜는 **소비 시점**(그 템플릿이 산출물을 만드는 지점: `board.py init` 의 `pm_state.md` · task pm_state 생성 · 사람이 스캐폴드를 복사해 domain 페이지를 만들 때)이 소유한다. 둘 다 manifest 등재라 설치가 날짜로 굳히면 다음 `pm_update` byte-copy 가 토큰-form 으로 되돌려 매 sync 진동한다. 나머지 파일(`status.md`·`architecture.md`·`log/current.md` 등 manifest 미등재 인스턴스 seed)의 `{{DATE}}` 는 설치일로 채우는 게 맞다. 선언 = `pm_import.CONSUMPTION_TIME_TOKENS`(파일 × 토큰 단위).
> opencode 타깃은 추가로 `{{OPENCODE_PRO_MODEL}}`(subagent 모델 ID)을 가지며 — sed 가 아니라 pm_import 의 결정적 `opencode models` 조회로 해소된다 ([`../templates/opencode/README.md`](../templates/opencode/README.md) §모델 선택).

직접 서술해야 하는(자유 형식) placeholder — 파일 안 `<!-- TODO -->` 주석으로 표시:

| 토큰 | 어디에 | 무엇을 채우나 |
|---|---|---|
| `{{PROJECT_CONSTRAINTS}}` | 진입 문서(`CLAUDE.md`/`AGENTS.md` §프로젝트 고유 제약) — 단일 거처 | 프로젝트의 **절대 위반 금지 제약**. 아키텍처 불변식·안전 경계 등. (예: "핵심 결정 로직 ↔ 비결정/LLM 계층 경계 분리", "외부 호출은 fail-soft") |
| `{{PROTECTED_PATHS}}` | **`pm_role.local.md`** §보호 영역 (어댑터엔 이 거처를 가리키는 정적 포인터만) | 서브에이전트·PM 이 **건드리면 안 되는 파일/디렉토리**. (예: 운영 한도·안전 상수 config, immutable `raw/` 스냅샷) |
| `{{USER_GATE_ITEMS}}` | **`pm_role.local.md`**(overlay) | PM 자율 결정 밖 — **사용자 사전 동의가 필요한 행위**. (예: 외부 비가역 행위, 유료 API 대량 호출) |

## 방법론 vs 누적 학습 분리 (ADR-0007)

- **`pm_playbook.md` = 순수 방법론** (프로젝트 무관: wave 절차·회귀 위생·운영 효율 규칙 등).
  엔진 문서이므로 `pm_update` 가 자동 갱신한다 — 직접 도메인 내용을 박지 않는다.
- **이 프로젝트의 누적 wave 학습·도메인 사례 → `pm_playbook.local.md`** (인스턴스 소유·manifest 밖·tracked).
  `pm_import` 로 도입하면 이 빈 스텁이 자동 생성된다(재-import 에서도 기존 내용 비파괴 보존).

> ⚠️ **규약 — `agents`/`skills`/`pm_playbook.local` 등 placeholder 를 *fill 하는 순간* 그 파일은
> `engine.manifest` 밖으로 둔다.** 안 그러면 다음 `pm_update` 가 무치환 raw overwrite 로 그 fill 을
> 덮어쓴다. 방법론 본문은 manifest 안(synced)에, 인스턴스가 채운 학습은 manifest 밖(인스턴스 소유)에.
