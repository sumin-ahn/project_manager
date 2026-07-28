---
name: pm-adr
description: "ADR 발행/개정 명령어化 — 번호 자동 채번 + frontmatter scaffold + 개정(amends/supersedes) 대상 ADR 에 lifecycle back-ref(status·amended_by/superseded_by) 발행 시점 자동 부기 + decisions/README.md 색인(Accepted 삽입·개정 대상 Accepted→Amended/Superseded 이동) + log decide entry 를 한 trigger 로 원자화한다. 흩어진 손 단계의 누락 클래스(back-ref 미부기·README 미이동·log 누락)를 명령에서 닫는다. backbone CLI .project_manager/tools/pm_adr.py thin wrapper. Triggers: 'ADR 발행', 'ADR 작성', '결정 박제', 'ADR 개정', 'ADR amend', 'ADR supersede', 'ADR 번호', 'decisions 색인', 'pm-adr'."
audience: pm-internal
---

# /pm-adr — ADR 발행/개정 명령어化

> {{PROJECT_NAME}} 의 ADR(Architecture Decision Record) 발행/개정을 **한 trigger** 로 원자화하는
> pm-internal 스킬. 손으로 하던 다섯 단계 — 다음 번호 채번(`decisions/` 스캔) · frontmatter +
> 본문 골격 scaffold · 개정(`amends`/`supersedes`) 대상 ADR 에 lifecycle back-ref(`status`→amended/
> superseded · `amended_by`/`superseded_by`) **발행 시점 자동 부기** · `decisions/README.md` 색인
> (Accepted 표 신규 행 + 개정 대상 Accepted→Amended/Superseded 표 이동) · `log/current.md` decide
> entry — 를 한 명령으로 묶는다. 비즈니스 로직 0 — 엔진 CLI 호출 thin wrapper (명령어化
> 4요소). backbone = `.project_manager/tools/pm_adr.py`(`new`).

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 세션 중 자동 invoke. 사용자가 "이 결정 ADR 로 박아"·"ADR-00XX 를
개정하는 결정 발행해"·"이 방향으로 ADR 써" 라고 지시하면 PM 이 이 스킬을 부른다. 셋업(user-entrypoint)의
확장이 아니라 **운영중-관리** 스킬 — 청중이 다르다.

## 사용 시점 (trigger)

- **새 결정 박제** — 되돌리기 어렵거나 여러 모듈에 영향을 주는 구조 결정, 같은 질문이 두 번째로 나온
  경우, PM 자율 내부 프로세스 변경(`--scope internal-process`)을 ADR 로 남길 때
  (decisions/README.md §"ADR 를 쓰는 시점").
- **기존 ADR 개정** — 앞 결정을 부분 수정(`--amends ADR-NNNN`)하거나 완전 대체(`--supersedes ADR-NNNN`)
  하거나 확장(`--refines ADR-NNNN`·대상 불변)할 때. amends/supersedes 는 대상 ADR 의 status·back-ref 를
  발행 시점에 자동 부기해 lifecycle advisory 를 **사후 lint 가 아니라 발행에서 충족**한다
  ("redefine 후 back-ref 갱신 누락" 클래스 폐쇄).

> **mission scope 게이트:** 미션·scope·핵심 안전 경계를 바꾸는 결정(`--scope mission`)은 **사용자
> 사전 동의 필수** — PM 자율 발행 금지. 이 스킬은 문서 산출을 원자화할 뿐, mission 결정의 승인 게이트를
> 대체하지 않는다.

## 실행

공유 루트(`.project_manager` 있는 곳 = PM 홈)에서 실행한다 — ADR 실경로(`decisions/`)·README·log 는
인스턴스(PM 홈) 소유이고, backbone 이 자기 위치(self-location)에서 그 경로를 해소한다.

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

- `--title` — ADR 제목(한글·`:`·따옴표·`#` 등 자유·frontmatter 안전 quoting 됨). `--slug` — **파일명
  slug**, `NNNN-<slug>.md` 로 쓰인다(한글 제목에서 자동 유도 불가하므로 명시 필수). **영문 소문자로
  시작·소문자/숫자/하이픈/언더스코어만** 허용 — path separator·`..`·공백·선행 `.`·대문자·특수문자는
  거부된다(파일 주입/traversal 방지·부작용 이전 fail-loud).
