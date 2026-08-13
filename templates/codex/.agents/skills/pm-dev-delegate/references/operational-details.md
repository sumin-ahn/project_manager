# $pm-dev-delegate 상황별 운영 상세

> 아래 절은 Codex 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

#### 게이트 격리 스냅샷 (병렬 wave · 내부 reviewer 전용)

병렬 dev가 공유 트리를 라이브 편집 중이면 PM은 reviewer 위임 전에 검토 대상의 **staged** 상태를 격리 worktree로 스냅샷해 dev 편집·reviewer git 조작(sensitivity `git checkout` 등)의 경합을 막는다. 스냅샷 생성·신선도 검증은 엔진 도구 `gate_snapshot.py`가 수행한다 — 검토 대상 경로의 staged 내용이 working tree와 다르면(미-stage dev 산출 = stale index) 생성을 fail-loud로 거부해 stale 검토(false-green)를 기계로 차단한다. `<scratch>`는 repo 밖 경로(`/tmp` 또는 repo 상위 `..`), 최종 경로는 `<scratch>/gate-<T>`. 출력 경로는 공유 worktree·같은 저장소 git 공용 디렉터리·다른 등록 worktree 안이면 거부된다(prunable 등록 재사용은 `git worktree prune` 처방).

1. **stage** — 검토 대상 dev 산출을 먼저 `git add <경로>` 한다. **다라운드 게이트는 라운드마다 다시** — 누락하면 도구가 불일치로 차단한다(그게 이 도구가 닫는 클래스다).
2. **생성** —

   ```bash
   python3 .project_manager/tools/gate_snapshot.py \
       --repo <공유 트리 절대경로> --output <scratch>/gate-<T> \
       --paths <검토 파일1> <검토 파일2> ...
   ```

   - **병렬 wave에서는 `--paths`를 파일 단위로** 지정한다 — 디렉터리를 주면 같은 디렉터리의 타 dev WIP(tracked-unstaged·untracked)가 불일치로 검출돼 차단된다(설계 동작). 진단 안내대로 검토 대상이면 `git add`, 타 dev WIP면 파일 단위로 좁힌다.
   - rc=0 = 스냅샷이 캡처 시점 index와 일치함을 도구가 검증(HEAD OID·index 엔트리·파일 집합 이중 bookend·submodule gitlink 원본 불변·eol 정규화 비교 포함). rc=1 = fail-loud — 자동 `git add` 해소는 하지 않는다(타 dev WIP 오염 금지).
3. **주입** — reviewer 프롬프트 작업 위치에 `<scratch>/gate-<T>` **절대경로**를 넣고 그 스냅샷에서만 읽고 검토시킨다. 그 안의 git 조작(checkout·stash 등)은 공유 트리에 닿지 않는다.
4. **제거** — 리뷰 후:

   ```bash
   git worktree remove --force <scratch>/gate-<T>
   ```

   `--force`는 오버레이가 미커밋이라 dirty인, 버려도 안전한 스냅샷 제거에 필요.

- 내부 reviewer만 대상. codex `external_review`는 **staged diff** 기반이라 이미 스냅샷-안정 → 격리 **대상 아님**(라이브 working tree 를 읽지 않는다). 단 staged 가 최신인지는 같은 원칙이다 — 검토 파일을 재-`git add` 한 뒤 실행한다.
- 솔로(비병렬)는 격리 선택.
- 격리와 프롬프트의 *공유 트리 git 조작 금지* 완화는 **병행**한다(**이중 방어** — 절차가 경합을 구조적으로 막고, 프롬프트가 사고성 git 조작을 막는다).

## 병렬 wave touches cross-check

dev N 동시 spawn 전:

- 모든 claimed ticket touches가 완전 disjoint(file 겹침 0)인지 확인. 공통 통합 파일의 함수 단위 추가는 완화 조건으로 허용.
- 같은 함수·같은 줄 동시 수정은 차단.
- baseline 회귀는 dev cycle 후 한 번에 측정(race 회피).

## reviewer 후 처리

- **PM 직접 fix**: 1줄·1패턴·dev가 작업하지 않는 영역.
- **dev 재작업**: 여러 줄 또는 dev가 같은 file 작업 중.
- **별도 ticket 후보 메모**: 본 ticket 범위 밖/후속 caller.
- **suggestion 보류**: 운영 영향 0·기능 충분.

reviewer 결과를 그대로 믿지 말고 should-fix 전 코드 흐름을 PM이 독립 점검한다. 부정확하면 변경하지 않고 `log/current.md`에 영구 기록. 다른 ticket 결함을 현재 ticket 영역으로 잘못 attribute할 수 있으므로 실제 영역 확인 후 분기한다.

## 운용

- board 조작은 PM, 서브에이전트는 구현/검토만.
- dev 보고에는 변경 파일, 신규 테스트, 지정 회귀(범위 명시), DoD별 evidence를 강제. 전체 회귀는 릴리즈 절차 1단계 1회(PM)다.
- background 우선. 다음 ticket이 결과에 의존하면 foreground.
- 프롬프트가 길어지면 ticket 본문을 보강한다.
- 같은 dev를 반복 resume하면 transcript 누적으로 컨텍스트 한도에 실패한다(14회 resume에서 "Prompt is too long"). 대략 5~6회↑면 새 에이전트에 자족 프롬프트로 재투입하고 현 코드 상태(신설 심볼·미커밋 변경)를 요약한다. 산출물은 워킹트리에 유지된다.

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기
- `.project_manager/tools/pm_delegate.py` — cross-harness 위임
- `.codex/agents/developer.toml`
- `.codex/agents/code-reviewer.toml`
