# $pm-adr 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

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