- `--author` — provenance `<user>/<pm-slot>`(누가 결정했나·연속성 아님). 생략 시 빈 값이라
  `board.py lint` adr-author advisory 가 권고한다 — 명시 권장.
- `--amends`/`--supersedes`/`--refines` — 반복 지정 가능(`--amends ADR-0061 --amends ADR-0062`) 또는
  콤마 묶음. **amends/supersedes 대상은 frontmatter back-ref + README 표 이동**을 자동 수행, **refines
  대상은 불변**(related 링크만·"refines=추가는 status 불변"). 개정 대상은 신규 ADR 의
  `related` 에도 자동 편입된다(dedup).
- `--dry-run` — 파일 쓰기 없이 발행 예정 번호·신규 파일 미리보기·back-ref 대상·log entry 를 stdout 으로만
  출력. 실 발행 전 확인용.
- **채번은 원자화 대상이 아닌 단일-PM 작업** — ADR 발행은 PM 한 명이 순차로 하므로 동시 채번 경합은
  실질 발생하지 않는다(board ticket 발행의 flock 과 달리 lock 불요). `decisions/` 스캔 max+1 로 채번한다.

### 산출 (apply 시)

1. `decisions/NNNN-<slug>.md` — frontmatter(title/created/updated/author/type/status/scope/개정 동사/
   related/tags) + 본문 골격(Context/Decision/Consequences/References·placeholder).
2. 각 amends/supersedes 대상 ADR frontmatter — `status`→amended/superseded · `amended_by`/`superseded_by`
   에 신규 ADR id 부기(surgical 정규식 치환·본문/포맷 불변·멱등).
3. `decisions/README.md` — Accepted 표에 신규 행 + 개정 대상 행을 Accepted→Amended/Superseded 표로 이동
   (또는 이미 이동됐으면 back-ref cell 에 append). 표/섹션 구조 불일치 시 crash 아니라 warning + 해당 단계
   skip(fail-soft — frontmatter back-ref 는 항상 수행되므로 lint 정합은 유지).
4. `log/current.md` — `## [YYYY-MM-DD] decide | ADR-NNNN — <title>` decide entry skeleton append.

## 잔여 PM 손 (backbone 후)

backbone 은 **파생 가능한 기계 부분만** 채운다(pm-handoff/ticket_finish skeleton 철학) — 판단은 PM 손:

1. **ADR 본문 서술** — `decisions/NNNN-<slug>.md` 의 Context/Decision/Consequences/References placeholder
   (`<... PM 서술>`)를 채운다. 신규 결정의 요약 서문(`>`)도.
2. **README 개정 요약 cell** — 개정 대상 행의 "무엇이 바뀌었나"/"무엇이 대체됐나" 열 placeholder
   (`<개정 요약 — PM 서술>`)를 채운다(기계는 무엇이 바뀌었는지 모른다).
3. **log decide 본문** — decide entry 의 `<!-- PM: ... -->` placeholder 에 결정 요약·발단·게이트·메타를
   서술한다.
4. **git commit — pathspec 필수** — 발행/개정 산출 4종(신규 ADR·개정 대상 back-ref·README·
   log)을 **한 커밋**으로 묶되, 그 경로만 나열한다. 공유 PM 홈에서 bare `git commit` 은 다른 슬롯의
   미완성 wiki 편집을 함께 싣는다:

   ```bash
   git add .project_manager/wiki/decisions/NNNN-<slug>.md   # 신규 ADR 은 untracked — add 선행 필수
   git commit -m "ADR-NNNN — <title>" -- \
     .project_manager/wiki/decisions/NNNN-<slug>.md \
     .project_manager/wiki/decisions/MMMM-<개정 대상 slug>.md \
     .project_manager/wiki/decisions/PPPP-<또 다른 개정 대상 slug>.md \
     .project_manager/wiki/decisions/README.md \
     .project_manager/wiki/log/current.md
   ```

   pathspec commit 은 미추적 경로를 잡지 못하므로(`error: pathspec '…' did not match any file(s)
   known to git` · rc=1 로 커밋 전체가 죽는다) 신규 ADR 파일은 `git add` 를 선행한다.
   `--amends`/`--supersedes` 는 **반복 지정 가능**하므로 back-ref 가 부기된 **대상 ADR 마다 한 줄**씩
   적는다(위 `MMMM`·`PPPP`). 개정이 없으면 대상 ADR 행은 전부 생략한다.
   (재정의 후 기존 자산 갱신을 발행과 원자화). trailer `Co-Authored-By: Claude`.
