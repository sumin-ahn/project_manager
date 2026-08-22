---
name: pm-adr
description: "ADR 발행/개정 명령어化 — 번호 자동 채번 + frontmatter scaffold + 개정(amends/supersedes) 대상 ADR 에 lifecycle back-ref(status·amended_by/superseded_by) 발행 시점 자동 기록 + decisions/README.md 색인(Accepted 삽입·개정 대상 Accepted→Amended/Superseded 이동) + log decide entry 를 한 trigger 로 원자화한다. 흩어진 손 단계의 누락 클래스(back-ref 미기록·README 미이동·log 누락)를 명령에서 닫는다. backbone CLI .project_manager/tools/pm_adr.py thin wrapper. Triggers: 'ADR 발행', 'ADR 작성', '결정 박제', 'ADR 개정', 'ADR amend', 'ADR supersede', 'ADR 번호', 'decisions 색인', 'pm-adr'."
audience: pm-internal
---

# $pm-adr — ADR 발행/개정 명령어化

PM 에이전트가 사용자 지시로 ADR을 발행·개정할 때 `.project_manager/tools/pm_adr.py new`를 호출한다.
새 결정은 구조적·비가역적·다중 모듈 영향, 같은 질문의 재발, PM 내부 프로세스 결정
(`--scope internal-process`)일 때 기록한다. 기존 결정을 부분 수정하면 `--amends`, 완전 대체하면
`--supersedes`, 대상 불변 확장이면 `--refines`를 쓴다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

> **mission scope 게이트:** 미션·scope·핵심 안전 경계를 바꾸는 결정(`--scope mission`)은 **사용자
> 사전 동의 필수** — PM 자율 발행 금지. 이 스킬은 문서 산출을 원자화할 뿐, mission 결정의 승인 게이트를
> 대체하지 않는다.

상황별 산출 해석·후속 작업·동작 경계는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 실행

공유 루트(`.project_manager` 있는 PM 홈)에서 실행한다. 인스턴스가 `decisions/`, README, log를 소유하고
backbone이 self-location으로 해소한다.

```bash
# 새 ADR 발행 (기본 scope=internal-process·status=accepted)
python3 .project_manager/tools/pm_adr.py new \
  --title "결정 제목" --slug short-english-slug \
  --author "<user>/<pm-slot>" \
  [--scope internal-process|mission] [--status proposed|accepted] \
  [--amends ADR-NNNN ...] [--supersedes ADR-NNNN] [--refines ADR-NNNN] \
  [--related ADR-XXXX,ADR-YYYY] [--tags v1.3.0,adr] \
  [--dry-run]
```

- `--title`: 한글·`:`·따옴표·`#` 등 자유 제목이며 frontmatter-safe quoting된다. `--slug`:
  `NNNN-<slug>.md` 파일명용 필수값.
  **영문 소문자로 시작하고 소문자/숫자/하이픈/언더스코어만** 허용한다. path separator·`..`·공백·선행
  `.`·대문자·특수문자는 부작용 전에 거부한다.
- `--author`: provenance `<user>/<pm-slot>`. 생략하면 빈 값이고 `board.py lint`가 adr-author advisory를
  권고하므로 명시한다.
- `--amends`/`--supersedes`/`--refines`: 반복 지정(예: `--amends ADR-0061 --amends ADR-0062`) 또는
  콤마 묶음. amends/supersedes는 대상 frontmatter back-ref와 README 표 이동을 수행한다. refines는
  대상 불변이며 related 링크만 만든다. 모든 개정 대상은 신규 ADR의 `related`에도 dedup 편입된다.
- `--dry-run`: 쓰기 없이 예정 번호·신규 파일 미리보기·back-ref 대상·log entry를 stdout에 출력한다.
- 채번은 단일 PM이 순차 실행하는 전제이며 lock 없이 `decisions/`의 max+1을 쓴다.
