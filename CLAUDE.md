# CLAUDE.md — 프레임워크 자체 개발 (루트 도그푸딩)

> 이 repo 는 **Claude Project Framework 를 개발하면서 동시에 자기 자신으로 운영(도그푸딩)** 하는
> 모노레포다. 루트가 그 도그푸딩 공간. (채택자용 템플릿은 `templates/claude_code/` — 그쪽
> CLAUDE.md 는 출하 스캐폴드이고, 이 문서는 *프레임워크 개발자(=PM 세션)* 용이다.)

## 새 세션 부트스트랩

1. **이 문서** → 구조·핵심 규칙 파악.
2. **루트 도그푸딩 상태** — 프레임워크 자신의 진행:
   - [`.project_manager/wiki/architecture.md`](.project_manager/wiki/architecture.md) — **현재-아키텍처 단일 진실**(① live / ② target · ADR-0022). 현재-기준은 이것 (ADR 은 *왜*의 히스토리·현재 구속력 없음·충돌 시 architecture 가 기준).
   - [`.project_manager/wiki/roadmap.md`](.project_manager/wiki/roadmap.md) — 무엇을/왜 (역류·restructure·후속).
   - [`.project_manager/wiki/decisions/`](.project_manager/wiki/decisions/) — ADR (결정 *근거*·히스토리 · 0001~0023).
   - [`.project_manager/wiki/decisions-needed.md`](.project_manager/wiki/decisions-needed.md) — 열린 결정.
   - 설계 spike: [`.project_manager/wiki/raw/spikes/`](.project_manager/wiki/raw/spikes/).
   - 보드: `python3 .project_manager/tools/board.py list`.

## 구조 (ADR-0005)

```
.project_manager/      # 루트 도그푸딩 = canonical 엔진의 단일 진실
templates/
  claude_code/         # 출하 Claude Code 템플릿 (엔진 사본 + .claude 어댑터)
  opencode/            # (미구현·요구사항 대기)
tests/                 # 엔진 단위테스트 (pytest)
```

## 핵심 규칙 (반드시)

- **엔진은 루트에서 고친다.** `board.py`·`ticket_finish.py`·`pm_*.py`·`external_review.py` 등
  엔진 코드와 `wiki/` 방법론·`_template` 의 **단일 진실 = 루트 `.project_manager/`**.
  "고치는 곳 = 테스트하는 곳" — 루트에서 고치고 `tests/` 로 검증한다.
- **`templates/*/` 의 엔진을 직접 고치지 마라.** 거긴 루트에서 동기화된 사본이다 —
  엔진 변경 후 `pm_update --from <루트>` 로 각 타깃에 내보낸다(루트→템플릿).
- **타깃별로 다른 건 어댑터층뿐** — `claude_code/.claude/`(agents·skills)·`CLAUDE.md`.
  opencode 는 그 자리에 opencode 등가물. 엔진은 공유.
- **설계 산출(ADR·roadmap·spike)은 루트 `.project_manager/wiki/`** (decisions/·raw/spikes/·roadmap).
  `/spike-new` 로 설계 spike 박제(도그푸딩).
- **테스트 없이는 끝난 게 아니다.** `python3 -m pytest tests/ -q` 통과가 완료 조건.
- **작은 단위 분할 → 단계별 검증 · 최소 변경 · 명시적 풀네임.**

## 의존성 설치

```bash
python3 -m pip install -r requirements-dev.txt   # PyYAML(런타임) + pytest(테스트)
```

`requirements.txt` = 런타임(`PyYAML>=6` — board/ticket 도구가 frontmatter 파싱),
`requirements-dev.txt` = 그 위에 `pytest`. fresh clone 은 이걸 먼저 깔아야 import 단계를 넘는다.

## 자주 쓰는 명령

> **Windows 노트:** `python3`/`python` 은 Windows 에서 WindowsApps 가짜 shim(Git Bash 에선
> Permission denied)일 수 있다 — **런처 `py`(예: `py -3.12`)를 1순위로** 쓴다. Linux/macOS 는
> `python3` 그대로. 인코딩은 **엔진이 코드로 처리**(PM 7차 — 파일 IO `encoding="utf-8"`·콘솔
> stdout reconfigure)하므로 env prefix 없이 동작한다. cp949 콘솔·외부 파이프에서 드물게 깨지면
> **각 셸 문법으로** 붙인다 — PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. (bash 전용 아님.)

```bash
python3 -m pytest tests/ -q                               # 엔진 테스트 (Windows: py -3.12 -m pytest ...)
python3 .project_manager/tools/board.py list              # 도그푸딩 보드
python3 .project_manager/tools/board.py lint              # 의존성·thin·wikilink 검사
# 엔진 변경을 타깃에 내보내기 (루트 → 템플릿):
#   cd templates/claude_code && python3 .project_manager/tools/pm_update.py --from ../../ --dry-run
# 외부 프로젝트로 import (루트 파사드 — deep 경로·인터프리터 캡슐화·cwd 무관):
#   <manager>/pm-import.sh --new <dest> --harness opencode   (Windows: pm-import.cmd)
#   — pm_import.py 로 인자 verbatim forward. --from 은 자동으로 manager 루트로 해소(생략 가능).
```

## 후속 (roadmap 단일 진실)

opencode 어댑터(요구사항 수집 후·spike) · 다운스트림 인스턴스 백포트(별개·다운스트림 인스턴스 PM) · 엔진 테스트 확충.
자세한 건 [`.project_manager/wiki/roadmap.md`](.project_manager/wiki/roadmap.md).