5. **정합 확인** — `python3 .project_manager/tools/board.py lint` 로 adr-lifecycle advisory 가 clean 인지
   확인(발행이 back-ref 를 채웠으므로 clean 이어야 정상). warning 이 stderr 로 나왔으면(대상 파일 부재·
   README 구조 불일치) 해당 부분을 손으로 보정한다.
6. **모순 lint advisory 확인**(개정 시) — amends/supersedes 발행이면 backbone 이 stderr 로
   개정 대상을 참조하는 문서 목록을 표면화한다. 그 문서들이 새 결정과 **모순되는 잔여 서술**을 담고
   있는지 대조해(옛 결정 전제 문장이 뒤집힌 결정과 어긋나는지) 필요 시 함께 고친다(판정=사람·차단 아님·
   "redefine 후 자산 갱신 누락" 클래스). 프롬프트가 필요하면
   `python3 .project_manager/tools/contradiction_lint.py --new-adr ADR-NNNN --amends ADR-MMMM --show-prompt`.

## 결정 (모델)

- **4요소 규약**(skill+backbone+라이브테스트+청중) 적용 — 여러 손 단계(채번·frontmatter·
  back-ref·색인·log)를 명령으로 원자화해 **누락 클래스를 발행 명령에서 닫는다**.
- **lifecycle back-ref 발행 시점 자동 부기** — advisory lint 를 사후가 아니라 발행에서
  충족한다. `refines`(추가·대상 불변)는 back-ref 대상 아님(related 링크만).
- **스킬 단일 소비** — canonical `SKILL.md` 하나로 claude·opencode 양 하네스를 커버한다
  (opencode 1.17.19 가 `.claude/skills/*/SKILL.md` 네이티브 스캔). opencode command 수기 사본을 별도
  출하하지 않는다(쌍-출하 은퇴).
- **청중 = pm-internal**.

## 참고

- backbone: `.project_manager/tools/pm_adr.py`(`new` — `next_adr_number`/`build_adr_file`/
  `apply_lifecycle_backref`/`insert_accepted_row`/`move_or_append_backref_row`/`build_decide_log_entry`/
  `AdrIssuer.plan`/`AdrIssuer.apply`).
- (PM 관리 명령어化 4요소·청중 라벨) · (ADR lifecycle amends/refines/supersedes
  back-ref lint) · (스킬 라이브 하네스 테스트) · (opencode 스킬 단일 소비) ·
  decisions/README.md §"새 ADR 추가 절차"(이 명령이 자동화하는 손 절차의 단일 진실).
- **contradiction lint 트리거**(**배선됨**) — 이 발행/개정 명령이 모순 lint
  의 트리거다. `--amends`/`--supersedes`(개정)일 때 backbone 이 `contradiction_lint.py` 를 호출해,
  개정된 결정을 `[[wikilink]]` 참조하는 문서(back-ref 범위)의 **잔여 모순 후보**를 재정의 순간(인지 시점)
  에 stderr advisory 로 표면화한다. 탐지=LLM(기본 dry·미호출·프롬프트 표면화)·판정=사람(차단 아님·
  mechanize-dont-instruct-llm). 신규 plain 발행·`--refines` 는 참조 스코프가 없거나 대상 불변이라 발화 안 함.
- 라이브 하네스 테스트 = 실 LLM 이 스킬로 ADR 발행/개정 → 파일/색인/back-ref/log 실 상태 단언
  (on-demand `PM_ORCH_LIVE`). backbone 은 기계 단위테스트(`tests/test_pm_adr.py`)로,
  스킬(프롬프트)은 라이브로 검증한다.
