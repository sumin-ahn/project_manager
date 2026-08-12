---
name: pm-adr
description: "ADR 발행/개정 명령어化 — 번호 자동 채번 + frontmatter scaffold + 개정(amends/supersedes) 대상 ADR 에 lifecycle back-ref(status·amended_by/superseded_by) 발행 시점 자동 부기 + decisions/README.md 색인(Accepted 삽입·개정 대상 Accepted→Amended/Superseded 이동) + log decide entry 를 한 trigger 로 원자화한다. 흩어진 손 단계의 누락 클래스(back-ref 미부기·README 미이동·log 누락)를 명령에서 닫는다. backbone CLI .project_manager/tools/pm_adr.py thin wrapper. Triggers: 'ADR 발행', 'ADR 작성', '결정 박제', 'ADR 개정', 'ADR amend', 'ADR supersede', 'ADR 번호', 'decisions 색인', 'pm-adr'."
audience: pm-internal
---

# /pm-adr — ADR 발행/개정 명령어化

PM 에이전트가 사용자 지시로 ADR을 발행·개정할 때 `.project_manager/tools/pm_adr.py new`를 호출한다.
새 결정은 구조적·비가역적·다중 모듈 영향, 같은 질문의 재발, PM 내부 프로세스 결정
(`--scope internal-process`)일 때 기록한다. 기존 결정을 부분 수정하면 `--amends`, 완전 대체하면
`--supersedes`, 대상 불변 확장이면 `--refines`를 쓴다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

> **mission scope 게이트:** 미션·scope·핵심 안전 경계를 바꾸는 결정(`--scope mission`)은 **사용자
> 사전 동의 필수** — PM 자율 발행 금지. 이 스킬은 문서 산출을 원자화할 뿐, mission 결정의 승인 게이트를
> 대체하지 않는다.

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

## apply 산출

1. `decisions/NNNN-<slug>.md`: frontmatter(title/created/updated/author/type/status/scope/개정 동사/
   related/tags)와 Context/Decision/Consequences/References placeholder 골격.
2. amends/supersedes 대상 frontmatter: `status`→amended/superseded, `amended_by`/`superseded_by`에 신규
   ADR id를 부기한다. surgical 정규식 치환으로 본문·포맷을 보존하며 멱등이다.
3. `decisions/README.md`: 신규 행을 Accepted에 넣고 대상을 Accepted→Amended/Superseded로 옮긴다.
   이미 이동됐으면 back-ref cell에 append한다. 표/섹션 구조 불일치는 warning 후 해당 단계만
   skip하는 fail-soft이며 frontmatter back-ref는 수행한다.
4. `log/current.md`: `## [YYYY-MM-DD] decide | ADR-NNNN — <title>` skeleton을 append한다.

## backbone 후 PM 작업

1. 신규 ADR의 Context/Decision/Consequences/References `<... PM 서술>` placeholder와 요약 서문(`>`)을
   채운다.
2. README 대상 행의 `<개정 요약 — PM 서술>` cell을 채운다.
3. log decide entry의 `<!-- PM: ... -->`에 결정 요약·발단·게이트·메타를 쓴다.
4. 신규 ADR·모든 개정 대상·README·log를 경로 명시한 **한 커밋**으로 묶는다. 공유 PM 홈에서 bare
   `git commit`은 다른 슬롯 편집을 함께 싣는다.

   ```bash
   git add .project_manager/wiki/decisions/NNNN-<slug>.md   # 신규 ADR 은 untracked — add 선행 필수
   git commit -m "ADR-NNNN — <title>" -- \
     .project_manager/wiki/decisions/NNNN-<slug>.md \
     .project_manager/wiki/decisions/MMMM-<개정 대상 slug>.md \
     .project_manager/wiki/decisions/PPPP-<또 다른 개정 대상 slug>.md \
     .project_manager/wiki/decisions/README.md \
     .project_manager/wiki/log/current.md
   ```

   pathspec commit은 미추적 파일을 잡지 못해 `error: pathspec '…' did not match any file(s) known to
   git`·rc=1로 전체 실패하므로 신규 ADR에 `git add`를 선행한다. `--amends`/`--supersedes` 대상마다
   한 줄씩 쓰고 개정이 없으면 대상 행을 생략한다. trailer는 `Co-Authored-By: Claude`.
5. `python3 .project_manager/tools/board.py lint`로 adr-lifecycle advisory가 clean인지 확인한다.
   stderr warning(대상 파일 부재·README 구조 불일치)이 있으면 손으로 보정한다.
6. amends/supersedes 시 stderr에 표면화된 대상 참조 문서가 새 결정과 모순되는지 사람이 대조하고 필요 시
   함께 수정한다. 차단 검사가 아니다. 프롬프트가 필요하면
   `python3 .project_manager/tools/contradiction_lint.py --new-adr ADR-NNNN --amends ADR-MMMM --show-prompt`.

## 동작 경계

- amends/supersedes는 발행 시 대상 status·back-ref와 README 이동을 자동화한다. refines는 대상 status를
  바꾸지 않고 related만 추가한다.
- contradiction lint는 이 명령에 배선된다. amends/supersedes 시 `contradiction_lint.py`가 개정 ADR의
  `[[wikilink]]` 참조 문서를 stderr advisory로 표시한다. 탐지는 LLM 기본 dry·미호출·프롬프트 표면화,
  판정은 사람이며 차단하지 않는다. 신규 plain 발행과 refines는 발화하지 않는다.
- 저작 canonical 은 `.claude/skills/*/SKILL.md` 하나다. 모델 진입(스킬 툴)과 사람 진입(슬래시
  팔레트)은 별개 표면이라 opencode 는 `.opencode/command/*.md` 사본도 함께 출하하며, 그 사본은
  canonical 에서 기계 생성한다(손 편집 금지).
- backbone 기계 동작은 `tests/test_pm_adr.py`, 실제 스킬 흐름은 on-demand `PM_ORCH_LIVE` 라이브
  하네스로 검증한다.

backbone: `.project_manager/tools/pm_adr.py` (`new`).
