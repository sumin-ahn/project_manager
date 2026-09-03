# Changelog

이 프로젝트의 주요 변경 사항을 이 파일에 기록한다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 를 따르고,
버저닝은 [Semantic Versioning](https://semver.org/spec/v2.0.0.html) 을 따른다.

## [Unreleased]

## [1.7.12] - 2026-09-03

### Removed

- **BREAKING — 추가 리뷰어를 켜고 끄는 축을 없앴다.** `additional_reviewer.enabled` 키, `board init`·`pm-update` 의 opt-in 질문, 비활성 1회 강제 플래그(`--force`), 그 값을 읽던 모든 분기를 지웠다. 추가 리뷰어는 developer·architect 와 같이 **부르면 도는 역할**이다. 채택자 `local.conf` 에 남은 그 줄은 소비 지점에서 멈추며(대체 키 없음) 지우면 된다 — `true`/`false` 어느 값이든 실행 조건은 같다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- 추가 리뷰어에게만 붙던 가시 범위 격리 컨테이너(저장소 거울·임시 홈·리뷰어 전용 env allowlist)를 삭제했다. 리뷰어는 위임과 같은 조건으로 돈다 — cwd 는 검토 대상 저장소, env 는 위임 채널이 소유한 같은 seam 이 조립한다. 리뷰어 전용 노브 `additional_reviewer.env_keep_extra`·`.home_artifacts_extra` 도 대체 키 없이 제거됐다.
- 보낼 내용을 미리 재던 자리를 전부 지웠다 — 프롬프트 시크릿 스캔과 승인 플래그(`--secret-scan-ack`), 리뷰 diff 의 시크릿 denylist 제외와 노브 `additional_reviewer.denylist_extra`, 기계 사본 경로 제외, 프롬프트 파일 경계 검사, 그리고 그 결과로 실행을 막던 분기와 보고 항목이 함께 사라졌다. 폴백 억제 규칙(`ack` 통과 실행)도 같이 없어졌다.
- codex 네트워크 attestation 축(`CODEX_SANDBOX_NETWORK_DISABLED` 게이트·`--codex-egress-escalated` 플래그·dry-run 승격 표기·카드 산문)을 지웠다. codex sandbox 의 명령 승인은 codex 자신이 소유하며 엔진이 그 축을 대신 판정하지 않는다.
- **BREAKING — `board.py cluster new` 가 코드 트리를 추측하던 두 번째 규칙을 지웠다.** 인자가 없으면 활성 슬롯, 그것도 없으면 PM 홈으로 조용히 접혀 통합 브랜치가 엉뚱한 저장소·기점에 생기던 자리다. 이제 `--repo/--slot`(또는 `--task`) 명시가 없으면 첫 쓰기 앞에서 거부한다. 코드 트리 추측을 전제하던 `cluster show` 의 브랜치 실재 표기와 lint `cluster-branch-missing` 축도 함께 사라졌다.
- **BREAKING — 엔진이 자기 PM 홈을 부모 폴더를 훑어 추측하던 경로 7곳을 지웠다.** 못 찾으면 경고 뒤 자기 자리로 강등해 진행하던 분기도 같이 없어졌다. 소유 PM 홈은 anchor 의 `.git` 포인터가 가리키는 공용 저장소에서 유도하며(`pm_log.owning_pm_home`), 못 찾으면 오류로 멈춘다. opencode 훅 코어가 세션 폴더의 부모를 최대 12단 훑어 엔진 루트를 추측하던 `findEngineRoot` 도 같은 클래스라 지웠다 — 엔진 루트는 훅 파일 자기 위치에서 받는다.
- 위임 raw·라운드 장부의 저장 위치를 못 정하면 OS 임시 폴더로 떨어뜨리던 폴백을 지웠다. `pm_relay.raw_storage_paths` 는 해소 실패를 오류로 세우고, 폴백 전용 인자 `temp_dir=` 는 파라미터째 사라졌다.
- **BREAKING — `board.py section-add` 가 고정 라운드 예산 밖에서 새 순번을 만들던 경로를 닫았다.** 묶음 장부가 있는 티켓에는 거부하고 `pm_delegate.py ticket prepare` 를 안내한다. 장부 없는 draft 의 architect 설계 인라인 자리로만 남는다.
- 위임 스킬 카드 `pm-dev-delegate` 의 opencode 판을 지웠다. canonical 1판 + codex override 1판이며, claude 와 opencode 가 함께 읽는 `.claude/skills/` 자리는 canonical 이 채운다. codex 판의 사실 오류 3건(native 위임 산출이 라운드 파일에 남는다는 서술의 반대)도 정정했다.

### Changed

- **BREAKING — `ticket done`·`cluster closed`·`slot released` 를 서로 다른 상태로 분리한다.** 활성 묶음 멤버의 `board.py complete` 직접 호출은 첫 write 전에 거부하고, `ticket_finish.py` 의 `ClusterCloser` 가 넘기는 내부 결속값이 티켓 frontmatter·장부의 양방향 귀속과 정확히 같을 때만 완료 게이트로 들어간다. 장부·역참조가 둘 다 없는 구세대 티켓은 크기 1 해석 id 와 같은 결속만 통과한다. 부트스트랩 슬롯 카드에서 direct complete 처방을 없애 완료 진입은 `/pm-wave-finish` 하나이며, 손상된 cluster 장부가 하나라도 있으면 mutation 은 첫 write 전에 정지한다(조회용 목록과 분리 · 판정 불능은 통과가 아니다).
- 추가 리뷰어의 canonical 물리 진입점을 `.project_manager/tools/additional_reviewer.py`로 완전 개명했다. 구 `external_review.py`는 shim으로 남기지 않고, `pm_update` full sync가 신 파일 설치를 확인한 뒤 채택자의 구 파일을 `.pm_import_backups/`에 보존하고 퇴역한다. 과거 raw/config/header/role 판독은 호환을 유지하지만 신규 writer는 `additional_reviewer` 식별자만 생성한다.

- 우리가 띄우는 행위자를 `외부` 로 부르던 표기를 역할 이름(추가 리뷰어·위임·하네스)으로 바꾸고, `외부 전송`·`외부 송신` 계열 표기까지 출하 표면 전량에서 0 으로 고정하는 검사를 `tests/test_terminology.py` 에 추가했다. 기계 밖으로 나가는 행위·저장소 밖 경로의 `외부` 는 그대로 둔다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- 남은 판단 축을 자기 근거로 부른다 — 라운드·wave 예산이 세는 것은 **유료 호출** 횟수다. 엔진 안내·카드·문서의 `전송`/`송신` 표기를 `호출` 로 바꿨고, 손대지 않은 축은 이 예산 상한 하나뿐이다.
- **위임자는 피위임자에게 자신과 같은 권한을 준다** — 위임 방향·하네스 조합과 무관하다(코덱스가 PM 일 때 클로드에게 위임하든, 오픈코드가 코덱스에게 위임하든 같다). 이 규칙을 판단 원칙 레지스트리와 세 하네스의 위임 카드에 실었고, 위임 경로에서 피위임자 권한을 좁히는 자리는 파리티 원장에 등재해 새 좁힘이 미등재로 걸리게 했다. 남는 비대칭은 CLI 형식과 역할축(generate≠evaluate)뿐이다.
- 묶음 종결(`ticket_finish.py --cluster`)이 차단 사유를 하나 찾을 때마다 멈추지 않고, 첫 부작용 앞에서 읽기로 판정 가능한 사유를 전부 모아 번호와 함께 한 번에 보고한다. 죽은 확인 단계를 지워 파이프라인은 8단계에서 7단계가 됐다.
- 엔진이 자기 작업용으로 만드는 임시 디렉터리를 OS 임시 폴더가 아니라 프로젝트 안 고정 폴더 `.project_manager/.local/tmp` 에 만들고 실행 후 비운다(`pm_relay.temp_root` 단일 소유 · 생성 호출 4자리). 대상 파일 옆에 써야 하는 원자적 쓰기 3곳은 그대로다.
- **이어 시키는 위임은 새 순번을 만들지 않고 같은 순번을 다시 연다** — `pm_delegate.py ticket prepare --reopen-ordinal N`. 재개방은 라운드 예산을 소비하지 않고, 슬롯 사본은 board 라운드 파일의 현재 내용으로 시드되며, 회수는 그 파일을 통째로 교체한다(과거 판은 board git 이 보존). 한 단계 = 한 순번 = 한 파일이다. ADR-0090 의 "회수 후 불변 · 이어 시키면 새 라운드" 는 ADR-0096 이 개정했다.
- 리뷰 지적 회수 판정이 삭제를 변경으로 센다(`--diff-filter` 에 `D`). 지적의 `fix_contract.test` 가 테스트를 지목하지 않아도 계약이 지목한 경로 중 하나라도 diff 에 있으면 회수되고, 지목한 경로가 하나도 없을 때만 거부한다 — 죽은 코드를 지우는 지적에 테스트를 강요하지 않는다.

### Added

- `qa.platforms` / `test.<platform>.cmd` 선언으로 Linux core 회귀와 같은 HEAD의 Windows QEMU 회귀를 하나의 `pm-qa`·livegate 증거로 집계하는 platform QA gate를 추가했다. 각 스위트 내부는 `pytest -n auto`로 병렬 실행하고, 공유 QEMU 경합을 피하기 위해 platform 간은 직렬로 검증한다.

- 하네스 이름을 조건으로 접근 권한·경로·env·판단 축을 가르는 분기를 AST 로 세어 원장 밖 자리를 거부하는 `tests/test_harness_parity_guard.py` 를 추가했다. 현재 등재는 CLI 형식(argv 조립·어댑터 설치·dry-run 표기) 20건이고 권한 분기는 0 이며, 원장은 축소 방향으로만 바뀐다.
- `pm_delegate.py ticket abandon --discard-reason <사유>` — 산출이 있는 미회수 라운드 사본을 재실행 대체 없이 폐기한다. 산출은 순번과 무관하게 board 라운드 파일로 옮겨 `pm-review-refused` 표식을 붙이고, 사본도 board 파일도 없는 행은 사유만으로 장부를 닫는다. `--superseded-by` 와 함께 줄 수 없다.
- 멤버가 전부 폐기된 묶음을 `ticket_finish.py --cluster` 가 닫는다 — 코드 산출을 전제하는 단계(완료 기록·커밋·재배치·머지)는 대상에서 빠지고 슬롯 반납과 board 기록만 돈다.

### Fixed

- `board complete` 만 먼저 실행돼 **멤버 ticket=done 인데 cluster=open** 으로 남은 반쪽 종결을 `ticket_finish.py --cluster <C> --reconcile-integrated` 로 닫는다. 멤버마다 정확히 한 evidence commit 을 요구해 선택한 제품 git 의 exact HEAD 에서 `anchor <= evidence <= HEAD` 조상 관계를 기계 검증하고(anchor 는 `claimed_rev`, 그 필드가 없는 legacy 멤버만 명시 `--legacy-base-rev`), merge·rebase·code commit·review resolve·slot release 를 0회 호출하며 작업 중인 다른 슬롯은 보존·보고만 한다. 같은 증거 재실행은 무부작용 성공이고 증거·HEAD 불일치는 거부다. 정상 8단계는 이 상태를 만나면 첫 부작용 전에 멈추고 복구 경로를 안내한다.
- `.project_manager` 가 없는 linked app worktree 의 하위 `--cwd` 가 그 app 의 Git 루트로 해소되지 않아 위임 diff 루트가 어긋나던 결함을 고쳤다(3-repo 분리 형상 채택자 제보). 잘못된 `base_rev` 의 `git diff` 실패도 빈 changed-paths 가 아니라 진단적 오류로 표면화한다.
- 종료 archive를 같은 task identity로 복원하는 `task reopen`을 추가하고, archived 이름의 신규 bootstrap을 차단했다. `task end`는 handoff 진입이 남긴 durable `pid=0` intent 뒤에만 허용해 무handoff 종료의 슬롯·state 손실을 막는다.
- Windows 11 QEMU 전체 회귀에서 발견한 99 node·13파일의 테스트 이식성 결함을 실행 인터프리터, 논리/네이티브 경로, JSON backslash, 과대한 parameter ID 축으로 전수 폐쇄했다. 가짜 WindowsApps `python3` shim을 설치한 실제 VM에서도 출하 계약을 유지한다.
- 한 shell cell의 `git-anchor` `PreToolUse` 판정은 최강 verdict만 남기고 인접한 동일 경고를 `호출 5–6 [pm-home/warn] ×2: …` 형태로 압축한다. 하위 `slot/ok` 반복은 최종 `systemMessage`에서 제외하되 deny code·호출 순서는 보존한다.

- `rounds resolve --cluster --pm-verified` 가 리뷰를 통과한(must-fix 0) 멤버를 "처분할 반려 잔여가 없습니다" 로 거부해, 통과 멤버가 하나라도 섞인 묶음이 닫히지 않던 결함을 고쳤다. 통과 멤버는 무대상으로 건너뛴다(`--gate` 단건 거부는 그대로).
- Windows 에서 자기 축 회귀의 실패 노드 ID 가 플랫폼 경로 표기로 나와 없는 실패를 새로 판정하던 것을 POSIX 표기로 고정했다(정규화 지점 `_self_axis_failed_node_ids` 한 곳 · 플랫폼 분기 0).
- 위임 raw 저장 경로의 0600·O_EXCL 보장을 단언하는 테스트가 폴백 테스트 삭제와 함께 사라져 있던 것을 실 경로에 다시 세웠다.
- 묶음 종결이 PM 홈 자신의 lease 행(`slot "."`)을 반납 대상으로 세던 결함을 고쳤다 — dirty 홈에서 "슬롯 반납 실패" 로 멈추거나, 반납 뒤 그 홈의 정체성 의존 조작(claim·checkpoint·snapshot)이 "세션 미해소" 로 막히던 자리다. 홈 행은 반납 무대상이다(`worktree_pool.HOME_SLOT` 단일 비교). (T-0897)
- `pm_log.py checkpoint` 가 compaction 트리거에서만 정체성 미해소를 rc 0 으로 조용히 생략하던 특례를 지웠다 — 어느 트리거든 rc 1 `[중단]` 하나다. `snapshot` 도 만들 텍스트가 없으면 rc 1 이고 stdout 에 아무것도 내지 않는다(`--json` 의 `{"suppressOutput":true}` 무응답 엔벨로프 삭제). 세 하네스의 압축 훅이 같은 실패를 같은 폴백 안내로 드러낸다 — codex 만 미등록 홈에서 침묵하던 차이가 사라졌다. (T-0897)
- 단일 clone 채택자(`pm_import --new` 직후 · 커밋 0 · 기준 브랜치 미탄생)의 첫 티켓 종결이 "통합 브랜치가 이 코드 트리에 없다" 로 멈추던 결함을 고쳤다. HEAD 가 앉은 미탄생 기준 브랜치는 이 종결의 커밋 단계가 첫 커밋을 얹을 자기 브랜치로 읽고(`_unborn_baseline` 관측 한 곳), 측정 폭·사설 참조 가드는 기존 "측정 불가 = 가드 off + loud 한 줄" 등급, 잔여 인구는 선언 경로뿐이다(그 밖의 미추적 파일은 건수와 `git add` 처방 한 줄로 보고 · 차단 없음). detached·다른 브랜치·선언 브랜치 부재는 종전대로 정지한다. (T-0896)

### 업그레이드 노트

- **BREAKING — 티켓 완료 진입이 `/pm-wave-finish`(`ticket_finish.py`) 하나다.** `board.py new` 로
  발행한 티켓은 크기 1 장부에 귀속되므로 `board.py complete T-NNNN` 직접 호출은 이제 첫 write 전에
  거부된다. 스크립트·문서·훅에서 direct complete 를 부르고 있으면 묶음 종결 호출로 바꿔라. 부트스트랩
  슬롯 카드에서도 그 줄이 사라졌다.
- **BREAKING — 반쪽 종결은 복구 전용 경로로만 닫힌다.** 과거에 `board complete` 만 실행해 멤버가 전부
  `done` 인데 장부가 `open` 으로 남은 묶음이 있으면, 정상 종결은 첫 부작용 전에 멈추고 복구 경로를
  안내한다. 그 상태는 아래로 닫는다(제품 git 과 슬롯에 쓰기 0 · 작업 중인 다른 슬롯 보존).

  ```
  ticket_finish.py --cluster C-<이름> --reconcile-integrated \
    --integrated-rev T-NNNN=<commit> [--legacy-base-rev T-NNNN=<commit>] \
    --user-ack C-<이름> --repo <repo> --slot <N>
  ```

  멤버마다 evidence commit 이 정확히 하나 필요하고, `claimed_rev` 가 없는 옛 티켓만 `--legacy-base-rev`
  로 시작점을 명시한다. `board.py cluster show <이름>` 으로 멤버와 상태를 먼저 확인하라.
- **`external_review.py` 는 없어졌다.** canonical 진입점은 `.project_manager/tools/additional_reviewer.py`
  다. shim 을 남기지 않으므로 옛 경로를 부르는 스크립트는 고쳐야 한다. `pm_update` full sync 가 신
  파일 설치를 확인한 뒤 채택자의 구 파일을 `.pm_import_backups/` 로 옮긴다.

- **BREAKING — `board.py cluster new` 는 코드 트리 정체성을 요구한다.** `--repo <이름> --slot <N>` 또는 `--task <이름>` 없이 부르던 스크립트·카드는 거부된다. 추측으로 PM 홈에 만들어졌던 통합 브랜치가 있으면 `cluster show` 의 선언값과 실제 저장소를 대조해 손으로 정리한다.
- **BREAKING — `board.py section-add` 는 묶음 장부가 있는 티켓에 거부된다.** 라운드를 이어 시키려면 `pm_delegate.py ticket prepare --reopen-ordinal N` 으로 같은 순번을 다시 연다. 폐기·복원으로 표식이 붙은 순번을 다시 시킬 때는 재개방 뒤 슬롯 사본에서 `pm-review-refused` 줄을 지운다.
- **BREAKING — PM 홈·엔진 루트를 추측하지 않는다.** anchor 의 `.git` 이 공용 저장소를 가리키지 않는 형상(복사본 checkout·submodule 없는 사본)에서는 이제 오류다. 오류 문안이 anchor 와 공용 저장소를 찍으므로 그 값으로 형상을 고친다.
- 임시 파일이 `.project_manager/.local/tmp` 아래로 온다. 그 경로를 백업·감시 대상에서 빼고 있었다면 확인한다.
- opencode 채택자: `pm_update` 는 인스턴스 소유 guest 절의 옛 `pm-dev-delegate` override 행을 보존한다. `./pm-config.sh add-harness --harness opencode` 를 한 번 다시 돌려 canonical 카드로 갱신한다.

## [1.7.11] - 2026-08-25

### Added

- OpenCode `session.idle`에서 선언 후 무실행·열린 todo·잘린 응답을 감지하면 원인별 재개 처방을 주입하는 stall watchdog을 추가했다. 연속 무진행·세션 총량 상한과 진행 시 reset으로 무한 재개를 막는다.

### Changed

- OpenCode 대형 파일 작성은 실측에 따라 safe-write 전환 하한 64KB와 권장 chunk 16KB를 사용한다. researcher의 제한된 bash 생성 경로를 역할 계약과 맞추고 mode 값을 enum으로 고정했다.

### Fixed

- OpenCode safe-write가 개행 없이 끝난 파일에 개행 없이 시작하는 chunk를 append해 줄 경계를 붙이는 손상을 거부하고, 다음 조각의 정확한 복구 방법을 안내한다.
- `board.py regression run --cwd <target>`가 실제 실행 target의 Git HEAD와 절대 `conf_anchor`를 같은 좌표로 기록하고, `check`도 그 target의 HEAD·수집 하한을 기준으로 stale을 판정한다. 손상된 present anchor는 fail-closed이며 key가 없는 legacy 기록만 PM 홈으로 폴백한다.
- Codex `PreToolUse` native matcher를 `Bash`·`collaborationspawn_agent`로 제한하고 프롬프트 단위 ctx/principle 알림을 도구 호출 경로에서 제거했다. live Bash payload에 내부 workdir가 없을 때만 PM 홈 상대 pytest 오차단을 1회 warning으로 낮추며, 명시 좌표·Git·엔진 mutation deny는 유지한다.

## [1.7.10] - 2026-08-25

### Changed

- **BREAKING — 티켓 개발을 PM → architect → developer → reviewer → fix의 고정 5단계로 수렴시킨다.** architect 테스트 계약은 developer 종료 게이트가 되고 reviewer must-fix는 수정·추가 회귀 계약을 포함하며, 마지막 fix가 두 테스트 계약과 전체 회귀를 모두 통과해야 한다. fix 뒤 추가 설계·구현·리뷰 라운드는 열리지 않는다.
- **리뷰 잔여 처분은 `--pm-verified` 하나만 남는다.** 추가/내부 리뷰 CLI의 `--into`·`--fixed`, `pm-fixed`, 클러스터 replan writer와 legacy reader를 삭제했다. 옛 resolution kind는 현행 처분으로 해석하지 않아 잔여가 있으면 fail-loud한다.

## [1.7.9] - 2026-08-24

### 업그레이드 노트

- **BREAKING — 운영 단위가 티켓에서 묶음(클러스터)으로 바뀐다.** 설계·리뷰·fix 는 묶음당 1회, 개발만
  티켓당 1회이며 티켓 하나짜리 wave 도 크기 1 묶음이라 같은 경로를 탄다(특례·별도 코드 경로 없음).
  티켓 frontmatter 에 `cluster` 필드 하나가 붙고, 필드도 장부도 없는 기존 티켓은 **읽는 자리에서**
  크기 1 로 접히므로 파일 마이그레이션이 없다. 발행은 활성 묶음에 새 티켓을 끼워 넣지 않는다 —
  티켓마다 그 티켓 이름의 크기 1 장부를 함께 만들고 그 사실을 stderr 1줄로 고지하며, 여러 티켓을
  한 묶음으로 묶는 것은 `board.py cluster new` 선언이다(그때 크기 1 장부는 흡수된다). 자동 생성된
  크기 1 장부는 발행 시점 코드 트리의 브랜치를 `base_branch` 로 싣고 묶음 브랜치(`branch`)만
  비우므로, 종결의 머지 단계가 `묶음 브랜치 미선언 — 무대상` 으로 건너뛴다. `base_branch` 가 비면
  (detached·비-git 트리에서 발행) 재배치·머지는 무대상이 아니라 **정지** 다 — 판정 기준이 없기
  때문이다. `board.py cluster show <이름>` 으로 확인해 채운 뒤 다시 실행한다.
- **BREAKING — 라운드 수를 장부의 고정 예산이 정한다.** 묶음 라운드 순번이 곧 단계이고
  (`01-architect` → `02-developer` → `03-code-reviewer` → `04-developer`), 라운드 예약이 예산
  초과와 순서 밖 역할을 거부한다. 판정은 준비 표면과 무관하다 — 티켓 단축 표기(`--ticket`)로
  준비해도 같은 예산·같은 순서 판정을 받으며, 장부가 없는 옛 티켓만 이 축 밖이다. 초과의 유일한
  출구는 재설계(`board.py cluster replan <이름> --reason <사유>`)로 예산 4키를 전부 리셋하고 주기를
  처음부터 다시 여는 것이며, 라운드를 더 얹는 플래그는 없다.
- **리뷰 위임의 손 git 이 0이 된다.** 격리 스냅샷 생성·프롬프트 조립·라운드 자리 예약·스냅샷 정리를
  엔진이 하고, PM 이 넣는 것은 검토 중점 문단 하나(`--focus`)다. 리뷰 입력은 장부가 선언한 통합
  브랜치와 묶음 브랜치의 merge-base 이후 변경 전부이며, 리뷰 대상 파일에 커밋되지 않은 변경이 있으면
  거부한다. 그 트리를 확정하는 것도 엔진이다 — developer 라운드를 돌려받을 때 그 슬롯의 코드
  변경이 티켓 제목을 문안으로 커밋된다(변경이 없으면 커밋도 없다). 손으로 스냅샷을 만들거나 리뷰
  전에 커밋하던 절차와 그 도구 호출은 방법론에서 사라졌다.
- **BREAKING — 추가 리뷰어의 판정 라운드 상한(전송 횟수 축)이 제거됐다.** 같은 게이트로 판정 4회를
  채우면 막던 축이 사라지고, 티켓 단위 차단은 수렴 축(`additional_reviewer.rounds_max`·must_fix
  추이)과 미완 축(`additional_reviewer.incomplete_rounds_max`)이 맡는다. 두 축이 겹치지 않는
  형상(`rounds` 가 빈 승계 항목 · `--confirm-fix` 실행)에서는 라운드가 전보다 늦게 막힌다.
  구키 `additional_reviewer_round_limit`·`external_review_round_limit` 는 v1.7.8 부터 이미 값을
  공급하지 않았고, 이제 그 축 자체가 없다.
- **라운드 장부의 폐지 필드 `acked_through` 를 버린다(승계 없음).** 폐지된 라운드 연장 승인이 남긴
  값이라 새로 늘어나지 않지만, 값이 남은 게이트는 집계 창이 잘려 있었다. 정규화가 그 키를 떨구며
  게이트·값을 한 번 알리고, 장부가 다시 기록되면 안내는 사라진다(별도 마이그레이션 명령 없음).
  구 장부는 그대로 읽히고 게이트 항목 판별도 유지된다(`count` 가 같은 판별을 덮는다).
- **managed `.codex/hooks.json` 값 변경 — codex 채택자는 `/hooks` 재승인 1회가 필요하다.** 훅 이벤트
  6종(`PreToolUse`·`UserPromptSubmit`·`PostToolUse`·`SubagentStart`·`PreCompact`·`PostCompact`)이
  이벤트당 범용 진입점 하나(`matcher .*` → `.codex/pm_orch_codex.py --hook-dispatch <이벤트>`)로
  통일됐고 timeout 이 전부 15 다. 무편집 채택자는 `pm-update` 가 백업 후 교체하며(같은 실행에서
  진입점 소견 0줄), 손편집한 채택자는 `pm-config sync-adapter-config --accept .codex/hooks.json`
  1커맨드로 받는다. 가드 **동작**은 바뀌지 않는다 — 옛 matcher 판정이 디스패처 registry 로 값
  그대로 옮겨 왔다(T-0777·T-0806 합산 1회).
- **BREAKING — 솔로 모드 폐지. PM 홈이 장부의 첫 슬롯 행(`<repo>_1`)으로 등록된다.** multi-PM 이 단일
  운영 방식이고 슬롯 1개짜리 홈은 그 안의 N=1 경우다. "lease 행이 없다 = 솔로 형상"으로 읽던 추론과
  그 위의 솔로 전용 폴백(정체성 자동 해소·회귀/livegate cwd·핸드오프 경로·완료 회귀 트리)이 전부
  제거됐다. 마이그레이션은 `pm-config update`(pm-update) 흡수 말미에 1회 자동 실행되며 멱등하다
  (장부 byte 동일). 신규 채택은 `pm-config init`(pm-import 경유 포함)이 그 자리에서 등록한다.
  등록 repo 가 2개 이상이면 어느 repo 의 홈인지 기계가 정할 수 없어 등록하지 않고 안내만 낸다 —
  그 홈은 종전대로 `--repo/--slot` 명시로 운영한다.
- **BREAKING — 등록 전(lease 행 0개) 홈의 귀속 조작은 fail-loud 한다.** 조용한 폴백이 없으므로
  claim·complete·핸드오프·livegate 는 `[중단] 세션 미해소` 로 멈춘다. 처방은 `pm-update` 1회(또는
  `--repo <repo> --slot <N>` 명시)다.
- **BREAKING — `pm_state.md` 위치 이동.** git-tracked `wiki/pm_state.md` 를 쓰던 홈은 첫 핸드오프에서
  내용이 `.project_manager/.local/slots/<repo>_1/pm_state.md`(git-ignored)로 옮겨진다. 원본은 이동
  후 남지 않으므로, 그 경로를 참조하던 채택자 스크립트·문서는 슬롯 경로로 갱신한다.
- **BREAKING — handoff log 헤더에 슬롯 태그가 붙는다.** 무태그 `## [날짜] handoff | PM N차 → 다음 PM
  세션` 대신 `PM N차 (<repo>_1)` 로 적힌다. 기존 무태그 entry 는 그대로 읽히며(소유 판정은 태그 없는
  과거 entry 를 현재 슬롯 것으로 본다) 새로 쓰는 entry 만 태그를 갖는다.
- **BREAKING — `--user-ack` 토큰이 `solo` 에서 `<repo>_1` 로 바뀐다.** 스킬은 값을 엔진 출력에서 받아
  쓰므로 계약 문구는 그대로이고, `--user-ack solo` 를 하드코딩한 스크립트만 갱신이 필요하다.
  bare 핸드오프는 슬롯 축으로 승격돼 `--session-seq`·`--wave-summary` 를 종전대로 요구한다.
- **BREAKING — `local.conf` 키 표기 통일(점 표기 단일) · 구키 수용 없음.** flat snake_case·
  suffix-per-harness 등 혼재 표기가 점 표기 하나로 바뀐다. `reviewer_cmd` 는 제거되고 구조화
  `ReviewerTarget`(`additional_reviewer.*`)이 필수이며, `delegate_enabled` 는 `delegate.enabled`
  (채널 무관 마스터 스위치·기본 true)로 재정의된다. 구표기 값은 소비 지점에서 조용한 폴백 없이
  fail-loud 로 멈추고 `board.py init` 재실행이 신표기 재생성을 처방한다. `pm-update` 는 apply 를
  막지 않으며(엔진은 받게 한다) `pm_import`/`pm_update` 가 교체 안내를 출력한다. 모델 값은 자동
  이관하지 않는다(하네스·모델 조합은 환경마다 달라 수동 설정). 채택자 소유 `.codex/config.toml` 의
  delegate 문구는 키 단위로 지목된다. 출하 conf 템플릿은 실값만 담고 키 설명은 출하 문서로 옮겼다
  (T-0767).
- **developer 라운드 준비에 architect 선행 게이트가 생긴다.** `ticket prepare --role developer`·cross
  자동 준비·`board section-add --role developer` 는 선행 architect 라운드(실 산출)가 있어야 통과하며,
  묶음 architect 라운드가 그 티켓 자리를 채웠으면 같은 게이트를 통과한다. 없으면 rc≠0 으로 거부된다.
  **면제 경로는 없다** — `design` 값은 `required | done | n/a` 세 형식뿐이고 폐지된 면제 표기는
  발행·`board.py design <T-NNNN> <값>` 양쪽에서 거부된다.
- **완료 기록(`ticket_finish`)이 `touches` 밖 미스테이지 잔여를 차단한다.** 종전에는 스테이지에서
  빼고 경고만 냈으나, 이제 잔여가 있으면 board 무변경으로 rc≠0 거부하고 잔여 목록과 처방(`touches`
  보강)을 출력한다. 테스트 파일을 `touches` 에 적지 않은 티켓은 완료 기록이 막히므로 선언을 맞춘다
  (T-0854).
- **완료 기록 preflight 에 사설 참조 유입 차단이 생긴다.** claim 이후 추가한 줄(출하 python 표면 ∩
  `touches`)에 채택자가 조회할 수 없는 참조(티켓 ID·사설 라벨)가 있으면 `파일:라인 토큰` 을 지목하며
  rc 1 로 막는다. 미커밋 신규 줄은 raw 전부가 차단 축이고, 커밋된 줄의 raw-only 는 `git blame` sha 를
  실은 경고다. 판정식은 `private_refs.py` 를 그대로 호출하며 allowlist·baseline 파일을 읽지 않는다
  (재생성 우회 없음). 완료 기록 순서를 사람이 지키던 규칙은 사라졌다 — 종결 파이프라인이 커밋·재배치·
  머지를 고정 순서로 실행하고, 판정 기준도 커밋 시점과 무관하게 바뀌었다(아래 Changed 참조).
- **편집 시 자동 회귀 훅(`run_tests_hook.sh`) 폐지 — 기존 채택자는 직접 정리한다.** 상류는 삭제를
  전파하지 않으므로(`pm_update` 는 상류 삭제를 dest 에 적용하지 않고, `.claude/run_tests_hook.sh` 는
  manifest **파일** 엔트리라 은퇴 파일 보고에도 안 잡힌다) 기존 인스턴스의 훅은 그대로 계속 돈다.
  끄려면 **이 순서로** 정리한다.
  1. `.claude/settings.json` 에서 `hooks.PostToolUse` 의 `run_tests_hook.sh` 블록과
     `permissions.allow` 의 `"Bash(./.claude/run_tests_hook.sh)"` 행을 지운다.
  2. 그 다음 `.claude/run_tests_hook.sh` 파일을 지운다.
  역순(파일 먼저)이면 배선이 남은 편집마다 `not found` 비차단 오류(rc127)가 뜬다. `settings.json` 은
  인스턴스 소유라 이 편집은 다음 동기에 덮이지 않는다. `local.conf` 의 `test_cmd` 키는 유지한다 —
  회귀 게이트(`board.py regression`·pre-push 훅)가 같은 키의 주 소비자다.

### Removed

- **BREAKING — 내부 리뷰 채널의 확인 전용 라운드(`--confirm-fix`)와 PM 직접 해소 처분
  (`rounds resolve --pm-fixed`) 폐지.** 상한을 한 번 더 여는 인자와 그 상한을 소진해서 여는 처분이
  같이 사라진다. 상한·예산에 걸린 게이트의 출구는 재설계 하나이며, 반려 잔여의 처분은 후속 티켓
  (`--into`)·근거 게이트(`--fixed`)·기계 확인(`--pm-verified`) 셋뿐이다. 두 표기를 붙여 호출하면
  usage error 로 거부된다(조용한 무시 없음). 추가 리뷰어 채널(`external_review.py`)의 같은 이름
  플래그는 그대로다 — 폐지 대상은 내부 채널이다. 이미 기록된 `pm-fixed` 처분은 완료 재검증에서
  종전대로 읽힌다.
- **설계 면제 값 폐지** — `design` 값 집합이 `required | done | n/a` 로 줄고 폐지된 면제 표기는 발행·
  `board.py design` 양쪽에서 rc=1 로 거부된다. 면제가 남으면 그 티켓만 라운드 순번이 어긋나고 면제가
  곧 우회 플래그가 되기 때문이다. 설계가 몇 줄이면 몇 줄로 쓰고 `done` 으로 올린다. 기존 티켓에 남은
  면제 값은 `lint` 가 표면화한다.
- 솔로 형상 추론·솔로 전용 분기 일체 — "등록 repo 1개 && lease 행 0개 → `<repo>_1`" 유도층
  (`identity_args.single_registration_session`·`lease_row_count`), livegate/회귀 cwd 의 PM 홈 폴백,
  핸드오프의 legacy `wiki/pm_state.md` 결속, `pm_log` 의 legacy pm_state 정체성 층, 솔로 전용
  상수·메시지 (T-0793).
- **추가 리뷰어 판정 라운드 상한 축 제거** — 상수(`DEFAULT_ROUND_LIMIT`)·차단 분기·안내 문구의
  판정 상한 항목이 사라진다. 실 장부 53게이트에서 한 번도 발동하지 않은 축이고, 같은 범위를
  수렴 축이 must_fix 추이로 더 정확히 본다 (T-0772).
- **라운드 장부 스키마에서 `acked_through` 제거** — 정규화·집계·조회 표(`--rounds-report`)·게이트
  항목 판별 마커에서 모두 빠진다. 값이 남은 구 장부는 loud 안내와 함께 그 값을 버린다 (T-0772).
- **편집 시 자동 회귀 훅(`run_tests_hook.sh`) 제거** — `.py` 편집 1회마다 `test_cmd`(전체 회귀)를
  돌리던 claude_code 전용 `PostToolUse` 훅을 폐지했다. 훅 본체 2본·배선(양 `settings.json` 의
  `PostToolUse` 블록과 권한 행)·manifest 등재 2본·`pm_import` 어댑터 훅 집합 항목·전용 테스트
  2파일이 사라진다. 회귀 보장 층은 줄지 않는다 — 이 훅이 실제로 낸 유일한 신호(수집 단계 오류)는
  티켓 지정 회귀와 push 게이트 회귀가 중복으로 잡고, 전체 회귀의 실행 지점은 릴리즈 절차 1회다.
  claude_code 채택자의 `.claude/settings.json` 에서 `PostToolUse` 항목이 없어지며, 그 이벤트 자체를
  금지하지는 않는다(채택자 자작 훅은 무관) (T-0771).

### Added

- **클러스터 장부와 묶음 조작 커맨드** — `tickets/clusters/<이름>.md` 가 멤버·통합 브랜치·묶음
  브랜치·설계 문서·라운드 예산·재설계 기록을 담는다. `board.py cluster new <이름> --tickets ...
  [--spike <경로>]` 가 장부와 묶음 브랜치를 만들고 겹침·가용 슬롯 재료를 낸다(자동 묶기 없음).
  `cluster show` 는 선언값과 멤버 현재 status 를, `cluster replan <이름> --reason <사유>` 는 재설계
  1회를 기록하며 예산을 리셋한다. `board.py lint` 가 중복 귀속·멤버 부재를 표면화한다.
- **묶음 리뷰 라운드** — `pm_delegate.py --role code-reviewer --cluster <C-이름>` 이 격리 스냅샷을
  저장소 밖에 만들고, 프롬프트(리뷰 단위·스냅샷 경로·입력 범위·변경 파일·멤버 티켓 본문 N·PM 검토
  중점)를 조립하고, run-dir 하나에 티켓별 리뷰 자리 N개를 예약한 뒤 실행하고 회수한다. `--focus`
  는 PM 검토 중점 문단, `--background` 는 분리 세션 실행이고 회수는 `cluster wait --cluster <C-이름>
  --cwd <worktree>` 가 라운드 회수 상태로 판정한다. `review delta|disposition-template|
  verify-template` 과 `rounds resolve --pm-verified` 도 `--cluster` 를 받아 멤버 전부를 한 번에 낸다.
- **묶음 라운드 예산** — 장부의 `budget` 4키(`architect`·`developer_per_ticket`·`code-reviewer`·`fix`)
  가 라운드 역할 수열을 정하고, 묶음 라운드 예약이 그 수열 밖 요청을 예약 **전에** 거부하며 재설계
  처방을 낸다. 확인 전용 라운드도 예산 안에서 산다(예산 밖 송신을 여는 인자 없음).
- **묶음 종결 파이프라인** — `ticket_finish.py --cluster <C-이름>`(티켓 ID 를 준 호출도 그 티켓의
  묶음으로 해소된다)이 기계 확인 → 리뷰 게이트 처분 → 티켓별 완료 기록 → 슬롯 커밋 → 통합 브랜치로
  재배치 → 통합 브랜치 머지 → 슬롯 반납 → board·포인터 커밋을 고정 순서로 실행한다. 각 단계는 자기
  부작용이 이미 있는지 **관측**해서 건너뛰므로 실패 지점을 고치고 다시 부르면 재개가 되고, 관측 자체가
  실패하면 무대상으로 접지 않고 그 자리에서 멈춘다. 커밋 문안은 엔진이 낸다(티켓 커밋은 티켓 제목,
  머지는 `<단위> merge — <제목>`).
- **`pm_delegate.py changelog material --since <tag|rev>`** — 릴리즈 노트 재료 추출. 코드 체크아웃
  (`local.conf upstream.path`)에서 그 rev 의 커밋 시각을 해소하고, 그 시각 이후 완료된 done 티켓의
  목표·결정·완료 조건을 티켓당 블록(분류 후보·채택자 영향 인용·근거 절)으로 stdout 에 낸다. board 는
  읽기만 하고, 분류 확정과 문안은 내지 않는다(사람이 쓴다). 코드 체크아웃이나 rev 를 해소하지 못하면
  빈 목록이 아니라 rc≠0 이다.
- `local.conf` 노브 `delegate.code-reviewer.rounds_max`(기본 3) — 내부 code-reviewer 라운드 수렴
  상한을 채택자가 조정한다. 추가 리뷰어 축과 **별개 예산**이라 한쪽을 올려도 과금 라운드는 늘지
  않는다. 거부 안내가 설정값과 키를 그대로 싣고, `pm-fixed` 처분의 발동·완료 재검증도 같은
  값을 본다 (T-0772).
- **판단 원칙 레지스트리 + 하네스 3종 recall 주입** — `wiki/pm_principles.md`(출하·`pm_update`
  관리)에 RECALL(패턴 매칭 시 주입)·JUDGMENT(판단 시점 원칙) 항목을 두고 로더
  `tools/pm_principles.py`(`load`·`judge_recall`·`count`·`rearm`)가 읽는다. claude 는
  `ctx_stop_hook.py`, codex 는 `pm_orch_codex.py --hook-dispatch PreToolUse`(in-process), opencode 는
  새 플러그인 `.opencode/plugins/principle-recall.js` 가 매칭 항목을 `[principle-recall]` 로 주입한다.
  PM 홈 로컬층 `pm_principles.local.md` 는 같은 스키마로 채택자 소유(manifest 미등재) (T-0848).
- **티켓 처분 종결 상태 `discarded/` + `discard`·`reopen` 서브커맨드** — `board.py discard <T-NNNN>
  merged|dropped --reason` 이 open·claimed·blocked 는 물론 `.drafts/` 의 draft 도 받아 `discarded/` 로
  옮기고 사유를 기록한다(draft 출처는 `discarded_from: draft`). `reopen` 은 draft 출처면 `.drafts/`
  로, 그 밖은 `open/` 으로 되돌린다. 번호 소비 규칙은 불변이고, 미회수(round-pending) 라운드가 있는
  티켓의 discard 는 차단 없이 `ⓘ` abandon 처방 1줄만 낸다. `complete` 는 DoD 전항이 `[>]` 이월이고
  `[x]` 가 0건이면 거부(discard 처방)하며, `complete`·`block`·`unclaim`·`unblock` 은 실행 세션이
  `claimed_by` 와 다르면 거부한다(해소는 `unclaim --takeover --reason` 1회) (T-0781·T-0865).
- **내부 위임(architect·developer·code-reviewer) 라운드 기계 상한** — 역할별 상한에 도달하면
  `ticket prepare` 가 거부하고 현재 라운드 목록과 재설계·분할 처방을 낸다. 새 conf 키·플래그는 없고
  우회 수단은 외부 채널과 같은 것 하나뿐이며 사유가 장부에 남는다 (T-0841).
- **`livegate record` 의 PM 홈 엔진 사본 drift-0 선행조건** — 실행 엔진 사본(`.project_manager/tools/`)이
  upstream 과 drift 하면 라이브 wave 를 돌리기 전에 rc 1 + fail 기록으로 거부한다. 엔진 사본 rev skew 도
  판정 불능으로 접지 않고 fail 기록으로 번역한다 (T-0861·T-0848).
- **완료 preflight 를 base 대비 자기 축 델타로 재설계** — 완료 대상 작업 트리에서 `claimed_rev` 대비
  신규 실패만 차단하고, base 부터 red 였던 상속 실패는 경고만 낸다. 병합 미리보기(합성 트리) 방식은
  폐기됐다. 새 conf 키·플래그 0 (T-0847).
- **위임 잔여 정리 `ticket abandon`** — kill·미재개 위임이 남긴 시드 라운드 예약·미회수 장부 행을
  정리한다. 기본 거부(fail-closed)이며 native 위임처럼 프로세스 생존을 기계로 확인할 수 없으면
  `--assume-dead` 명시가 필요하다. 산출이 있는 라운드·중간 순번은 거부한다. 재실행으로 대체된 라운드는
  `--superseded-by <N>` 명시 축으로 종결한다 (T-0789·T-0850).
- **추가 리뷰어 stale 반려 게이트 종결** — 채널 폐지·PM rejected 처분 뒤에도 남던 반려 게이트를
  `--resolve-gate --pm-verified` 로 외부 재송신 없이 종결한다(`--pm-fixed` 는 여전히 거부) (T-0791).
- **codex 하네스 가드 파리티** — git cwd-anchor 보호(PM 홈/worktree 밖 커밋 deny·5필드 deny 엔벨로프)와
  세션 중 ctx nudge(잔여 밴드 진입 시 checkpoint 권고)가 codex 에도 배선된다. `.codex/hooks.json` 은
  무변경이며 판정은 디스패처 내부 분기다. ADR-0081 D3 은 "PreCompact 단일" 에서 "잔여 밴드 + PreCompact"
  로 개정 (T-0765·T-0770).
- **위임 모델 카드 5역할×3타깃 완성** — claude_code·opencode 에 `developer-hard` 카드가 신설돼 hard
  티어가 전용 지침으로 돈다. codex 역할 카드 4장에 `model`·`model_reasoning_effort` 가
  `local.conf delegate.<role>.model/.reasoning` 값으로 렌더된다(ADR-0070 D5 "모델 생략" 폐기) (T-0766).
- **티켓 발행 시점 `touches` 겹침·가용 슬롯 표면화** — `board.py new`·`promote` 가 다른 활성/draft
  티켓과 겹치는 경로별 집계와 lease 장부 실측 가용 슬롯 수를 stderr 에 낸다(never-block) (T-0778).
- `board.py lint` 의 `local-conf-unknown-key` advisory — 엔진이 읽지 않는 키(오타·폐기 키)를 1줄로
  표면화한다(`--gate` 종료코드 비기여). `init` 병합 경로에도 같은 목록이 뜬다 (T-0761).

### Changed

- **preflight novel 판정 기준 교체** — "미커밋 줄"이 아니라 "통합 브랜치의 조상이 아닌 줄"이 novel
  이다. 커밋·재배치가 언제 일어나도 판정이 같으므로 완료 기록 순서를 사람이 지킬 이유가 사라졌다.
  흡수분(조상)은 경고로 내려간다.
- **diff 서킷브레이커 폭 기준 교체** — 측정 구간이 `merge-base(<통합 브랜치>, HEAD)..작업트리` 이고,
  같은 트리에 있는 형제 티켓 `touches` 는 잔여 인구에서 빠진다. 재배치 여부가 폭을 바꾸지 않는다.
- **판단 원칙 레지스트리에서 완료 기록 순서 항목이 빠지고 리뷰 스냅샷 항목의 주어가 엔진으로 바뀐다.**
  순서 항목은 판정 기준 교체로 무의미해졌고, 스냅샷은 엔진이 장부의 묶음 브랜치 tip 에 결속해 만든다.
- **방법론·스킬 카드가 묶음 경로로 교체됐다** — 운영 매뉴얼(`pm_role.md`)·플레이북(`pm_playbook.md`)과
  위임·claim·종결·티켓 발행·추가 리뷰어 카드가 묶음 단계 표(선언 → 설계 1 → 개발 N → 리뷰 1 → fix 1 →
  종결)와 라운드 순번=단계 규약으로 서술된다. 티켓당 설계·리뷰 절차 서술과 PM 손 스냅샷·손 커밋 절차는
  사라졌고, 크기 1 묶음 한 문장이 그 자리를 대신한다.
- codex 훅 배선이 이벤트별 직결 커맨드에서 진입점 + 디스패처 registry 로 바뀐다 — 이후 가드 기능
  추가는 엔진 코드 변경뿐이고 채택자 config·재승인을 다시 요구하지 않는다. 등록 기능 목록은
  `python3 .codex/pm_orch_codex.py --hook-features` 가 JSON 으로 낸다 (T-0777·T-0806).
- **채택자 대면 메시지의 '부기' 표기 폐지** — 일본식 한자어(附記/簿記)를 '기록' 계열로 바꿨다
  (`[완료] T-NNNN 기록 완료.`·`board-git 기록 보류:`·`이중 기록 가능`·`back-ref 기록`). 엔진 메시지·
  argparse help·방법론 문서·스킬·3타깃 템플릿 사본이 모두 새 표기이며, 이 문자열을 파싱하거나
  단언하는 채택자 스크립트는 갱신이 필요하다. 동작·스키마·플래그는 무변경이다.
- **출하 산문의 리뷰 루프 서술을 현행 흐름으로 정리** — 추가 리뷰어는 기본 OFF 인 opt-in 채널이라
  "내부 code-reviewer + 추가 리뷰어 (둘 다)"·"표준 리뷰 게이트" 표기를 걷어내고, 켠 채택자만
  병행하는 것으로 서술한다. 기본 흐름은 code-reviewer 1회 → PM 판정 delta → PM 기계 확인이다.
- **`board.py promote` 내용 검토 게이트** — 형식 검사에 더해 본문이 인용한 실측이 범위 밖이면 red,
  존재하지 않는 `touches` 경로는 경고 1줄. `design: required|done` 티켓은 architect 라운드 산출이
  회수·충전돼 있어야 통과한다 (T-0776).
- **raw 장부 보존 상수 상향** — `raw_outputs.json` 완료 레코드 보존이 7일/256건에서 90일/4096건으로
  늘어 요약 레코드가 원문 `.txt` 보다 먼저 사라지던 역전을 정정했다. 장부가 참조하지 않는 고아 원문은
  건수·바이트 경고 1줄과 읽기 전용 목록으로만 표면화한다(삭제 코드 없음) (T-0774).
- **사설 참조 가드 시야 확장 + 출하 산문 위생** — 가드가 어댑터 python(`.claude/ctx_*.py` 등)과
  비-python 출하 표면 11언어(셸·cmd·TOML·JS/CJS·manifest·JSON/JSONC·rules·txt·확장자 없음) 85파일까지
  본다. 출하 산문의 조회 불가 티켓 ID·circled 번호(①·⑦)·fault 라벨(F1 류)·스트립 잔재(빈 괄호)가 읽을
  수 있는 표현으로 정리됐고, 채택자 대면 CLI 출력(`pm_bootstrap.py`·`pm_orch_*.py --help`·
  `worktree_pool.py set-base`)도 같은 규칙이다. 동작·스키마 무변경 (T-0814·T-0818·T-0810·T-0801·T-0820).

### Fixed

- 리뷰 라운드 기계 판정 — 확인 라운드의 finding 재선언을 `harvest` 시점에 거부(fail-late 제거) ·
  시드 그대로인 라운드를 `review delta`·`verify-template`·`rounds resolve` 판정면에서 무해화(malformed
  차단 제거) · pending 시드 1건이 확인 이력을 지우던 병합 충돌 정정 · `rounds recalculate` 가 회수된
  라운드 파일의 기계 블록에서 판정을 재도출 · code-reviewer 판정이 terminal reply 대신 라운드 파일
  기계 블록을 우선 입력으로 사용(오탐 재리뷰 처방 제거) (T-0788·T-0813·T-0822·T-0842·T-0804).
- 빈틈 보고(prescription-gap) 라운드가 PM 기계 확인을 전면 차단하던 결함 — 검증 사유 어휘에
  `prescription-gap` 이 추가돼 태만(`missing`)과 구별되며 `missing`·`stale` 만 rc≠0 이다. 판정
  미기입 상태의 `ticket prepare` 는 부작용 없이 거부된다. 검증 골격의 boolean placeholder·`command`
  금지 문자 안내도 실값으로 정정 (T-0805·T-0808).
- 미회수 developer 라운드 위에서 code-reviewer·추가 리뷰어 라운드를 준비하면 stderr 경고(rc 무변경) ·
  리뷰 프롬프트 미리보기가 실제로 실리는 라운드 이름을 값으로 말한다 (T-0807·T-0812·T-0819).
- 위임 라운드 예약이 거부·예외·경합에서 run-dir·미회수 장부 행을 남기던 문제를 단일 `try/finally`
  경계로 정정. 정리 실패가 원 거부 사유·rc 를 덮지 않는다(제어 예외 포함) (T-0846).
- cross 전용 역할에 수동 `ticket prepare` 를 실행하면 고아 시드가 남던 문제 — rc≠0 거부·board 무변경
  (T-0855).
- codex code-reviewer 격리 실행이 `--cwd` 검토 대상 대신 빈 mount 에서 시작하던 결함 — 실행 전
  preflight 가 root↔`--cwd` 불일치·비-저장소·staged 0 을 외부 호출 없이 잡는다 (T-0844).
- codex 훅 디스패처 — deny 와 `additionalContext` 동시 발화 시 후자가 유실되던 합본 정정(이벤트별
  output 스키마 허용키 합본·규칙 없는 키 fail-loud) · 자기 경로 재구성이 flat 레이아웃에서 실패하던
  문제 정정(`Path(__file__)` 기준) · ctx 가드가 첫 turn 에 0% 단정 안내를 내지 않는다
  (T-0824·T-0845·T-0835).
- diff 서킷브레이커 — wave 공유 트리에서 타 티켓 변경분을 합산해 4~10배로 오측정하던 문제(공유
  디렉터리 양보 보정) · `pm_update --all-targets` 기계 산출(templates 어댑터층)을 손작업으로 계상하던
  문제(`engine.manifest` `@source=` 파생 제외) 정정 (T-0790·T-0832).
- `board.py show` 가 티켓 단위 원격 신선도를 대조·표기하고 `claim` 거부 문구에 stale·behind 수치를
  싣는다 · claimed 티켓의 `block`→`unblock` 왕복이 소유 필드를 보존한 채 claimed 로 돌아온다(lint
  advisory 추가) (T-0782·T-0783).
- `worktree_pool.py` — `rebase --onto <로컬 브랜치>` 가 stale `origin/<b>` 로 조용히 대체되던 문제
  정정(무인자도 로컬이 앞서면 로컬 tip + stderr 좌우 커밋 수) · `switch` 가 다른 worktree 에 checkout 된
  브랜치를 강제 리셋하던 결함을 rc 1 거부로 정정 (T-0849·T-0859).
- git-anchor 사본 대조가 sha 불일치를 무조건 "stale import 사본" 으로 단정하던 문제 — worktree
  dirty/clean 으로 방향을 판정하고 판정 불가를 표기한다(경고 전용) (T-0800).
- 티켓 상태 디렉터리 집합 소비처 통일 — `external_review`·`pm_log` census·`pm_bootstrap` dump 가
  `board.STATUS_DIRS` 파생으로 바뀌어 `discarded` 같은 새 상태가 조용히 누락되지 않는다 (T-0839).
- 완료 기록 실체화 경로의 쓰기 규약 — newline 미선언·삭제 실패 무시를 정정(정리 실패 표면화) (T-0851).
- 리뷰 라운드 finding ID 충돌 — 라운드 시드와 사본 프리앰블이 다음 finding ID 실값을 싣고, 엔진이
  시드에 넣은 그 ID 는 회수 판정에서 선언으로 세지 않는다(자기 충돌 제거). 중간 순번이라 보존한
  board 라운드 시드에는 엔진 표식을 발행해 `round-pending`·판정 표면·직전 산출에서 함께 빠진다.

## [1.7.8] - 2026-08-22

리뷰·확인 프로세스 기계화 릴리즈 — 리뷰 라운드 수렴 비용(확인 라운드 reviewer 재투입)을 기계 판정으로
대체하고, 컴팩션 복구 주입을 장부 실측으로 보강한다.

### 업그레이드 노트

- **BREAKING — local.conf `session=`/`prefix=` 폴백 폐지.** solo 전용 분기가 제거되고 정체성 진실은
  lease 장부와 areas 레지스트리다. 그 두 키만으로 돌던 인스턴스는 조용한 폴백 없이 fail-loud 하며,
  `board.py init` 재실행이 areas repo 행을 항상 등록한다(마이그레이션·안내 문구는 실행 시 출력).
  fresh 홈의 첫 `pm-config repo add` 처럼 lease 이전 조작은 `--owner <id>` 또는 `$PM_SESSION_NAME`
  명시가 필요하다.
- **추가 리뷰어 구키 4종은 여전히 읽지 않는다(v1.7.7 폐지 유지).** `external_review_enabled` 와 노브
  `external_review_round_limit`·`external_review_incomplete_round_limit`·`external_review_wave_budget`
  은 제거됐다 — 신키 `additional_reviewer_enabled`·`additional_reviewer_*` 로만 동작한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **fix 라운드 산출에 검증 골격이 의무화된다.** developer fix 골격에 accepted finding 별
  `pm-review-verify-v1` 행(재현 커맨드·기대값·fix 전 실값 — 메타문자 없는 단일 비파괴 명령)이
  시드되고, PM 이 직접 실행한 기계 확인은 명세 PM 영역의 `pm-review-confirmation-v1` 블록(라운드
  결속·단조 순서·expected⊆observed)으로 남는다. `pm_delegate.py review verify-template` 이 골격을
  프리필하며 verify 행 없는 accepted 가 있으면 rc=1 이다. 완료 게이트에 `pm-verified` 처분
  (`pm_delegate.py rounds resolve --pm-verified`)이 추가되어 reviewer 재스폰 없이 기계 확인 증거
  (delta 정상 파싱·accepted 0·기계 확인 ≥1 — 완료 시점 라이브 재검증)로 티켓을 닫는다.

### Added

- 리뷰 delta 꼬리에 수정 범위 제약 블록(허용 범위 내 수정·빈틈은 보고-후-종료가 정상 산출) 1회 부착 —
  fix 라운드 처방 밖 수정을 렌더러 수준에서 봉쇄 (T-0785).
- 확인 라운드 기계화: verify/confirmation 블록 2종·`review verify-template` CLI·`pm-verified` 완료
  처분·확인 라운드 스코프 문구 상수(cross charter+native 시드 양 경로) (T-0786).
- 컴팩션 snapshot 에 전언 경고 절(always-keep — deadline·총량 cap 양 경로 보존)과 진행 중 작업 절
  (미회수 라운드·미마감 raw·claimed 티켓·슬롯 WIP — 장부 in-process 실측·subprocess 는 WIP git
  프로브 ≤3회만·deadline 잔여 재계산·제어문자 정규화·줄/절 상한 강제·다중 슬롯 생략 표기) (T-0787).
- `SNAPSHOT_MAX_BYTES` ≤ opencode 채널 maxBuffer 파리티 가드 신설 (T-0787).

### Changed

- claude_code git-anchor hook 이 같은 세션의 완전일치 중복 경고를 첫 1회만 발화한다(멤버십은 기존
  `.local/ctx-stop/` 파일 1개·마커 IO 실패는 fail-open 발화 유지) (T-0764).
- Windows 콘솔 인코딩이 ambient env(`PYTHONUTF8` 등)에 기대지 않고 엔진 코드로 처리된다 — 콘솔
  reconfigure 연결 가드 포함 (T-0762).
- 장부 행 검증의 board 모듈 로드가 캐시되어 행별 재-import 가 제거된다 (T-0787).
- `pm_relay` raw 장부에 무락 읽기 경로(`lock=False`)가 추가된다 — 컴팩션 훅 경로가 배타락을 잡지
  않는다(기본값 불변) (T-0787).

### Fixed

- solo 전용 분기 잔재로 폐지 키를 안내하던 산문 표면 4곳(부트스트랩 카드·pm_role·manual-import)
  교정 (T-0779).
- fix 라운드 delta 렌더·harvest 표시면의 스테일 산문·기대값 다수(전량 회귀에서 검출된 blast-radius
  잔여 포함 — 스테일 테스트 8건 갱신·엔진 산문 private-ref 22줄 제거).

## [1.7.7] - 2026-08-19

### 업그레이드 노트

- **BREAKING — 티켓 컨테이너가 명세 파일 + 라운드 사이드카로 바뀐다 (채택자 1회 마이그레이션).**
  기존 board 를 쓰는 인스턴스는 엔진 흡수 후 **한 번** 실행한다:
  `python3 .project_manager/tools/board.py rounds migrate`
  (미회수 구 위임 사본이 남아 있으면 rc 1 로 멈춘다 — 목록을 확인한 뒤 `--discard-unharvested` 로
  함께 폐기한다. `--dry-run` 은 계획만 출력한다.) 미실행 board 는 `board.py lint` 가
  `legacy-growth-section` 으로 red 이고 pm-update 가 흡수 뒤 같은 문구로 한 번 안내한다.
  - **데이터 이동**: 역할 산출(리뷰·설계·구현 보충·조사)은 티켓 본문이 아니라
    `tickets/rounds/T-NNNN/NN-<role>.md`(고정 위치 · 티켓 상태 이동을 따라가지 않음)에 산다. 티켓 안
    `pm-ticket-section`·`pm-ticket-seal` 문법과 성장 장부 `tickets/.growth/`(+ `.gitattributes` 의 그
    union 선언)는 사라진다. `board.py show` 는 명세 + 라운드를 조립해 보여 준다.
  - **삭제된 CLI**: `pm_delegate.py ticket seal-backfill` · `ticket prepare --transfer-from` ·
    `--capability-stdin` · `external_review.py --ticket-body-max`.
  - **무시되는 local.conf 키**: `review_ticket_body_max_bytes`(추가 리뷰어 입력 상한). 입력 선별이
    파일 단위(명세 + 역할별 마지막 라운드)로 바뀌어 바이트 상한 자체가 없어졌다.
  - **위임 왕복**: `ticket prepare` 가 board 에 라운드를 예약(빈 시드 파일)하고 슬롯 run-dir
    `<T>/<run>/`(역할 세그먼트 없음)을 만든다 — 쓰기 가능 파일은 라운드 파일 `NN-<role>.md` 하나이고
    `spec.md`(명세)·`rounds/`(이전 라운드)는 읽기 전용 입력이다. `ticket harvest` 는 run 당 1회이며
    성공 시 run-dir 을 지운다. PM 홈 장부는 `.local/delegate-rounds.jsonl` 하나이고 구
    `ticket_copies.jsonl`·`delegate-ticket-copy-trust/`·구 레이아웃 사본은 위 명령이 지운다.
  - **거부 표식 정책**: 회수 검증에 걸린 추가 리뷰어 산출은 라운드 파일을 만들지 않고 raw 로만
    남는다(`pm-review-refused` 는 엔진 전용 표식이며 새 회수는 발행하지 않는다). 변환은 옛 거부 산출의
    그 줄을 그대로 옮겨 판정 표면 제외를 유지한다.
  - **되돌림**: board 커밋 1개(`rounds migrate: N tickets · .growth removed`) revert. 구 사본 산출물
    삭제는 비가역이라 미회수분만 명시 플래그로 막는다.
- **어댑터 카드·스킬·방법론(pm_role·pm_playbook)이 라운드 파일 모델로 다시 쓰였다.** 위임 프롬프트는
  라운드 파일 절대경로를 지정하고, 대상 역할은 그 파일 하나에만 쓴다(첫 줄 헤더 보존 · 그 아래 엔진이
  시드한 골격을 채움 · 스키마 재타이핑 없음). 손으로 고친 카드가 있다면 다음 흡수가 canonical 로
  되돌린다.

- **guest 어댑터(`add-harness` 로 얹은 하네스)의 렌더물이 이제 `pm-update` 로 갱신된다.** 그 카드의
  `model` 을 손으로 고쳐 뒀다면 다음 흡수가 `local.conf` 값(또는 미설정 시 중화 주석)으로 되돌린다 —
  값을 유지하려면 카드가 아니라 `local.conf` 의 `delegate.<role>[.<tier>].{model,reasoning}` 에 둔다.
  `add-harness <harness>` 재실행은 어댑터 파일이 새로 추가/폐기됐을 때만 필요하다.
- **이 판을 받는 흡수는 스코프 없이(전량) 실행한다.** `--paths` 로 어댑터만 좁히면 인스턴스에 설치된
  구 엔진이 새 토큰(`{{DELEGATE_MODEL_DEVELOPER_HARD}}` 등)을 몰라 렌더 leak 으로 rc1 이 된다.
  전량 실행은 같은 계획 안에서 엔진(`.project_manager/tools/**`)을 먼저 얹으므로 안전하다.

- (v1.7.2 이월 안내) **추가 리뷰어 구키 4종은 제거됐다(읽지 않는다).** 게이트
  `external_review_enabled` 와 노브 `external_review_round_limit`·`external_review_wave_budget`·
  `external_review_incomplete_round_limit` 가 대상이다. 구키만 있는 `local.conf` 는 추가 리뷰어가
  꺼진 상태이므로, 키 이름을 신키(`additional_reviewer_*`)로 직접 바꾸거나 opt-in
  질문(`board.py init`·`pm-update`)에 다시 답한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Changed

- **diff 서킷브레이커가 claim 시점 rev 부터 잰다.** `board.py claim` 이 그 시점 코드 트리 HEAD 를
  ticket frontmatter `claimed_rev` 로 박제하고, 완료 부기(`ticket_finish`)와 추가 리뷰어 진입 검사
  (`external_review`)가 그 rev 와 현재 작업트리의 차이를 측정 폭으로 쓴다. dev 브랜치를 `--no-ff`
  merge 로 흡수하고 전파 커밋이 뒤따르는 형상에서 옛 폭(작업트리 → 비면 직전 커밋 한 칸)이 0 줄로
  접혀 상한 초과 wave 가 통과하던 것을 닫는다. 박제 실패(비-git·코드 트리 미해소)는 경고 1줄이고
  claim 을 막지 않으며, `claimed_rev` 가 없거나 이 트리에서 해소되지 않는 구 티켓은 옛 폭으로 재되
  "폭 과소 측정 가능" 경고를 남긴다. `--base` 명시는 종전대로 그 폭이 우선한다.

- **티켓 컨테이너 = 명세 파일 + 라운드 사이드카 (ADR-0090 · R1~R7).** 티켓 한 파일에 역할 절을
  키우고 봉인·성장 장부·MAC·신뢰 사본·baseline·절 밖 대조·차등 판정·반사실 프로브로 보정하던 단일
  파일 컨테이너를 없애고, 명세(`tickets/<status>/T-NNNN-*.md`)와 라운드
  (`tickets/rounds/T-NNNN/NN-<role>.md` · 티켓 전역 순번 · `os.O_EXCL` 예약)로 나눴다.
  - `ticket_rounds.py`(신설 seam): 경로 규약 · 라운드 예약/적재/교체 · `verify_rounds`(round-gap ·
    round-dup 은 blocking · round-name · round-pending · round-temporary · round-unreadable 은 표시) ·
    산출 없음(pending) 판정은 라운드 파일 하나의 내용 구조만 본다(날짜·다른 라운드·명세 비의존) ·
    같은 역할의 직전 산출 라운드 규칙(`latest_round_of_role` · pending 제외)이 시드 프리필과 추가
    리뷰어 확인 대상 ID 의 단일 소유자.
  - `pm_delegate.py`: `ticket prepare/harvest/copies` 재작성(위 업그레이드 노트) · 삭제된 장치의
    심볼 94개·CLI 3개·테스트 3파일 제거 · `review delta`/disposition 골격은 라운드 파일을 읽는다.
  - `board.py`: `show` = 명세 + 라운드 조립(미회수 표시) · `section-add` = 라운드 예약 ·
    `complete` 게이트에 `verify_rounds`(gap/dup red) · `reid` 가 `rounds/` 도 rename · `promote` 가
    `rounds/T/` 를 함께 커밋 · lint `round-*`·`legacy-growth-section`(blocking) · 절명은
    `ticket_rounds.ROLE_LABELS` 파생 · `rounds migrate`(위) 신설.
  - `external_review.py`: 회수 = 내용 검증 통과 시 `ticket_rounds.reserve_round(content=…)` 로 라운드
    파일 직접 생성(거부는 raw 만) · 입력 = 명세 + 역할별 마지막 라운드 파일 · 입력 바이트 상한 삭제.
  - `ticket_finish.py`: stage 후보에 `tickets/rounds/T-NNNN/` 의 라운드 파일(문법 판정은
    `ticket_rounds.parse_round_filename` · 점-접두 임시 파일 제외 · board-git 분리 형상 제외).
  - 어댑터 3타깃(claude `.claude/agents`·`skills` · codex `.codex/agents`·`.agents/skills` · opencode
    `.opencode/agents`·`command`) + `pm_role.md`·`pm_playbook.md`: 라운드 파일 모델 문구 · 옛 컨테이너
    어휘 0 을 기계 가드로 고정(`test_delegation_docs_drop_single_file_container_vocabulary`) ·
    fresh-adopter e2e 가 3타깃 template 사본에서 준비→쓰기→회수 1라운드를 실제로 돈다.

- **PM 홈 push 는 회귀를 요구하지 않는다 — 회귀 게이트는 `tests/` 가 있는 트리(코드 repo)에만
  붙는다**(채택자 제보 2항목). pre-push 훅이 push 시점에 그 트리를 보고 스스로 가린다: `tests/` 가
  있으면 종전대로 `regression check || regression run --final` 을 요구하고, 없으면(코드가 worktree
  슬롯에 사는 분리 형상의 PM 홈) 회귀 줄을 건너뛰며 그 사실을 한 줄로 알린다. 제보 형상에서는 코드가
  한 줄도 바뀌지 않은 board/wiki push 가 슬롯 스위트 전량(앱 14,292건·30분+)을 요구했고, leased
  슬롯이 둘 이상이면 회귀 cwd 가 모호하다는 이유로 push 자체가 거부됐다. 그 원인이던 회귀 cwd 의
  활성 슬롯 우회는 삭제됐다 — 회귀는 push 되는 트리 자신에서 돌고, 다른 트리를 겨냥하려면
  `--cwd`(또는 `--task`)로 명시한다. 릴리즈 라이브 게이트(`livegate record`)의 슬롯 해소와
  `--repo`/`--slot` 핀은 그대로다.

  **lint 게이트는 두 형상 모두 그대로 push 를 막는다** — 대상이 그 트리 자신의 board/wiki 라 코드
  유무와 무관하다. 코드 쪽 게이트도 무변경이다(`tests/` 있는 트리의 회귀 게이트·티켓 마감
  `ticket_finish --tests-pass`·보호 브랜치 훅·라이브 게이트). 새 게이트를 대신 붙이지 않았다.

  같은 축으로 두 가지가 더 정리됐다. (1) `board.py regression run`(무 `--cwd`/`--task`)은 스위트가
  없는 트리에서 **pytest 를 띄우기 전에 거부**한다 — 그 트리에 `tests/` 가 없는데 test_cmd 가
  이 트리를 대상으로 삼는 경우(`tests/` 지목, 또는 수집 경로 미지정이라 cwd 를 재귀 수집)이며,
  측정도 기록도 하지 않고 처방(`--cwd <코드 트리>` / `--task <이름>`)을 낸다. 경로 미지정 형상까지
  보는 이유는 엔진 기본 폴백 test_cmd(`pytest -q`)가 스위트 없는 홈에서 슬롯 worktree 의 테스트를
  잘못된 rootdir 로 긁어 **FULL green 을 기록**했기 때문이다. 자기 경로를 명시하는 커스텀
  test_cmd(`pytest src -q` 등)와 비-pytest test_cmd 는 무영향이다. rc5(수집 0) 진단도 더 이상
  세션/lease 설정을 지목하지 않는다(회귀 cwd 가 그것을 보지 않으므로 거짓 처방이었다). (2) 훅 세대
  판정이 `lint --gate` 진입에도 붙는다 — 회귀 게이트가 안 도는 트리에서도 구버전 훅이 있으면 같은
  `board.py init` 처방으로 push 가 막힌다(보고 전용 `lint` 는 무변경). 그 안내 문구의 접두는
  호출 경로 중립(`pre-push 훅: `)이라 `/pm-bootstrap` 의 lint 칸도 이 창을 "파싱 실패"가 아니라
  "훅 세대 차단"으로 보고한다.

  **인스턴스 조치**: 이미 깔린 옛 훅은 세대가 달라 push 시 차단되고 `board.py init` 재실행 1회를
  처방한다(엔진은 설치된 훅을 몰래 고치지 않는다). 흡수(`pm-update`) 뒤 `board.py init` 을 한 번
  돌리면 새 훅으로 교체된다. 남의 pre-push 훅·서명 없는 훅은 대상이 아니다.

### Fixed

- **Windows 잠복 테스트 함정 정리.** 테스트의 전역 `os.name` 변이 16곳(7파일)을 엔진 seam
  (`board._probe_os_name` · `console_encoding` · `pm_bootstrap` · `pm_handoff` · `ticket_finish`) patch 로
  바꾸고, python 자식 subprocess 의 encoding 미명시 4곳을 명시했다. 병렬 회귀(`pytest -n 8`)의 자식
  pytest 가 cp949 콘솔에서 한글 경고를 내다 리더 스레드가 죽던 1건(`test_regression_parallel`)은
  자식 env `PYTHONUTF8=1` 로 닫았다(Windows VM `-n 8` 전수 실측). 재발 방지 AST 가드
  `tests/test_tests_windows_portability_discipline.py`.
- **opencode 역할 권한: 출하 카드 frontmatter `permission` == 런타임 fragment
  (`pm_relay.opencode_runtime_role_config`) 파리티 가드**(4역할 전수 · webfetch deny 절대 앵커). 카드만
  고치고 fragment 를 남기는 half-fix 클래스가 기계로 닫혔다(테스트만).
- **엔진 부트스트랩 블록(22 모듈 동일) 편집 파급 목록 기계화**(테스트만): 라인-핀 allowlist 를
  (파일·함수·호출형태) 심볼 키로 바꿔 라인 이동에 무감·같은 함수 안 신규 쓰기에 red · 가드 실패
  메시지가 원장 2종(hard_allowlist=`--regenerate` 기계 재생성 / baseline=검토형 ratchet 손 갱신)의
  재핀 절차를 가리킨다.
- **위임 모델 선언(`local.conf delegate.*`)이 세 하네스의 agent 카드 전부에 도달한다.** 카드의
  `model` 은 `delegate.<role>[.<tier>].{model,reasoning}` 의 렌더 파생물인데 세 곳이 어긋나 있었다:
  (1) `add-harness` 로 얹은 guest 어댑터(예 codex host + claude guest)의 렌더물은 update 계획에서
  빠져 conf 를 바꿔도 설치 시점 카드가 남았고(실측: 카드 `opus` ↔ conf `sonnet` 상시 경고),
  (2) codex `.codex/agents/developer-hard.toml` 의 모델·추론이 리터럴이었으며, (3) opencode 역할
  카드 4장이 역할 무관 단일 토큰(`{{OPENCODE_PRO_MODEL}}`)을 썼다. 이제 guest 절의 `@render` 행도
  core 와 같은 재렌더 경로를 타고(dry-run 은 `[render]` 로 예고), codex 티어 프로필과 opencode 역할
  카드가 역할별 토큰으로 렌더된다(opencode `pm.md` 는 PM 자신의 모델이라 설치 모델 pin 유지).
  카드가 가리키는 하네스가 conf 의 그 역할 하네스와 다르면 **미사용 프로필**이라 값을 채우지 않고
  사유를 남긴 채 중화한다(`# model: <model>  # TODO: …`) — 미해소는 update rc 를 바꾸지 않는다.
  카드 손편집은 다음 흡수가 conf 값으로 되돌린다(그게 렌더물의 의도된 동작이다). 인스턴스 조치는
  없다 — 갱신을 실행하는 것은 인스턴스에 설치된 `pm_update.py` 이므로, 이 판을 받은 뒤의 다음
  갱신에서 host/guest 무관하게 카드가 conf 와 일치한다. 다만 카드가 모델을 계속 명시하려면 그 값이
  `local.conf` 에 있어야 한다: opencode 역할 카드 4장은 `delegate.<role>.model`(설치 시 잡힌
  `opencode_pro_model` 은 이제 `pm.md` 에만 적용된다), codex hard 티어 프로필
  (`.codex/agents/developer-hard.toml`)은 `delegate.developer.hard.model`·
  `delegate.developer.hard.reasoning` 이다. 미설정이면 그 줄이 중화(주석화)돼 카드에 model 키가
  없는 상태가 되고, 하네스는 자기 config 기본 모델로 스폰한다(hard 티어의 상향 프로필은 conf 에
  값을 넣어야 유지된다).

## [1.7.6] - 2026-08-18

### 업그레이드 노트

- **티켓의 역할 절은 이제 엔진 경로로만 쓰인다(봉인).** `section-add`·`harvest`·`seal-backfill` 이
  각 역할 절 뒤에 봉인 줄(`<!-- pm-ticket-seal role=… ordinal=… sha256=… by=… -->`)을 같은 원자
  쓰기로 남기고, `review delta`·`board complete`·`lint` 가 절 본문과 봉인의 sha256 일치를 확인한다.
  손편집·절 이동·고아 봉인·중복 봉인은 loud RED 이며 우회 플래그는 없다. **업그레이드 직후 1회**,
  기존 open/claimed/blocked/draft 티켓의 미봉인 역할 절을 정합화해야 한다:

  ```bash
  python3 .project_manager/tools/pm_delegate.py ticket seal-backfill --ticket T-NNNN
  ```

  이 명령을 돌리기 전에는 그 티켓의 `prepare`·`harvest`·`section-add` 가 처방과 함께 거부된다
  (done 티켓은 소급 검증 대상이 아니라 조치 불요). 성장 절이 없는 티켓은 영향이 없다.

  봉인된 절과 미봉인 절이 **섞인** 티켓(업그레이드 뒤 구 엔진 사본으로 절을 한 번 더 만든 경우)은
  `seal-backfill` 대상이 아니다 — 그 명령은 대필을 막기 위해 mixed 를 거부한다. 이때는 거부 메시지가
  해당 절의 role·ordinal·본문 유무를 짚어 실행 가능한 복구를 안내한다: 빈 절이면 제거 후
  `section-add` 재생성, 내용이 있으면 제거·재생성한 뒤 사본을 다시 `prepare` 해 역할이 재기록하게
  한다. 역할 산출을 사람이 옮겨 적거나 봉인을 손으로 만드는 경로는 없다.

- **역할 절의 개수·이력이 티켓 파일 밖 장부에도 남는다 — 절을 통째로 지우는 손편집이 검출된다.**
  봉인은 절 *본문*의 변조를 잡지만 마지막 절을 봉인째 지우면 남는 흔적이 없었다. 이제 엔진이 절을
  쓸 때마다 `<board>/tickets/.growth/T-NNNN.jsonl` 에 (역할, 차수, sha256, 쓴 주체) 레코드를
  append-only 로 함께 남기고, `review delta`·`board complete`·`section-add`·`harvest`·`prepare` 가 티켓의
  봉인과 장부를 대조한다 — 장부에는 있는데 티켓에 없는 절은 "절 삭제 검출" 로 loud RED 이며 backfill
  로도 지워지지 않는다(레코드 제거 경로가 없다). 쓰는 주체는 엔진 경로뿐이다(`section-add`·`harvest`·
  `seal-backfill`·`promote`·추가 리뷰어 절 기록). draft 티켓은 장부에 쓰지 않고 `promote` 시점에 1회
  기록한다. board-git 형상에서는 장부 파일이 티켓과 같은 부분 커밋에 실리고 `.gitattributes` 에
  `merge=union` 이 선언된다. **업그레이드 직후 1회, 보드 단위로 sweep 한다:**

  ```bash
  python3 .project_manager/tools/pm_delegate.py ticket seal-backfill --all
  ```

  이 명령은 위 봉인 backfill 과 장부 생성을 함께 처리하고(봉인은 이미 있고 장부만 없는 티켓도 대상)
  잔여 0 이 되면 마이그레이션 stamp(`tickets/.growth/.migrated`)를 남긴다. sweep 전에는 봉인은 있고
  장부가 없는 티켓의 `prepare`·`harvest`·`section-add` 가 이 처방과 함께 거부되고, stamp **이후** 같은
  상태는 마이그레이션 잔여가 아니라 장부 파일 삭제 의심으로 판정된다(backfill 로 되살리지 않는다).
  done 티켓은 소급 대상이 아니다.

- **Windows native 에서 티켓 성장 위임(prepare→역할 실행→harvest)이 다시 동작한다.** v1.7.5 는 사본 루트를
  `info/exclude` 에 등록하는 단계가 POSIX 전용 안전 경계(dirfd/nofollow)를 필수로 요구해 Windows 에서
  rc=1 로 거부됐다. 사본 루트 `.project_manager/.local/delegate-ticket-copies/` 는 tracked
  `.project_manager/.gitignore` 의 `.local/` 규칙으로 이미 무시되므로 그 등록 단계와 안전 경계를
  제거하고 `git check-ignore` 확인만 남겼다. 그 확인은 무시 여부만이 아니라 **규칙의 출처**도 본다 —
  출처가 tracked `.project_manager/.gitignore` 여야 통과하고, 이 클론에만 있는 로컬 규칙(`.git/info/exclude`·
  전역 `core.excludesFile`·untracked 상위 `.gitignore`)만으로 무시되는 형상은 다른 클론·채택자 트리에서
  사본이 그대로 노출되므로 그 사실을 짚어 거부한다(첫 커밋 전 트리는 인덱스 등록으로 충분하다).
  채택자가 `.gitignore` 의 `.local/` 규칙을 지운 형상은 prepare 가 복구 처방과 함께 fail-loud 한다.
  인스턴스 조치는 없다.
- (v1.7.2 이월 안내) **추가 리뷰어 구키 4종은 제거됐다(읽지 않는다).** 게이트
  `external_review_enabled` 와 노브 `external_review_round_limit`·`external_review_wave_budget`·
  `external_review_incomplete_round_limit` 가 대상이다. 구키만 있는 `local.conf` 는 추가 리뷰어가
  꺼진 상태이므로, 키 이름을 신키(`additional_reviewer_*`)로 직접 바꾸거나 opt-in
  질문(`board.py init`·`pm-update`)에 다시 답한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Added

- **추가 리뷰어(codex) 산출이 티켓 절로 회수되고 내부 리뷰어와 같은 판정 표면에 오른다.**
  `external_review --ticket T-NNNN` 이 끝나면 엔진이 회신 전문을 그 티켓의 `external-reviewer` 역할 절로
  기록한다(봉인·장부·게이트는 내부 리뷰어 harvest 와 같고, 리뷰어에게 티켓/사본 편집 권한을 주지
  않는다). 회신 안의 `pm-review-v1` 블록은 결함 ID 에 `X-` 접두가 붙어 내부 리뷰어 ID 와 섞이지 않고,
  `review delta`·PM 판정 블록이 두 채널을 한 표면에서 다룬다. 블록에 **심각도**(`must-fix`/`should-fix`/
  `suggestion`) 필드가 생겨 리뷰어 산문·블록·PM 판정 3중 기재 없이 블록만으로 "반드시 고쳐야 하는가"
  가 기계로 읽히며, `board show T-NNNN` 이 본문 뒤에 라운드별 판정 요약을 붙인다. researcher 도 같은
  티켓 사본 왕복을 타서 조사 산출이 티켓 절로 남는다 — 그래서 researcher 카드는 저장소는 읽기 전용
  계약을 유지한 채 자기 사본 절만 편집할 수 있는 최소 권한(claude `Edit`·opencode `edit: allow`·codex
  `workspace-write`)으로 바뀌고, 세션 불일치 뒤 자동 재실행은 code-reviewer 와 같이 하지 않는다(절 이중
  기록 방지). 옛 심각도 없는 블록(v1)은 계속 읽힌다.

- **병렬 위임의 touches 겹침을 엔진이 계산해 경고한다.** `pm_delegate.py --ticket T-NNNN` 이 같은 세션이
  claim 중인 다른 티켓들의 touches 와 이 티켓의 교집합(디렉터리 접두·`<repo>_<N>/` 슬롯 표기·Windows
  구분자 정규화 포함)을 stderr `=== ⚠ 병렬 위임 touches 겹침 ===` 블록으로 낸다 — 차단하지 않으며 위임
  뒤에는 실제로 바뀐 겹침 경로를 다시 표시한다. `gate_snapshot` 은 `--paths` 가 staged 변경의 부분집합일
  때 그 사실을 경고하고 `--strict-scope` 로 차단할 수 있다. PM 이 손으로 하던 disjoint 확인이 기계
  산출로 옮겨졌다.

- **리뷰 블록 형식을 엔진이 공급한다 — 사람이 스키마를 옮겨 적지 않는다.** `section-add` 가 역할별
  본문 골격을 시드하고(리뷰어는 `must-fix`/`should-fix`/`suggestion`/판정 머리와 채워진 리뷰 블록
  자리를, developer·architect 는 보고 항목 골격을 받는다), 확인 라운드에는 직전 라운드에서 반려되지
  않은 결함 ID 가 미리 채워진다. PM 판정 블록도
  `pm_delegate.py review disposition-template --ticket T-NNNN` 이 미판정 결함 ID 를 전부 프리필해
  낸다 — 이미 판정한 항목은 값 그대로 보존한 단일 블록 전문이라 그대로 교체해 붙일 수 있다.
  블록의 키·분류·상태 낱말은 전부 엔진 파서 상수에서 파생하므로 카드·스킬·방법론 문서에 스키마를
  복제하지 않으며, 복제가 되살아나면 정적 가드가 막는다. 골격을 그대로 둔 채 회수하면 "산출 없음"
  경고가 뜬다. 직전 라운드 기록이 옛 형식이라 프리필을 만들 수 없으면 차단하지 않고 빈 골격으로
  내려가며 사유를 남긴다. 인스턴스 조치는 없다.

- **추가 리뷰어가 diff 와 함께 게이트 티켓 본문을 받는다.** `external_review --ticket T-NNNN` 이 그 티켓의
  §목표·§인터페이스·§결정·§설계·§완료 조건과 성장 절·PM 판정을 프롬프트에 함께 싣고, 리뷰 맥락에 "티켓
  §결정·§설계·PM 판정 블록은 권위 있는 확정 사항" 임을 명시한다. 확정된 설계를 되돌리라는 지적은
  `design-proposal` 로 분류되고 must-fix 로 올라오지 않는다. 성장 절은 **역할별 마지막 라운드만** 싣고
  앞선 라운드는 생략 표기로 접는다(권위 절·PM 판정은 전량) — 다라운드 티켓이 상한 때문에 교차검증을 못
  받던 형상이 사라진다. 그래도 본문이 `review_ticket_body_max_bytes`(기본 65536)를 넘으면 **자르지 않고
  전송을 거부**하며, 상한은 `--ticket-body-max <bytes>` 로 올린다.

- **어느 엔진 사본이 실행되는지 판정 시점에 알려준다.** PM 홈과 작업 트리에 엔진 사본이 함께 있는
  형상에서, 상대경로로 엔진 도구를 부르면 실제 실행될 사본의 절대경로·그 사본의 저장소 앵커·다른
  사본과의 파일 해시 일치 여부를 Bash 호출 직전에 표시한다. 기계로 확정되는 두 경우는 차단한다 —
  `tests/` 가 없는 트리에서 `pytest tests/` 를 실행할 때, 그리고 board 소유 트리가 아닌 곳에서 board
  를 변경하려 할 때. `cd <경로> && <명령>` 은 이후 호출까지 작업 디렉터리가 남는다는 경고와 함께
  대상을 절대경로로 지정하라는 처방을 낸다. claude·opencode 어댑터가 같은 대상 집합을 쓴다.

- **티켓 사본 왕복이 스스로 상태를 기억한다.** `prepare` 가 발급한 회수 자격을 PM 홈 장부에
  기록하므로 `harvest` 에 그 값을 다시 넘길 필요가 없고, `ticket copies [--unharvested]` 로 아직
  회수하지 않은 사본을 조회할 수 있다. 사본이 최신이 아니어서 회수가 거부되면
  `prepare --transfer-from <구 사본>` 이 같은 역할·차수의 절 내용을 새 사본으로 옮겨 준다 — 사람이
  역할 산출을 옮겨 적는 경로가 사라졌다. 장부는 PM 홈에만 두고 사본 쪽에는 자격 값을 남기지 않는다.

### Fixed

- **`pm-update` 가 중앙 로더 seam 을 선복구한 직후 같은 실행에서 Windows 가 간헐적으로 `ModuleNotFoundError`
  로 끝나던 것을 고쳤다.** dest 에 `repo_owned_files.py` 가 아예 없던(중단된 갱신) 인스턴스에서 복구
  사본을 쓴 뒤 이름으로 import 하는 폴백 로더가 Python import 캐시(디렉터리 목록·mtime 기준)에 막혀
  10회 중 4회꼴로 rc1 이 났다. 폴백 로더가 import 전에 `importlib.invalidate_caches()` 를 부른다(전 엔진
  모듈의 동일 부트스트랩 블록). 갱신을 실행하는 것은 인스턴스에 설치된 `pm_update.py` 이므로 v1.7.6 을 받은
  뒤의 다음 갱신부터 적용된다. 인스턴스 조치는 없다.
- **opencode 로 cross 위임된 `researcher` 가 티켓 사본의 자기 역할 절을 쓸 수 있다.** 엔진이 결속하는
  runtime 역할 fragment 가 researcher 의 `edit` 를 deny 해, 출하 카드(`edit: allow`·`bash: deny`)와
  달리 회수 절이 빈 채로 남았다. fragment 도 카드와 같은 축(edit allow · bash/task/webfetch deny)이다.
  인스턴스 조치는 없다.
- **`ticket_finish` 의 `--no-pytest` 가 회귀 실행만 건너뛴다.** 이전에는 슬롯(코드 트리) 해소까지 건너뛰어
  diff 서킷브레이커 측정과 `[4/5]` stage 가 PM 홈(분리 형상에서는 엔진 import 사본)을 보았다. 이제
  코드 트리 소비자(diff 측정·stage·PM-direct 재검)는 전부 같은 해소 결과를 쓰고, 다중 슬롯이 모호하면
  `--task <이름>` 또는 `--repo <name> --slot <N>` 명시를 요구한다(우회 없음). touches 는 repo 별 몫으로
  갈려 PM 홈-상주 경로(wiki·결정 기록·domain)와 코드 경로가 각자의 트리에 stage 되고, 추적 중인 삭제도
  남는다. `/pm-wave-finish` 카드에 task-mode 호출 경로를 명시했다.
- **Windows 에서 테스트 24건이 호스트 전제로 깨지던 것을 두 OS 대칭으로 고쳤다.** 원자 교체를 `os.replace`
  로 가로채던 테스트 16건은 엔진이 실제로 부르는 seam(`file_lock.atomic_replace`)에 주입점을 옮겼고,
  POSIX 백엔드 baseline·전역 `os.name` 변이·POSIX 절대경로 픽스처·cp949 디코드·`python3` 리터럴 처방
  8건은 호스트 판정·`pm_bootstrap` 의 os_name 프로브 seam·OS-중립 절대경로·UTF-8 디코드·엔진 해소값으로
  바꿨다(플랫폼 skip 없음).
- **추가 리뷰어 산출 회수가 판정 표면을 잠그는 절을 봉인하지 않는다.** 회수 게이트가 예정 본문에 판정 파서를
  그대로 적용해 malformed 로 *바뀌는* 절(표면 밖·PM rejected ID 를 confirmations 로 참조·finding 0 과 산문
  모순 등)을 같은 거부 처리로 돌리고, 기존 결함은 새 라운드 탓으로 세지 않는다(반사실 프로브 기준선 · 프로브
  손상은 fail-loud). 확인 전용 라운드의 근거·프롬프트 골격도 회수 거부·PM rejected 를 뺀 확인 가능한 ID 만
  싣는다.
  **알려진 한계**: 이 게이트는 "붙이기 전 티켓이 정상이었을 때" 만 판정한다(차등). 티켓이 다른 이유로
  이미 판정 불가인 동안(예: 내부 리뷰어 위임 직후 골격 절이 아직 비어 있을 때) 들어온 위반 산출은
  검사 없이 봉인될 수 있고, 그 뒤 원래 원인이 사라지면 판정 표면이 잠긴다. 단일 파일에 라운드를 누적하는
  컨테이너의 한계라 다음 릴리즈의 티켓 폴더화(라운드=파일)로 구조적으로 닫는다. 그 전까지는 내부 리뷰어
  위임과 추가 리뷰어 회수를 같은 티켓에서 겹치지 않게 순서를 두는 것이 안전하다.
- **기계 판독 출력(하네스 훅 JSON·`--json` 페이로드·capability JSON)이 콘솔 코덱과 무관하게 UTF-8 로
  나간다.** PowerShell 캡처 보정이 사람이 읽는 출력의 코덱을 cp949 로 바꾸면 같은 스트림을 쓰던 기계
  판독 한 줄까지 대체표 변환을 타서 되돌릴 수 없는 손실이 났다. 기계 판독 한 줄은 이제 단일 seam 으로
  stdout 의 바이트 레이어에 UTF-8 + LF 종결로 직접 쓰고(`sys.stdout is None` 형상은 무출력), 훅 host 가
  읽는 응답 종결자도 플랫폼과 무관하게 LF 다. claude 어댑터의 git-anchor 훅(`pm_orch_claude.py`)도 같은
  방식으로 JSON 을 내보내 Windows 파이프 stdout 의 기본 코덱에서 훅이 죽거나 빈 응답을 내던 경로를 닫았다.
  재발은 정적 가드가 막는다(엔진 `tools/*.py` 와 어댑터 훅의 JSON 텍스트 write 형태 전수 검사).

- **opencode 전송 파일 정리 실패가 더 이상 무음이 아니다.** 프롬프트 전송 파일의 쓰기 실패 뒤 롤백
  삭제가 실패하면 조용히 넘어가 부분 전송 프롬프트가 디스크에 남아도 표시가 없었고, 그 잔여를 모르는
  cleanup 이 같은 디렉터리의 자기-은닉 `.gitignore` 만 지워 민감 사본이 untracked 로 노출될 수 있었다.
  정리 실패는 경로와 함께 loud 로 올라오고(주 결과는 보존), 잔여가 남은 경우 cleanup 이 자기-은닉 ignore
  를 보존한 채 삭제를 재시도하며 그마저 실패하면 남은 경로를 알린다. 같은 판정으로 남아 있던 사유 없는
  무음 fail-soft 2곳(권한 probe 임시파일 삭제 실패·프롬프트 파일 denylist 경로 해소 실패)도 경고 또는
  사유 주석으로 바꿨다.

- **Windows checkout에서도 티켓 성장 봉인이 유지된다.** 역할 절 seal의 sha256 입력에서
  `CRLF`·lone `CR`을 `LF`로 정규화해, LF에서 발급된 기존 봉인이 Git for Windows의 CRLF
  워킹카피에서도 재발급이나 마이그레이션 없이 검증된다. 파일 bytes는 재작성하지 않는다. 별도
  board git의 `.gitattributes`에도 `*.md text eol=lf`를 멱등 backfill/seed해 이후 checkout의
  유입도 함께 막는다. 인스턴스 조치는 없다.

- **Windows 에서 엔진이 쓰는 텍스트가 더 이상 CRLF 로 변환되지 않는다.** 엔진의 텍스트 파일 쓰기가
  개행을 명시하지 않아 Windows 에서 `\n` 이 `\r\n` 으로 바뀌었고, LF 기준으로 byte 를 비교하는 지점
  (어댑터 config 채택 판정·`engine.manifest` 추가 기록·render/drift 판정·로그 tail)이 어긋났다. 엔진의
  텍스트 쓰기 전수를 LF 로 고정했다. 재발은 정적 가드가 막는다 — 텍스트 쓰기에 개행이 리터럴로
  명시됐는지 검사하며, 판정할 수 없는 형태는 통과가 아니라 실패로 둔다. 인스턴스 조치는 없다.

- **핸드오프가 이 세션의 checkpoint 를 다시 수집한다.** checkpoint 를 쓰는 쪽은 항상 정체성 태그를 달아
  기록하는데 핸드오프는 무태그 헤더를 찾고 있어, solo·`local.conf`·legacy 형상에서 handoff entry 의
  "이 세션 박제 entries" 가 비어 나왔다. 두 축을 같은 정체성 해소 체인으로 맞췄다. 정체성을 해소하지
  못하는 형상(등록되지 않은 task, 장부 조회 실패, 구 엔진)에서는 중단하지 않고 수집 전용으로 내려가며
  그 사유를 한 줄로 표시한다 — 기존 차수·window·rc 는 그대로다.

- **Windows 에서 opencode 위임과 import fill raw 저장이 다시 동작한다.** opencode 전송 파일 생성·삭제와
  `pm-import` fill 실패 원문 저장이 POSIX 전용 능력(dir_fd·O_NOFOLLOW·geteuid·procfs)이 없으면 위임/저장을
  거부하던 fail-closed 3겹을 제거했다. 방어 대상(작업 폴더 안 경로 바꿔치기)은 이미 작업 폴더 쓰기 권한자만
  가능한 경합이라 추가 이득이 없었다. 남는 경계는 플랫폼 무관하게 같다: 경로 lexical containment · `O_EXCL`
  신규 생성(기존 파일 덮어쓰기 0·충돌 시 선존재 파일 보존) · POSIX 0600/0700 · 생성 후 containment 재확인 ·
  정리 실패 loud. 능력이 있으면 강화(O_NOFOLLOW)하고 없으면 같은 기능을 이식 경로로 수행한다. 인스턴스 조치는
  없다.
- **Windows PowerShell 캡처에서 엔진 출력 한글 깨짐 해소.** 한국어 Windows(콘솔 코드페이지 949)에서
  PowerShell 이 엔진 stdout 을 캡처하면(에이전트 쉘·`$x = py -3 …`) UTF-8 출력이 cp949 로 해석돼 깨졌다.
  엔진이 조상 프로세스(`py` 런처 건너뜀)와 스트림 파이프 여부를 판정해 **PowerShell 부모 + 파이프 + 비 UTF-8
  콘솔**일 때만 그 코드페이지로 맞춰 내보내고(코드페이지에 없는 문자는 `?` 대신 대체표 변환), stderr 에
  UTF-8 프로필 설정 안내를 1회 남긴다. 콘솔 직행·Git Bash·cmd·UTF-8 로 캡처하는 Python 부모
  (`PYTHONUTF8`/`PYTHONIOENCODING`)는 종전대로 UTF-8. 엔진이 바꾼 콘솔 코드페이지는 종료 시 원복한다
  (같은 창의 후속 cp949 도구 역깨짐 방지). 인스턴스 조치는 없다 — `[Console]::OutputEncoding=UTF8` 을
  프로필에 두면 안내가 사라진다.
- **slot 세션 compaction checkpoint 복원.** `pm_log.py checkpoint` 가 slot 정체성(`<repo>_<N>`)을 예약
  패턴으로 거부하거나 정체성 미해소 시 조용히 no-op 하던 회귀를 닫았다. slot 헤더 `(<repo>_<N>)` 을
  추가하고, 정체성 미해소+compaction 은 rc=0 을 유지하되 stderr 진단을 남긴다. `pm_handoff` 의 slot 태그
  판정이 checkpoint 형 헤더를 못 잡아 solo 세션이 타 slot checkpoint 를 오수집하던 교차 오염도 정합했다.
  task·solo 형상은 byte 불변.
- 위임 raw 장부 테스트의 고정 날짜 시드가 prune 창(완료 7일)을 지나며 결정적 red 가 되던 시간폭탄을
  상대 시각으로 수리했다(엔진 무접촉).
- 티켓 사본 prepare/harvest 가 하위 디렉토리 `--cwd` 에서도 git 최상위 기준으로 동작한다
  (cross·native 양 경로 단일 seam).

#### Windows native 지원 (전수 실측 기반 일괄 수리)

Windows 11 에서 엔진 전체 회귀를 돌려 나온 실패를 원인별로 닫았다. 아래는 채택자가 체감하는
변화이며, 리눅스·macOS 동작은 바뀌지 않는다.

- **동시 쓰기 보호가 Windows 에서 실제로 선다.** 종전 배타락은 커널 대기가 아니라 유한 재시도라
  경합이 조금만 길어지면 잠금 획득 자체가 실패했다(실측: 약 9초 뒤 포기). 커널이 대기하는 방식으로
  바꿔 리눅스와 같은 무기한 획득이 된다. 여러 PM 이 같은 장부를 동시에 고쳐도 기록이 유실되지
  않는다(4 프로세스 × 25 회 동시 갱신에서 유실 0). 잠금을 못 거는 형상은 더 이상 조용히 넘어가지
  않고 경고를 남긴다.
- **줄바꿈 표기 때문에 갱신이 영구히 막히던 문제 해소.** Windows 체크아웃은 파일을 CRLF 로 펼치는데
  엔진이 내용 동일성을 바이트로 판정해, 내용이 같아도 항상 "바뀜"으로 보였다. 그 결과 어댑터 설정
  갱신이 매번 차단되고, 전파는 같은 파일을 매 실행 다시 썼다. 판정은 줄바꿈 정규화 후에 하고 쓰기는
  원래 표기를 보존한다. 엔진 소유 파일은 체크아웃 단계에서 LF 로 고정된다(`.cmd`/`.bat` 는 CRLF 유지).
- **위임 장부 기록이 Windows 에서 전면 실패하던 문제 해소.** 읽기 전용 파일 핸들에 디스크 동기화를
  걸어 Windows 가 거부했다. 동기화는 쓰기 핸들에서 수행한다.
- **정리 실패가 잔재를 남기고 조용히 끝나지 않는다.** 읽기 전용 속성이 붙은 파일(Windows git 객체가
  그렇다)을 지우지 못해도 성공으로 처리되던 경로를 닫았다. 속성을 풀고 재시도하며, 끝내 못 지우면
  남은 경로를 알린다.
- **위임 샌드박스가 Windows 임시 디렉터리를 인식한다.** 자식 프로세스 환경에서 `TEMP`/`TMP` 가 빠져
  하네스가 임시 파일 위치를 잃던 문제와, 경로 길이 상한(260자)에 걸려 쓸 수 있는 디렉터리를 "못
  쓴다"고 오판하던 문제를 함께 고쳤다.
- **재실행 안내 커맨드가 그 셸에서 실제로 붙여넣어 실행된다.** 인용 규칙을 플랫폼별로 적용하고,
  PowerShell 5.x 가 지원하지 않는 `&&` 체이닝을 안내에서 없앴다.
- **훅 응답이 잘려 가드가 무음 통과하던 문제 해소.** PowerShell 이 인자의 큰따옴표를 삼켜 훅 래퍼가
  상시 폴백하고, 그 폴백이 차단 판정을 통째로 건너뛰었다. 응답 필수 키를 엔진 상수로 고정하고
  누락은 실패로 처리한다.
- **경로 표기가 기록·판정에서 플랫폼을 따라가지 않는다.** 장부에 적히는 상대경로는 POSIX 표기로
  단일화해 다른 OS 에서도 읽힌다. 대소문자를 구분하지 않는 파일시스템에서 티켓 번호 계열이 끊기던
  문제, 사용자 홈 확장이 없는 사용자도 성공으로 보던 문제도 함께 닫았다.
- **추가 리뷰어 실행 경로가 Windows 에서 뭉개지지 않는다.** 설정의 리뷰어 커맨드를 POSIX 규칙으로만
  분해해 `C:\...` 의 구분자가 사라졌고, 그 실행 실패가 "리뷰어를 찾을 수 없음"으로 흡수돼 교차검증이
  조용히 환불됐다. 분해 규칙을 실행 플랫폼에 맞춘다.
- **원자 파일 교체가 Windows 에서도 열린 리더와 공존한다.** 엔진의 장부·설정 쓰기(임시 파일 → 교체)는
  "락 없는 리더도 일관 스냅샷을 본다"를 전제로 하는데, Windows 의 `os.replace` 는 누가 그 파일을 열어
  두면 접근 거부로 실패했다. 여러 PM 이 같은 장부를 동시에 쓰는 형상에서 쓰기가 간헐적으로 실패하던
  원인이다. 교체는 POSIX 의미 rename(`FileRenameInfoEx` + `POSIX_SEMANTICS`)으로, 읽기는 공유 삭제를 허용하는
  핸들로 열도록 엔진 전체를 옮겼다(쓰기 19지점 · 읽기 178지점). 리눅스·macOS 동작은 글자 그대로 같다.
  업데이트가 중간에 끊긴 트리를 스스로 고치는 복구 경로 2곳만 옛 방식으로 내려앉되 그 사실을 알린다.
- **native agent 카드의 모델을 `local.conf` 로 정한다.** v1.7.5 가 `delegate.<role>.model` 을 라우팅 진실로
  선언했지만 claude 카드는 `model: opus` 로 고정돼 있어 채택자가 고쳐도 다음 `pm-update` 가 되돌렸고,
  opus 이외 채택자는 상시 경고와 선언↔실행 불일치를 겪었다. 카드가 conf 해소값으로 렌더된다. 위임을 아직
  설정하지 않은 채택자는 카드가 TODO 로 중화되고 다른 어댑터 갱신은 그대로 진행된다.
- **회귀 게이트가 채택자 등록 형상 전부에서 해소된다.** `pm-config repo add` 로 등록한(prefix 빈 값 = 기본)
  채택자에서 게이트가 areas.md 의 올바른 `test_cmd` 행에 도달하지 못하고 하드코딩 `pytest tests/` 로
  폴백해, `tests/` 가 없는 repo(Go·Node·커스텀 CLI)는 회귀가 항상 red 로 오판되고 핸드오프가 막혔다.
  해소는 areas prefix 행 > areas repo 행 > `local.conf test_cmd` > 솔로 pytest 4층이고, pytest 가 아닌
  게이트는 exit code 로 판정한다(실패는 그대로 중단). `pm_handoff` 가 `ticket_finish` 와 같은 해소를 쓴다.
- **claude 단독 채택자의 `미등재 flavor 파일 관측` 오탐 해소.** host 매니페스트가 디렉터리 등재로 이미 덮는
  경로를 타 하네스의 파일 단위 선언 때문에 "미등재"로 분류하던 것을 고쳤다. 진짜 stray 파일은 여전히 경고한다.

## [1.7.5] - 2026-08-14

### 업그레이드 노트

- **추가 리뷰어 구키 4종은 더 이상 읽지 않는다.** `external_review_enabled`와
  `external_review_{round_limit,incomplete_round_limit,wave_budget}`만 남은 `local.conf`는 추가
  리뷰어가 꺼진 상태다. 대응하는 `additional_reviewer_*` 키로 직접 바꾸거나 `board.py init`·
  `pm-update`의 opt-in 질문에 다시 답한다. 엔진은 인스턴스 소유 `local.conf`를 대신 고치지 않는다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **위임 작업은 티켓의 역할별 성장 절을 사본으로 전달하고 회수할 수 있다.**
  Claude·Codex·OpenCode native 위임은 각 하네스의 `Agent`·`spawn_agent`·`task` 앞뒤에서
  `pm_delegate.py ticket prepare|harvest`를 사용한다. cross Codex reviewer의 좁은 named permission
  profile은 유지하고, Claude·OpenCode는 단일-path write 격리 미보장을 warning으로 알린 뒤에도
  사용자가 고른 target으로 실행한다. PM 홈에는 HMAC·ticket/role/ordinal·marker 밖·stale 검증을
  통과한 자기 역할 절만 회수한다.
- **스킬 카드의 상황별 운영 상세가 sibling `references/operational-details.md`로
  분리됐다.** `pm-update`가 카드와 reference를 함께 배송하므로 수동 복사는
  필요 없다. OpenCode 평면 command도 실제 model-skill reference로 링크를 해소한다.
- **bootstrap 명령 표기가 실행 환경을 따른다.** `local.conf` 의 마지막 `py=`
  assignment를 우선하고, 미설정 시 Windows는 `py -3`, Linux/macOS는 `python3`를
  표시한다. PowerShell 5.x 비호환 `&&`를 생성 명령에 사용하지 않는다.

### Added

- **선택 18셀 릴리즈 라이브 게이트** — 사용자가 실제 쓰는 native Claude·Codex·OpenCode와
  cross Claude→Codex·Claude→OpenCode·Codex→Claude의 Architect·Developer·Reviewer를
  실제 하네스로 실행한다. 각 역할은 ticket-copy 자기 절에 고유 marker를 쓰고 harvest 뒤 새
  프로세스가 canonical 티켓에서 다시 읽어 영속성·역할·marker 밖 byte 보존을 확인한다.
- **리뷰 finding의 PM 판정·승인 delta** — Architect 설계, developer 구현 판단,
  reviewer finding을 같은 성장 티켓에 남기되 PM 판정 전에는 다음 단계의 명령으로 쓰지 않는다.
  `pm-review-v1`/`pm-review-disposition-v1`의 엄격한 구조와
  `pm_delegate.py review delta --ticket T-NNNN`으로 accepted finding만 재작업 입력에 넣고,
  rejected·resolved·finding 0은 빈 delta로 끝낸다. 미판정·decision-required·동일 finding 2회
  미해소는 Architect 재설계나 티켓 분할로 돌린다. draft 티켓도 Architect만 역할 절을 안전하게
  작성·회수할 수 있어 설계부터 구현·리뷰·확인까지 한 티켓의 추적성을 유지한다.
- **성장 티켓 절** — `board.py section-add` 가 architect·developer·code-reviewer 역할
  marker를 누적하고, `pm_delegate.py ticket prepare|harvest`가 그 역할의 최신 절만
  별도 사본으로 안전하게 왕복시킨다. 파일 밖 per-run capability·HMAC, canonical
  재조회, stale/marker-밖 변경 거부, CRLF 보존, 동시 lifecycle writer 직렬화를
  포함한다.
- **티어 판별 보조** — `board.py tier-signals` 가 도구 모듈 수, 공유 코드 소비,
  docs-only 여부를 repo-owned 인벤토리에서 산출한다. `ticket_finish.py`는
  `pm-direct`의 파일 상한·테스트 동반을 완료 직전 never-block 경고로 재검한다.
- **native 위임 model drift 표면** — `delegate.<role>[.<tier>]`를 native/cross 공통
  라우팅 진실로 삼고, Claude native 카드의 `model:` 불일치·손상을 spawn 차단
  없이 `additionalContext`로 경고한다.

### Changed

- **Reviewer 위임은 항상 canonical 티켓에 귀속**된다. `--ticket` 또는 같은 canonical ID인
  `--gate`가 필수이며, resume mismatch 뒤 fresh 재실행으로 같은 reviewer 절을 중복 쓰지 않는다.
  큰 리뷰 상세도 별도 임시 산출물 대신 ticket-copy reviewer 절에 남는다.
- **15개 PM 스킬 카드를 상시 절차와 상황별 상세로 분리**했다. 상시 카드
  합계는 142,710→92,071 bytes로 줄었으며 trigger·backbone·필수 명령은 그대로
  남았다.
- **환경별 실행 안내를 공통 Windows/POSIX guide 두 벌로 수렴**했다.
  Claude·Codex·OpenCode 템플릿과 OpenCode 사람 command 표면까지 동일 계약을 배송한다.

### Fixed

- OpenCode native/cross 위임이 더 이상 내장 `build`/`plan`이나 default agent로 강등되지 않는다.
  네 역할 카드는 `mode: all`과 역할별 permission을 사용하고, cardless cross 실행도 엔진이 같은
  exact-role 런타임 설정을 주입한다. PM은 허용된 네 역할만 task로 호출하며 researcher는
  bash/edit를 갖지 않고 reviewer는 제품 코드를 바꾸지 않은 채 ticket-copy 절만 기록한다.
- 티켓 성장 write와 claim/block/complete/migration의 경합으로 상태 파일이 중복되거나
  strict claim rollback이 성장 데이터를 잃던 경합을 공용 lock 순서로 닫았다.
- ticket-copy trust 위조·cross-ticket overwrite·same-ticket stale 우회, git exclude
  symlink/hardlink 외부 쓰기, runner/harvest 이중 예외 마스킹을 fail-closed 경계와
  회귀로 막았다.
- OpenCode command 15개의 operational reference가 루트 canonical에서만 우연히
  해소되고 fresh adopter에서 끊기던 출하 결함을 실배송 상대경로 가드로
  닫았다.

## [1.7.4] - 2026-08-12

### 업그레이드 노트

- **위임 채널 기계 가드가 3하네스에 배선됐다 — 어댑터 이벤트 배선을 수용해야 발화한다.** 엔진·훅
  스크립트는 `pm-update` 가 현행화하지만 이벤트 바인딩은 인스턴스 소유 config 에 있다. claude 는
  `.claude/settings.json` 의 PreToolUse 배선을 수용하고(`./pm-config.sh sync-adapter-config --accept
  .claude/settings.json` 또는 수동 병합), codex 는 세션에서 `/hooks` 로 새 훅 정의를 재승인한다.
  opencode 는 플러그인 경로가 엔진 동기라 추가 조치가 없다. 배선 전에는 가드가 설치돼 있어도
  발화하지 않는다.
- **내부 dev→reviewer 루프에 라운드 장부가 생겼다 — 리뷰어 회신 형식이 기계 파싱 대상이다.**
  판정은 줄머리 `판정: 통과|반려`, must-fix 는 `## must-fix` 제목 아래 마크다운 목록이며 0건은
  `- 없음` 항목으로 명시한다. 형식 요구는 리뷰어 프리앰블에 기계로 실리므로 표준 경로는 조치가
  없지만, 프리앰블을 우회해 자체 리뷰어 프롬프트를 쓰는 인스턴스는 형식을 맞춘다. 산문
  "없습니다" 는 0건으로 세지 않는다(false-green 방지).
- **codex read-only 역할 위임에 쓰기 가능한 임시 디렉터리가 주입된다.** `code-reviewer`·
  `researcher` 가 read-only sandbox 에서도 회귀를 돌릴 수 있다. 인스턴스 조치는 없다.
- (v1.7.2 이월 안내) **추가 리뷰어 구키 4종은 제거됐다 — 읽지 않는다.** 게이트
  `external_review_enabled` 와 노브 `external_review_round_limit`·`external_review_wave_budget`·
  `external_review_incomplete_round_limit` 가 대상이다. 구키만 있는 `local.conf` 는 추가 리뷰어가
  꺼진 상태이므로, 키 이름을 신키(`additional_reviewer_*`)로 직접 바꾸거나 opt-in
  질문(`board.py init`·`pm-update`)에 다시 답한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **Windows 에서 opencode 위임 채널 가드가 `py` 런처를 인식한다.** 이전에는 `python3`·`python`
  만 탐색해 `py` 만 있는 환경에서 가드가 항상 fail-open 이었다.

### Added
- **opencode 슬래시 팔레트 진입 복원 (`/pm-…`)** — v1.7.0~v1.7.3 의 opencode 템플릿에는 사람이
  타이핑하는 슬래시 진입(`/pm-bootstrap` 등)이 빠져 있었다. opencode 는 팔레트를
  `{command,commands}/**/*.md` 에서만 만들고 `.claude/skills/**/SKILL.md` 는 모델의 `skill` 툴
  표면이라, 두 표면이 서로를 대체하지 못한다(1.18.16 실측). `.opencode/command/` 15개를 출하
  채널로 되돌렸다 — 저작 소스는 여전히 canonical `.claude/skills/<name>/SKILL.md` 하나이고
  command 파일은 거기서 기계 생성하며, 누락·고아·내용 drift 를 회귀가 red 로 잡는다.
  **opencode 채택자는 `pm-update` 후 팔레트에 `/pm-…` 15개가 다시 보인다.**
- **위임 채널 기계 가드 (3하네스)** — PM(LLM)이 `local.conf` 매핑과 다른 하네스로 역할을 native
  스폰하면 기계가 차단한다. 판정 코어는 하네스-중립(`decide(role, tier, conf, self_harness)` +
  한 줄 JSON CLI)이고 표면은 claude PreToolUse 훅·opencode 플러그인·codex 훅 셋이다. codex 축은
  라이브 payload 1회 실측으로 상수를 확정했고(스폰 tool `collaborationspawn_agent`·역할 필드
  `tool_input.task_name`), 훅 신뢰가 미승인이면 조용히 무력화되던 축도 함께 닫았다. deny
  envelope 는 격리 홈 3셀 실측으로 스폰 차단을 확인해 코드·문서에 박제했다.
- **내부 리뷰 루프 라운드 장부** — external_review 게이트와 동형의 기계 판정을 내부
  dev→reviewer 루프에 도입. 라운드 상한 3·발산 조기 차단·확인 전용 라운드 1회·must-fix 항목
  장부(MF-n)를 엔진이 세고, `board.py complete` 가 미처분 잔여를 차단한다. 처분 축은 통과 라운드·
  `--fixed <근거 게이트>`·`--into <T-NNNN>`·**`--pm-fixed "<근거>"`**(PM 직접 해소) 넷이며,
  오계측 라운드가 게이트를 영구히 닫지 않도록 `rounds recalculate` 복구 경로를 둔다.
- **세션 자의 행위 기계 차단 2종** — 세션이 사용자 명시 없이 ① 슬롯을 만들어 자기에게 할당하거나
  prefix 를 신설하는 축(사용자-명시 ack 강제), ② 컨텍스트 잔량·상한 도달을 종료 신호로 읽어
  핸드오프·작업 축소를 선언하는 축(ctx 가드 문구 정정 + 필수 표현 기계 검증)을 닫았다.
- **ctx 설정창-실창 불일치 감지** — `ctx_window_tokens*` 가 하네스 실 auto-compact 지점보다 크면
  넛지/정지 밴드가 한 번도 발화하지 못한 채 압축되는 형상을 기계가 감지해 loud 처방을 낸다.
  `PostCompact` 중복 발화에서도 진단이 유실되지 않도록 marker·snapshot 수명주기를 경계 단위로
  멱등화했다.
- **슬롯 pm_state 자동 생성** — slot 모드 첫 세션이 연속성 앵커 없이 뜨던 구멍을 막았다(task 축의
  `ensure_task_pm_state` 와 동형).
- **위임 프롬프트 상수 2종** — 클래스 전수 열거(보고된 형상뿐 아니라 같은 클래스의 형제 경로를
  모두 열거)와 역방향 확인(고침이 반대 방향 결함을 만들지 않았는지) 의무를 프롬프트 템플릿에
  박아 PM 의 기억 의존을 없앴다.
- **검토 루프 경량화 프로토콜** — wave 중 전체 회귀 폐지(지정 회귀만·전체는 릴리즈 1회), 리뷰어
  보고서 원문 전달, 프롬프트 검증 근거 지정 의무를 방법론·스킬에 박아 출하. 엔진 판정으로는
  cold 재투입 거부(`--resume-from`)·preamble 회귀 범위·`--attach-raw` 를 배선했다.

### Fixed
- **격리 스냅샷 cwd 의 앵커 붕괴 2건** — 게이트 격리 스냅샷에서 실행한 `external_review` 가 PM 홈
  해소 실패 시 "repo 자기 앵커"로 강등돼 라운드 장부가 스냅샷 안에 기록되고 스냅샷 제거와 함께
  소멸하던 클래스, 같은 cwd 에서 `pm_delegate` 가 스냅샷 자신의 `local.conf` 로 폴백해
  `delegate_enabled=false` 로 위임이 거부되던 클래스를 닫았다. 자기-앵커 판정은 경로 관례
  (`<owner>/work/...`) 탈출구를 없애고 마커 기반으로 바꿨다.
- **opencode 전달 프롬프트가 sandbox 밖** — `pm_delegate`(위임 축)와 `external_review`(리뷰 축)
  모두 프롬프트 파일을 sandbox(`--dir`) 밖에 만들어 non-interactive auto-reject 로 cost=0
  무변경이 나던 경로를 닫았다. 전달 경로 조립·플랫폼 폴백을 경화하고, 준비한 fd 를 전달 경로까지
  결속해 TOCTOU(준비 후 sandbox rename + 동일 절대경로 치환)를 막는다.
- **diff 서킷브레이커의 타 티켓 diff 합산** — 여러 티켓이 같은 엔진 파일을 만지는 병렬 wave 에서
  `ticket_finish` 가 `touches` 경로의 미커밋 diff 전체를 자기 스코프로 재 완료가 교착하던 형상을
  티켓 귀속 측정으로 좁혔다.
- **external_review payload 에 기계 미러 포함** — 기계 미러 제외 술어가 측정에만 걸리고 전송
  payload 에는 안 걸려, 릴리즈 범위 리뷰가 런타임 산출물 삭제 diff 를 통째로 전송하던 구멍을
  닫았다(실측 57.4MB 중 99.1% 가 `.opencode/`).
- **prefix canonical case 비결정성·락 밖 TOCTOU** — 4소스 `set` 순회 첫 항목을 고르던 판정을
  결정적으로 바꾸고 fail-loud 화했으며, rename/merge 의 canonical 판정을 `board_lock` 안에서
  재검증한다.
- **domain covers 의 upstream 전용 경로가 downstream 에서 영구 unverifiable** — 판정이 repo 소유
  축을 읽도록 고쳐, 구조적으로 해소 불가능한 advisory 가 매 `lint` 마다 쌓여 실 finding 을 가리던
  형상을 없앴다.
- **핸드오프 dirty-tree 게이트의 draft 오탐** — board `.gitignore` 에 `tickets/.drafts/` 를 멱등
  보강해(엔진 자동) 핸드오프마다 `--ack-dirty` 를 요구하던 오탐을 소멸시켰다.
- **adopter#0 `.claude/` 훅 미등재 flavor** — 훅 9종이 어느 동기 채널에도 선언돼 있지 않아
  `pm-update` 가 rc=2 경고를 내고 자동 자기치유가 막히던 형상 부채를 정비했다.

### Changed
- **`.opencode/` 런타임 산출물 git 추적 해제** — 제품 repo 루트에 커밋돼 있던
  `.opencode/node_modules` 3,648 파일 + `package.json`·`package-lock.json` 을 untrack 하고
  (파일 삭제 아님) `templates/opencode/.opencode/.gitignore` 와 동형의 자기-은닉 `.gitignore` 를
  루트 `.opencode/` 에 신설했다.
- **커밋 규율 문서 보강** — `git commit -- <pathspec>` 은 staged 가 아니라 워킹트리 내용을
  커밋하므로 `git rm --cached` 류 index-only 변경이 조용히 무효화된다(실측 3,657건 untrack 증발).
  index-only 변경은 단독 stage 확인 후 bare commit 으로 싣도록 출하 스킬에 명시했다.
- **opencode ctx-guard 플러그인 코어 domain covers 편입** — 변경 시 dev 위임 domain 소환·stale
  판정이 발동한다.

## [1.7.3] - 2026-08-11

### 업그레이드 노트

- **compaction 경계 기계화는 어댑터 config 반영까지 해야 발화한다.** 엔진·훅 스크립트는
  `pm-update` 가 자동 현행화하지만 이벤트 배선은 하네스별 인스턴스 소유 config 에 있다 —
  claude 는 `.claude/settings.json` 의 PreCompact 배선을 수용해야 하고
  (`./pm-config.sh sync-adapter-config --accept .claude/settings.json` 또는 수동 병합),
  codex 는 세션에서 `/hooks` 로 새 훅 정의를 재승인해야 한다. opencode 는 플러그인 경로가
  엔진 동기라 추가 조치가 없다.
- **추가 리뷰어 코드 리뷰 라운드 상한 기본값이 3→2 로 내려갔다.** `local.conf` 에
  `review_rounds_max` 를 명시한 인스턴스는 영향 없다.
- (v1.7.2 이월 안내) **추가 리뷰어 구키 4종은 제거됐다 — 여전히 읽지 않는다.** 게이트
  `external_review_enabled` 와 노브 `external_review_round_limit`·`external_review_wave_budget`·
  `external_review_incomplete_round_limit` 가 대상이다. 구키만 있는 `local.conf` 는 추가
  리뷰어가 꺼진 상태이므로, 키 이름을 신키(`additional_reviewer_*`)로 직접 바꾸거나 opt-in
  질문(`board.py init`·`pm-update`)에 다시 답한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Added
- **compaction 경계 기계화 (3하네스)** — 컨텍스트 압축 경계의 보존·복구를 LLM 규율에서 기계
  실행으로 승격. 엔진 스냅샷 빌더(`pm_log.py snapshot` — 장부 직접 읽기·git 호출 0·3초/8,000자/
  24,000바이트 상한·포인터 중심)가 주입 텍스트를 단일 소유하고, 어댑터는 이벤트 바인딩+그대로
  주입만 한다: claude 는 PreCompact 훅 정식 등재 + marker-armed 1회 재주입, opencode 는
  `session.compacted` 관측 주입(payload 를 내구 marker 파일로 이중 적재해 `opencode run`
  one-shot 프로세스 경계에서도 유실 없음 — 세션 키 정규화·generation 소유 규율·1회 소비·
  전달 시점 소비 receipt 로 도달 관측·소비 경합은 GC-선행 순서로 무락 직렬화),
  codex 는 PreCompact/PostCompact 배선. 체크포인트 골격은 경계에서
  기계 생성하며(서사 채움은 PM 판단) 경계 dedup 은 UTC+UUID 식별자·경계별 pending 파일로
  archive/재시작/동시 경계에 안전하다. 정체성 해소는 cwd→lease → 활성 단일 task → solo/legacy
  3순위·해소 실패는 훅 경로만 무음 skip(수동 명령은 fail-loud).
- **raw 장부 kill 잔재 수동 마감 CLI** — `pm_delegate.py raw close <RECORD-ID> ...`.
  kill/비정상 종료로 미마감으로 남은 raw 레코드를 정식 마감한다(`rc=-1` 표기·`--note` 사유·
  살아있는 pid 는 거부하고 `--force` 로만 우회). 장부 부기 전용이며 프로세스·raw 파일은
  건드리지 않는다. pid 생존 판정은 공용 seam `pm_relay.pid_is_alive` 로 승격 — Windows 는
  `OpenProcess` 조회(비파괴·접근 거부는 생존 보수 판정)이며 worktree 회수와 같은 판정을 쓴다.

### Fixed
- **핸드오프 dirty-tree 게이트의 서브모듈 미탐지 폐쇄** — 게이트가 등록 서브모듈 working tree
  내부의 미커밋 잔여와 gitlink pin 미갱신을 상위 기준 상대경로로 열거·차단한다.
  `.gitmodules` 의 `ignore = all` 설정에서도 발동하며(게이트 판정에 한해
  `--ignore-submodules=none` override) 미초기화 서브모듈은 제외한다. `git submodule status`
  파싱은 공용 파서로 단일화해 공백 경로·충돌(`U`) 행을 손실 없이 보존한다.

### Changed
- **추가 리뷰어 코드 리뷰 라운드 상한 기본값 3→2** — `review_rounds_max` 기본값만 이동.
  `local.conf` 에 값을 명시한 인스턴스는 영향 없다. 근거는 장부 실측: 관측된 3라운드째는
  전부(8/8) 반려였고, green 종결 6건 중 5건이 2R(나머지 1건은 명세 빈틈 병리의 6R)였다.
- **must-fix 잔여 처분 표기 "이관" → "재설계"** — 상한 도달의 출구가 재설계·티켓 분할임을
  표기에 반영. CLI 메시지·도움말·문서 표기만 바뀌며 장부 JSON 스키마·`--into` 플래그·판정
  로직은 무변경(기존 장부 기록도 새 표기로 렌더된다).

## [1.7.2] - 2026-08-10

### 업그레이드 노트

- **추가 리뷰어 구키 4종이 제거됐다.** v1.7.0 개칭이 예고한 유예가 끝났다. 게이트
  `external_review_enabled` 와 노브 `external_review_round_limit`·`external_review_wave_budget`·
  `external_review_incomplete_round_limit` 는 **더 이상 읽지 않는다**. 구키만 있는 `local.conf` 는
  추가 리뷰어가 꺼진 상태(노브는 엔진 기본값)이고, 그 사실은 안내 1줄로 알린다 — 값을 대신 읽어
  주지는 않되 침묵하지도 않는다. 이주는 둘 중 하나다: `local.conf` 의 키 이름을 신키
  (`additional_reviewer_*`)로 직접 바꾸거나, 구키만 남은 conf 를 미결정으로 보고 다시 묻는 opt-in
  질문(`board.py init`·`pm-update`)에 답한다(그 답이 신키로 기록된다). 엔진은 인스턴스 소유인
  `local.conf` 를 대신 고쳐 쓰지 않으므로 구키 줄 자체는 남는다(무해·직접 지운다). 두 키가 함께
  있으면 종전대로 신키가 이긴다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **릴리즈에 must-fix 잔여 기계 차단이 생겼다.** `livegate record`/`check` 가 추가 리뷰어 라운드
  장부에서 미처분 must-fix 잔여를 발견하면 릴리즈를 rc 1 로 차단한다(우회 플래그 없음). 상한으로
  종결된 게이트의 잔여는 `external_review.py --resolve-gate <게이트> --into <후속 티켓>`(그 티켓
  done 이 조건) 또는 `--fixed <근거 게이트>` 로 처분을 선언한다. `PM_SKIP_LIVE_GATE=1` 긴급 우회도
  현행 잔여-무 표식(`clear`)이 있어야 동작한다.

### Added
- **must-fix 잔여 처분 장부** — `--resolve-gate` 처분 표면(이관·해소·라운드 결속·근거 재검증·
  미상 잔여 fail-closed)과 `--rounds-report` 처분 열.
- **인스턴스 소유 파일 델타 요약** — pm-update 종료 시 진입문서(CLAUDE.md/AGENTS.md)·config 류의
  템플릿 세대 변경을 파일별 1줄(+처방)로 요약한다(무변경 무출력·`--check` 동일 노출·1회성 계약
  명문화 — durable 백스톱은 백업/git).
- **`pm-import` 기본 하네스 `all`** — 무인자 import 가 등록 어댑터 전체(claude·codex·opencode)를
  설치한다(단일/조합 명시 지정은 종전대로).

### Changed
- **훅 세트 강등 경로 하드닝** — 구세대 상류를 물린 업데이트에서도 그 세대가 제공하던 보호(원자
  write·부분 전파 거부)를 유지하고, 세대 선언은 해시한 bytes 를 직접 로드해 결속하며, 조회 축
  강등은 사유를 표면화한다.
- **local.conf 온보딩 위생** — 예시/안내 블록 append 가 활성 키를 인지해 모순 블록을 붙이지 않고
  멱등이 됐다(기존 conf 는 소급 수정하지 않는다).

## [1.7.1] - 2026-08-10

### 업그레이드 노트

- **핸드오프에 dirty-tree 게이트가 생겼다.** `/pm-handoff` 는 이제 첫 단계([0/7])에서 PM 홈과
  활성 worktree 전수의 미커밋 잔여(tracked 수정 ∪ untracked-unignored·gitignored 제외)를 보고,
  잔여가 있으면 어떤 파일도 건드리기 전에 rc 1 로 차단한다. 정상 해소는 세션 산출을 먼저
  커밋하는 것이고, 불가피하면 `--ack-dirty "<사유>"` 로 명시 통과한다(사유는 handoff entry 에
  박제·개행은 공백으로 평탄화). 커밋이 하나도 없는 트리는 untracked 만으로 판정하므로,
  **`pm-import --new` 직후에는 scaffold 를 먼저 커밋해야 첫 핸드오프가 통과한다**(ADOPT.md 0단계).
- **pm-update 가 full 동기 종료 시 rev 수렴을 검증한다.** 활성 stamped 사본에 rev 혼합이 남으면
  baseline 기록을 억제하고 rc 를 비영으로 낸다(재실행이 처방·`--paths`/dry-run 은 대상 아님).
  혼합 `--from` 트리를 복사하고도 성공으로 끝나던 침묵이 사라진다.

### Added
- **어댑터 훅 세트 세대 게이트** — "신 config(채택자 소유 settings 류) + 구 훅 드라이버" 조합이
  훅 오류로 도구 호출을 전면 차단하던 락아웃 클래스를 기계로 폐쇄한다. pm-update 동기 경로가
  훅 세트 세대 정합을 판정해 미지원 조합을 rc 1 + 처방으로 막고(settings 가 실제 참조하는 스크립트
  경로 기준·전 하네스), `pm-config sync-adapter-config` 에 세트 수용 `--accept-all` 이 생겼다
  (엔진 파일 선행 검사·원장으로 무편집이 확인된 파일만 세트 수용·`edited`/`unrecorded` 는 단건
  `--accept` 처방·판정한 선언/template 스냅샷을 이중 해시로 쓰기 직전 재검증). `--paths` 부분
  전파가 훅 결합 묶음을 반쪽만 갱신하는 것도 거부한다. 훅 세트 파일 write 는 원자 교체다.
- **훅 세트 세대 판정의 상류-통일** — 세대 선언을 소비하는 게이트 전부가 설치본이 아니라 **상류
  세대 선언**으로 판정한다(단일 해소 seam·조회 축은 loud 폴백 관대·mutation 축은 fail-closed).
  직전 세대 엔진과의 혼재는 3단 강등 사다리(신 API → 구 blocker → loud 통과)로 호환한다.
- **pm-update mid-sync skew 흡수** — 동기 실행 중 per-file 순차 write 가 만드는 rev 혼합(정상
  과도 상태)을 내부 중첩 로드가 skew 오류로 fail-loud 해 동기 자신이 죽던 클래스를 폐쇄한다.
  등록 사유 장부로 흡수하고(흡수 밖 호출은 AST 전수 감사로 기계 박제), 종료 시 수렴 검증이 짝이다.
  동기 실행 밖에서는 skew 가 종전대로 fail-loud 다.
- **핸드오프 dirty-tree 게이트** — 위 업그레이드 노트 참조. 비대화 자동 실행용 예약 플래그
  `--auto-trigger`(차단 대신 loud 경고+사유 자동 박제) 포함.

### Fixed
- **인스턴스 로컬 산출물 커밋 유입 차단** — 추가 리뷰어의 인스턴스 overlay
  `.project_manager/review_context.local.md` 를 엔진 `.project_manager/.gitignore` 에 정확명
  등재했다(3타깃 전파·`*.local.md` 와일드카드 아님 — 채택자 소유 wiki 로컬 문서는 계속 추적 가능).

## [1.7.0] - 2026-08-09

### 업그레이드 노트

- **`board.py init` 을 한 번 재실행하라.** 이 릴리스는 pre-push 훅 본문 세대를 올린다. 구버전
  훅이 깔린 트리에서는 `regression run`/`check` 가 rc 1 로 막히고(따라서 push 도 막힌다) 안내
  1줄이 이 명령을 지목한다. 엔진은 설치된 훅을 **자동으로 고치지 않는다** — 재설치 1회가 유일한
  처방이고, 그 뒤로는 종전대로 동작한다. 훅을 손으로 고쳐 쓰던 트리는 재설치가 그 편집을 덮으므로,
  추가 단계가 필요하면 재설치 후 다시 얹는다(Windows 는 `py -3 .project_manager/tools/board.py init`).

### Changed
- **추가 리뷰어 게이트 키 개칭** — opt-in 게이트 키가 `external_review_enabled` 에서
  **`additional_reviewer_enabled`** 로 바뀐다. 구키는 이번 릴리즈까지만 fallback 으로 읽히고
  (신키 우선·둘 다 있으면 신키가 이긴다), 구키로 결정이 잡히면 `external_review.py` 게이트와
  `pm_update` 온보딩이 각각 deprecation 경고 1줄을 낸다. **구키는 다음 릴리즈에서 제거한다** —
  채택자는 `local.conf` 의 `external_review_enabled` 를 `additional_reviewer_enabled` 로 바꾼다
  (자동 마이그레이션 없음·엔진은 채택자 conf 를 고쳐 쓰지 않는다). 새 온보딩(`board.py init`·
  `pm_update` 첫 opt-in)은 신키만 기록한다. 모듈 파일명(`external_review.py`)·raw 파일 접두
  (`external_review_*.txt`)·레거시 타임아웃 키(`external_review_timeout`·
  `external_review_idle_timeout`·`external_review_progress_signal`)는 그대로다 — 파일명 변경은
  채택자 PM 홈에 구 사본이 남는 형상(동기는 상류 부재 파일을 지우지 않는다)을 만들고, raw 접두는
  이미 기록된 감사물의 이름이다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **라운드/wave 노브 키 개칭** — 게이트 키와 같은 규칙으로 예산 노브 3종도 개칭한다:
  `external_review_round_limit` → **`additional_reviewer_round_limit`**,
  `external_review_wave_budget` → **`additional_reviewer_wave_budget`**,
  `external_review_incomplete_round_limit` →
  **`additional_reviewer_incomplete_round_limit`**. 값 의미와 기본값(판정 4 · 미완 2 · wave 24)은
  바뀌지 않고 이름만 바뀐다. 구키는 이번 릴리즈까지만 fallback 으로 읽히고(신키 우선·둘 다 값이
  있으면 신키가 이긴다), 구키가 값을 공급하면 `external_review.py` 가 키마다 deprecation 경고
  1줄을 낸다(게이트 안내와 같은 깔때기·미리보기에서도 같은 자리).
  **세 구키는 다음 릴리즈에서 제거한다** — 채택자는 `local.conf` 의 세 키 접두를
  `additional_reviewer_` 로 바꾼다(자동 마이그레이션 없음).
- **추가 리뷰어(additional reviewer) 온보딩·명명** — 사람이 부르는 역할 이름을 "외부 리뷰어"에서
  **추가 리뷰어**로 바꾼다. `external_review.py`·`external_review_*` 등 모듈/raw 기계 식별자와
  외부 전송·격리·과금 축의 이름은 그대로다(게이트 키만 위 항목대로 개칭·자동 마이그레이션 0).
  `board.py init`·`pm_update` 의 첫 opt-in 은 **1회만** 묻고, "예" 면 `local.conf` 에
  `additional_reviewer_enabled=true` + `additional_reviewer.harness=codex` +
  `additional_reviewer.model=gpt-5.6-sol` + `additional_reviewer.reasoning=max` 4키를 원자적으로
  기록한다 — `reviewer_cmd` 는 만들지 않는다. 파일 변경이 0인 수렴 `pm_update` 도 같은 첫 opt-in 을
  배달한다. 이미 결정(true/false)이 있으면 다시 묻지 않고, 활성 플래그만 빠진 채 유효한 구조적
  튜플·레거시 `reviewer_cmd`가 있으면 대상은 byte 그대로 두고 플래그만 기록한다. 부분 튜플·이중
  대상은 쓰기 전에 크게 알리고, stdin EOF는 거절로 박제하지 않는다. 질문에 답하는 동안 다른
  writer가 설정을 바꿔도 commit 시점에 잠금 안에서 최신 파일을 다시 판정해 새 결정·대상을
  덮어쓰거나 이중 대상을 만들지 않는다. 재-import/update 는 커스텀 `additional_reviewer.*` 를
  포함해 무손실 왕복한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **추가 리뷰어 구조화 실행 계약** — `additional_reviewer.{harness,model,reasoning}` 을 원자적으로
  해소해 codex·claude·opencode 세 하네스를 같은 공용 relay seam 으로 실행한다. 기본값은
  `codex/gpt-5.6-sol/max`, 역할은 하네스별 고정 read-only `code-reviewer`이고, 실행 전 stderr·
  dry-run·raw 장부가 동일한 하네스/모델/reasoning/명령 출처를 기록한다. 일부만 설정된 튜플,
  구조화 튜플과 레거시 `reviewer_cmd` 동시 설정, 미지원 값은 격리·예약·송신 전에 fail-loud한다.
  레거시 명령은 호환 실행하되 모델을 추측하지 않고 `unpinned-model` 로 크게 표시한다.
- **비용 재승인 폐지** — `additional_reviewer_enabled=true` 는 설정된 외부 전송과 통상 과금에 대한
  지속 동의다. 카드·매뉴얼·플레이북이 리뷰마다·라운드 상한 재개마다 사용자에게 비용을 다시 묻던
  문구를 걷어낸다. 라운드/wave 상한은 기계적 anti-loop 정지로 남으며, PM 은 `--rounds-report` 를
  읽고 **같은 scope 의 정상 수렴이면 자율로 ack** 한다. 사용자에게 올리는 경우는 진짜 미수렴,
  중대한 scope 확대, 그 밖의 독립적 사용자 게이트 사유다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Changed
- **상태 변경 명령의 티켓 조회 엄격화** — `claim`·`complete`·`block`·`unclaim`·`unblock`·
  `promote` 등 상태를 바꾸는 명령은 티켓 ID 정확 일치만 허용한다. 종전에는 정확 일치가 없으면
  비슷한 번호(`T-NNNN-001` 류)로 폴백해 경고 후 이동까지 진행했으나, 이제 rc 실패 + 근접 후보
  안내로 차단한다(없는 번호 오입력이 엉뚱한 티켓을 옮기는 경로 폐쇄). 읽기 조회(`show`)의
  폴백은 경고와 함께 유지된다.

### Added
- **리뷰 수렴 게이트** — 코드 리뷰 라운드에 기계 수렴 판정을 건다. 기록 라운드가 상한
  (`review_rounds_max`, 기본 3)에 닿으면 must-fix 잔존과 무관하게 차단하고(사유 라벨
  `cap-unresolved`/`cap-reached` 분기), 직전 라운드 대비 must_fix 가 늘면(발산) 상한 전에 조기
  차단한다. `--ack-rounds` 라운드 연장은 폐지(rc=1 거부·재개 승인이 남은 축은 wave 예산 하나).
  잔여 fix 확인 전용 `--confirm-fix` 를 게이트당 1회 허용(수렴 축 예외일 뿐 전송 횟수 상한은
  열지 않음·전송 0 실행은 count/wave/confirm 3축 동일 조건 환불). 비수렴의 출구는 라운드 연장이
  아니라 티켓 재설계·분할이다. **`--confirm-fix` 는 `--gate` 필수** — 게이트 없는 확인 전용
  라운드는 경고 후 실행하던 것을 **전송 전 rc 1 거부**로 바꾼다(1회 제한을 세는 장부 항목이
  게이트 단위라, 게이트가 없으면 예외가 회계 밖에서 무한히 열린다). 조회면(`--rounds-report`)도
  같은 규율로 거부한다.
- **diff 서킷브레이커** — 티켓 touches 스코프 diff 총량이 estimate 상한(small 300 / medium
  1,000 / large 2,500줄·`diff_cap.<estimate>` conf override)을 넘으면 리뷰 진입과 완료 부기를
  차단하고 분할·재설계를 요구한다. dry-run·비활성·egress 차단 경로는 검사 밖(전송 확정 구간만).
  **측정 의미 = 손작업 스코프**: `templates/<타깃>/.project_manager/` 아래 pm_update 관리
  mirror 는 합산에서 제외한다(기계 산출을 손작업과 같은 가중으로 세면 구현 스코프가 출하 타깃
  수만큼 부풀어 분할이 불필요한 티켓이 막힌다 — mirror 정합은 drift-0 가드가 따로 지킨다).
  선언이 넓은 접두(`templates/`)여도 제외가 성립하고, 차단 안내가 그 측정 의미를 한 줄로 밝힌다.
  PM 홈 좌표 touches(`work/<repo>_<N>/…`)는 측정 트리 좌표로 정규화한 뒤 잰다(정규화 불능은
  경고 1줄 + 가드 off).
- **회귀 스테이징** — 활성 리뷰 사이클(라운드 장부 미종결 ∧ 티켓 claimed) 중 FULL 회귀 요청을
  touches targeted 로 강등하고, FULL 은 `--final`·pre-push 게이트 경로에서만 돈다.
- **구버전 pre-push 훅 차단** — 설치된 pre-push 훅이 현행 세대(`# pm-hook-rev` 스탬프 포함
  정확일치)가 아니면 `regression run`/`check` 가 **회귀를 돌리지 않고 rc 1 로 막고** `board.py init`
  재실행을 안내한다(구세대 registry 일치·엔진이 모르는 본문 둘 다 차단·읽지 못한 훅도 차단).
  훅 미설치·서명 없는 남의 훅·훅 위치 해소 불가는 무영향이고, 경로 해소는 순수 파이썬이라 회귀
  진입에 subprocess 를 더하지 않는다. 자동 교체(자기치유)는 **넣지 않는다** — 교체는 다음 실행부터
  유효한데 지금 도는 것은 교체 전 본문이라, 그 창을 닫으려면 "방금 고쳤다"를 별 프로세스에 전달할
  공유 상태가 필요하고 그 상태가 경합·TTL·표식 탈취를 낳는다. 차단은 상태가 필요 없다.
- **untracked 신규 파일이 리뷰 diff·측정에 포함된다** — `git diff` 는 아직 add 되지 않은 파일을
  보지 못해 리뷰가 새 파일을 못 보고 diff 서킷브레이커가 그것을 0 줄로 쟀다(완료 부기는 재고 나서
  stage 하는 순서라 대형 신규 파일이 상한을 그대로 통과했다). 이제 작업트리 단계('HEAD')의 추출과
  `--numstat` 측정이 `.gitignore` 밖 신규 파일을 함께 센다. 포함 방식은 `--no-index` 라 **index 를
  건드리지 않는다**(선언 pathspec 밖·기계 mirror 제외는 종전 규칙 그대로).
- **티켓 설계 단계** — frontmatter `design: required|done|waived:<사유>|n/a`(estimate=large 는
  required 기본)와 본문 `## 설계` 절(경계 실측·불변식·표면 상한·테스트 전략)을 신설. required
  티켓은 설계 검토 완료(`done`/`waived`) 전까지 promote 가 거부하고 claim 게이트가 막는다.
  형식 위반 값은 fail-loud(오타로 게이트가 조용히 꺼지지 않음)·구티켓(필드 부재)은 n/a 하위호환.
- **위임 세션 재사용** — pm_delegate claude 위임에 `--resume-from`(같은 티켓·role·harness 의
  rc=0 최신 레코드 결정 선택)을 배선. 재사용 성공 판정은 회신 session_id 일치이며 실패·미일치는
  fresh + full payload 로 loud 폴백한다(인프라 실패는 재실행이 아니라 폴백 축). 위임 레코드에
  session_id·usage 4필드(input/cache_creation/cache_read/output)·must_fix 항목·base rev 를
  구조화 저장해 비용 원장을 겸한다. codex/opencode 축은 미지원 선언(fresh+loud).
- **완료·위임 부기 게이트** — board complete 가 `## 완료 조건` 체크박스를 판정한다(`- [x]` 와
  `- [>] <원문> (이월: <사유·귀속>)` 만 통과·done 소급 없음). 위임 실패 종료는 단일 깔때기로
  수렴해 전 경로에 무음 대체 금지 안내를 부착한다(다른 하네스/모델 자동 대체 없음·명시 fallback
  tuple 의 1단 loud 폴백만 예외). 핸드오프는 미마감 raw 표면화("이 장부 기준" 명시)·미push
  commit 경고·push 체크 단계를 얻는다.

### Added
- **Codex `$pm-review` egress 승격 카드** — codex 전역 `network_access=false` 를 유지한 채 추가
  리뷰어 실 전송만 `exec_command` 건별 승격으로 실행하는 자족 절차를 codex 판 카드에 싣는다
  (sandbox `--dry-run` 선행 → `sandbox_permissions="require_escalated"` +
  `--codex-egress-escalated` attestation → 최초 승인은 좁은
  `prefix_rule=["python3", ".project_manager/tools/external_review.py"]`). codex flavor manifest 에
  file override 를 등재해 이 판이 공유 카드 렌더로 덮이지 않게 한다. claude/opencode 카드에는 이
  Codex tool metadata 를 싣지 않는다.

### Fixed
- **진행 중 리뷰 예약의 만료 기준이 레코드 자신의 것이다** — 예약 시점에 그 실행의 벽시계 백스톱을
  `deadline` 으로 장부에 새기고, 수렴 상한의 미마감 예약 합산이 그 값을 본다. 종전에는 지금 장부를
  읽는 호출자의 timeout 으로 재서, 짧은 timeout 의 후속 호출이 긴 timeout 으로 **실제 돌고 있는**
  라운드를 stale 로 접고 상한을 넘겨 예약할 수 있었다. `deadline` 없는 구레코드는 종전 규칙(호출자
  백스톱 대 `started_at`)으로 보수 합산한다.
- **위임 장부의 must-fix "없음" 정규화** — 통과 회신의 `- 없음` 표기가 `must_fix_items=["없음"]` 로
  박제돼 다음 라운드 delta 가 그것을 고칠 지적으로 되읽던 것을, 추가 리뷰 경로와 같은 술어로
  정규화해 항목 0 건으로 남긴다.
- **`local.conf` writer 단일 직렬화 경계** — `board init`·두 opt-in·`pm_config`·`pm_import`·
  `pm_update`의 모든 read-plan-write/postcondition 구간이 `file_lock.py`가 소유한 같은
  `local-conf.lock`을 사용한다. 질문 대기 중의 stale snapshot뿐 아니라 서로 다른 writer 종류가
  동시에 실행될 때도 마지막 writer가 앞선 결정을 되돌리지 않는다. 재-import 보존값 계산과
  `upstream`의 URL/path 형상 판정도 write와 같은 임계 구간에서 다시 계산해 stale 계획에 의한
  결정 롤백·`upstream_seen_rev` 오기록/누락을 막는다. 새 conf-lock API가 없는 구세대
  `file_lock.py` 사본은 같은 락 파일을 구 API로 잡고, 일반 형제 모듈 손상만 기존 복구 채널대로
  무락 진행한다. 표시된 engine-rev skew는 사용자 입력 오류로 번역하거나 삼키지 않고 다시 올린다.
- **Codex cross-harness egress 승인 브리지** — `workspace-write` 샌드박스의
  `network_access=false`를 유지한 채 `pm_delegate → claude/opencode/codex CLI` 실위임과
  `external_review.py` 추가 리뷰 송신만 각 진입점의 Codex `exec_command` 건별 승격으로 실행한다.
  dry-run이 승격 필요를 미리 표시하고, 실행은 `sandbox_permissions=require_escalated` +
  `--codex-egress-escalated` attestation을 동반한다. 최초 승인은 각 진입점 전용의 좁은 reusable
  prefix로 기억하고, `delegate_enabled=true`·`additional_reviewer_enabled=true`인 후속 호출은 과금을
  재질문하지 않는다. 일반 sandbox 오호출은 원격 CLI
  재시도·raw 예약·과금 전 fail-loud하고, 거절/실패를 native GPT로 무음
  대체하지 않는다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **추가 리뷰 실행 예산의 실제 spawn 판정** — 자식 프로세스 생성 여부를 실제 `Popen` 경계에서
  실행 전체에 걸쳐 단조롭게 기록한다. 첫 재시도에서 이미 자식이 생겼다면 뒤 재시도의 launch
  실패로 리뷰 예산을 환불하지 않는다. NUL argv·명령 부재·권한·경로 형상처럼 확실한 pre-child
  거절만 무실행으로 판정해 예약을 되돌리고, 그 밖의 모호한 `Popen` 예외와 `Popen` 반환 뒤
  초기화·정리 실패는 보수적으로 실행됨으로 유지한다.
- **worktree git mutation 앵커 가드** — Claude `PreToolUse(Bash)`와 OpenCode hook이 실제 shell
  command word·cwd 전이·Git pathspec을 중앙 `board.py` 판정으로 해석한다. PM 홈의 공유 경로
  mutation은 deny, 활성 canonical slot 안의 명백한 mutation은 allow, 동적 wrapper·복잡 shell·
  symlink alias처럼 확정할 수 없는 호출은 warn으로 내려 false-deny 없이 가시화한다. Codex는
  동등한 raw exec hook이 없는 capability gap을 문서화한다.
- **handoff 재실행의 멱등성과 선행 lease 해제** — 같은 task/session의 handoff는 기계 구획을
  append하지 않고 원자 갱신하며, 다른 세션·task는 새 구획을 만든다. 공유 log lock 안에서
  read-plan-write를 끝내고 CRLF·혼합 newline과 기계 구획 밖 PM 본문 바이트를 보존한다. task 모드는
  log 기록 전에 lease PID를 0으로 내려야 하며 해제 실패·부재·예외는 rc1로 중단한다.
- **어댑터 config 완료 게이트** — `pm_update`가 managed adapter 후보의 비교·수렴·baseline 장부를
  completion gate로 판정한다. 후보가 전혀 없는 partial recovery만 vacuous green이고, managed
  목적지가 있는데 source template·판정 채널이 없거나 baseline이 미수렴이면 엔진 파일 변경 0이어도
  rc1이다. 실제 구 updater RUN1→신 updater RUN2→accept/backfill→green 경로를 E2E로 고정한다.
- **PM 홈 상시-red 예시 CI 제거** — claude_code 채택자 템플릿에서
  `.github/workflows/regression.yml`을 제거하고 세 하네스 manifest 모두 GitHub workflow를
  비출하한다. 표준 PM 홈에는 제품 테스트가 없어 기존 예시가 push마다 pytest exit 5와 실패 메일을
  만들었다. 기존 채택자는 자동 삭제 대상이 아니므로 `git rm .github/workflows/regression.yml`로
  제거한다(프로젝트가 직접 만든 workflow라면 삭제하지 말고 프로젝트 CI로 소유권을 전환).

### Docs
- pm-handoff 스킬 §사용 시점 구 계약 잔재 교정 — 컨텍스트 임계·wave 자연 종료를 핸드오프
  트리거로 나열하던 서술 제거. 핸드오프는 사용자 명시 종료 신호가 유일한 트리거이며, 컨텍스트
  임계 규약은 checkpoint 박제 후 컴팩션 통과다(compaction-native·PM 실오판 재현 사례로 확인).

## [1.6.2] - 2026-08-07

**채택자 제보 흡수 릴리스.** 실채택자(v1.4.5→v1.6.0→v1.6.1 2단계 채택)가 제보한 엔진 결함
10건과 잔여 로컬 편차를 전량 흡수한다. 관통 성질: add-harness guest 형상과 기존-채택자
업그레이드 경로가 처음으로 상설 게이트에 들어온다 — 지금까지의 fresh-adopter 게이트는 신규
설치만 검증해 이 클래스(디스크의 옛 데이터·guest 이력·치환 상태에서만 발현)를 구조적으로
못 봤다.

### 업그레이드 노트

- **`pm-update` 를 2회 실행하라.** 엔진 자신이 `pm-update` 로 배달되므로 1회차는 구 엔진
  코드로 돈다. v1.5.0~v1.6.1 엔진 + v1.5.0 이전 add-harness 이력(옛 세대 guest 마커) 조합은
  1회차에서 guest 절이 소실될 수 있다 — 신 엔진이 착지한 2회차가 마커 세대를 수렴시키고,
  절이 이미 소실된 채택자는 `미등재 flavor 파일 관측` 경고의 안내대로
  `./pm-config.sh add-harness <harness>` 재실행으로 복구한 뒤 다시 동기하면 된다(이후에는
  guest 절 파생 백필이 자동 유지한다).
- guest 하네스의 **엔진 파일**(codex `pm_orch_codex.py` 등)이 이번 릴리스부터 `pm-update` 로
  갱신된다. 종전에 동결됐던 파일은 첫 동기에서 상류와 수렴한다.

### Added
- **guest 하네스 엔진 파일 동기 채널** — guest 절 행이 2채널로 갈린다: `@render` 행(어댑터
  렌더물)은 종전대로 add-harness refresh 소유, 비-`@render` `@source` 행(guest 엔진 파일)은
  `pm-update` 가 byte-copy 로 갱신한다. add-harness 가 엔진 행을 함께 등재하고(복사 술어 공유로
  "등재 ⊆ 복사" 구조 보장), 구세대 절 채택자는 flavor 배타 경로 증거로 엔진 행을 파생·등재해
  (receipt 미사용·cross-ns 오탐 가드·지속화) 재실행 없이 동결이 풀린다. instance-owned
  config(`config.toml`·`hooks.json`·`settings.json`·`opencode.jsonc`)는 종전대로 불가침.
- **상류 은퇴 파일 보고 채널** — 상류에서 삭제·rename 된 등재 파일을 apply·dry-run·`--changes`
  가 같은 문구로 보고한다(0건 침묵·상한 20 + "외 N건"). 삭제 전파는 하지 않는다 — 채택자
  로컬 자산(등재 디렉토리 안 공존 스킬 등)과 은퇴 파일을 기계로 구분할 수 없어, 보고가 정확한
  하한이다. `--changes` 는 D/R 을 `removed_upstream` 버킷으로 분리해 "이번 동기가 받는 것"
  오보를 제거한다.
- **회귀 FULL 게이트 수집 하한** — `local.conf` 의 `regression_min_collected`(기본 0=off)가
  rc0 부분수집 false-green 을 `partial-collection` 으로, 하한 활성 + 요약 검증 불가를
  `unverified-collection` 으로 강등한다. 플래그에 수집수·하한·conf 앵커를 기록해 green 재사용을
  산술로 무효화하고, pre-push 재실행이 같은 앵커를 승계한다. 러너는 Popen tee(실시간 출력 +
  캡처)·요약 파싱은 stdout 단독·요약행 문법 완전 일치(앞뒤 로그 오염 양방향 차단).
- **외부리뷰 라운드 산출 장부 + wave 총예산** — 게이트별 라운드 이력(`rounds`: 판정·must-fix
  수·예약 순번)과 세션 총예산(`external_review_wave_budget` 기본 24·`--ack-wave` 승인 재개·
  세대 토큰으로 환불 우회 차단)을 장부에 신설, `--rounds-report` read-only 조회면을 제공한다.
  게이트 상한과 wave 예산은 독립 축이며 승인은 서로를 열지 않는다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **업그레이드-채택자 e2e 게이트** — 구세대 디스크 상태(옛 guest 마커·add-harness 이력·치환
  상태)를 주입한 채택자 픽스처로 guest 절 생존·동결 가시성·토큰 안정성·reconcile 절차 안전을
  상설 검증한다(v1.6.1 엔진이면 red 인 민감도 실증 포함).
- **render 토큰 소유권 가드** — bare 등재 파일의 운영 토큰은 소비 시점 치환 선언
  (`CONSUMPTION_TIME_TOKENS`)이 없는 한 출하를 차단한다(파일×토큰 매트릭스·소비처 실재 검증).
- **사설참조 strip 데이터-리터럴 표식** — `# pm:data-literal`(라인·begin/end 블록) 주석이 붙은
  wire 리터럴은 스트립·재유입 가드가 함께 제외한다(tokenize.COMMENT 한정·완전 포함 면제·경계
  걸침 탐지 유지·near-miss 오탈자 loud·lone CR 거부). v1.5.0 스트립 사고(guest 마커 절단)의
  클래스 폐쇄.
- **pytest 요약 파서 공용 seam** — 요약행 파서 5벌(첫-매칭)을 board.py 의 끝-탐색·문법 완전
  일치 파서로 승격하고 livegate·bootstrap·ticket_finish·handoff 가 공동 소비한다(사본 재유입
  가드·seam 부재 fail-closed + 재동기 안내).

### Fixed
- **guest 절 마커 세대 단절** — v1.5.0 스트립이 마커 리터럴을 바꿔 기존 채택자 guest 절이
  다음 동기에서 경고 없이 소실되던 것(제보 1항목). 읽기는 세대 집합 인식·쓰기는 현행 단일,
  dest 재부착 직전 1회 치환한다(다중-harness manifest 분기 포함).
- **frozen 경고 도달 불가** — guest 소유 경로 하나로 flavor evidence 전체를 버리던 판정을
  개별 제외로 교체(제보 2항목의 탐지 축). add-harness 채택자에서 경고가 실제로 발화하고, 문구를
  `미등재 flavor 파일 관측` + facade 복구 안내로 재조준했다.
- **`--changes` 미리보기 과소·과대보고** — `@source` 매핑 무시(제보 5항목·출하 manifest 7/7
  오분류)와 self-heal 미반영(제보 6항목)을 좌표·owner-우선순위 판정과 계획 manifest 헬퍼 공동
  소비로 폐쇄(미리보기 == 계획·legacy_preserved·guest 채널 상속 포함).
- **render 토큰 진동** — 치환된 `wiki/README.md` 를 pm-update 가 토큰으로 되돌리던 반쪽(제보
  7항목)을 토큰 소유권 정리로 폐쇄: README 는 토큰 제거(하네스 중립 문구), `pm_state.template`·
  `domain/_template` 의 `{{DATE}}` 는 소비 시점(생성기) 치환 소유로 이관.
- **테스트 훅 러너 하드코딩** — `run_tests_hook.sh` 가 `local.conf` 의 `test_cmd` 를 해소해
  실행한다(제보 9항목·`sh -c` 신선 셸로 엔진과 실행 의미 통일·rc 보존·읽기실패와 미지정 구분·
  파서 동형성 26종 배터리 잠금).
- **opencode.jsonc 빈 배열 삽입 파손** — 빈(또는 비-문자열 원소) `instructions` 배열에 후행
  쉼표를 만들던 삽입을 비-공백 바디 판정으로 교체(제보 10항목).

### Docs
- pm-update 스킬 §3: 선-cp 는 기본 생략(self-prop 채택자는 엔진이 manifest 도달을 처리·실측
  게이트 박제), cp 사용 시 guest 절 소실과 복구 절차를 명시. README 의 guest 채널·frozen 경고
  서술 현행화. `docs/manual-import.md`·`docs/placeholders.md` 의 치환 판정을 엔진과 등가로 교정.

## [1.6.1] - 2026-08-06

**게이트·렌더 견고화 릴리스.** 관통 성질: 조용한 degrade 의 잔여 클래스를 기계 판정으로 폐쇄한다 —
표기 렌더의 미소유 파일 skip, 외부리뷰 앵커의 자기잠김/무필터 송신, 락 사본 중복, 현재-진실
문서의 히스토리 누적이 각각 가드·fail-loud·공용 seam 으로 닫힌다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Added
- **공용 파일락 seam** — `file_lock.py` 신설. board·pm_log·pm_relay·pm_handoff·worktree_pool·
  external_review 의 배타 파일락과 O_APPEND 원자 append 를 단일 구현(POSIX flock·Windows msvcrt·
  프리미티브 부재 시에만 무락 폴백)으로 수렴한다. 락 경로 규약·권한은 각 도구가 유지하며,
  플랫폼 락 분기의 재복제를 AST 가드가 차단한다(수렴 잔여 0 을 가드가 박제).
- **codex 외부 리뷰어 가시 범위 격리** — 게이트 리뷰어를 저장소 밖 tracked 거울(시크릿 denylist
  동일 적용) + 세션·이력 없는 임시 홈(인증만 선언 복제·projects/기능 테이블 scrub·경로 노출 성질
  자물쇠) + 최소 allowlist env 로 실행한다. 세션 전사·옛 raw 의 echo 오염은 회신 채널 한정 검출로
  판정을 전면 불명확 처리하고, 격리 실패는 기본 차단(`--allow-unisolated-reviewer` 탈출구)이다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **영속 설치 기록(install receipt)** — `pm_import`/`add-harness` 가 실제 성립한 하네스를
  `.project_manager/install.json`(git 추적)에 기록하고 표기 독자 판정이 기록을 1순위로 소비한다
  (부재 시 증거 추론 폴백·손상은 `.corrupt` 백업 후 재기록·미래 schema 읽기/쓰기 거부).
- **pm_update `--paths`** — 명시 경로만 전파하는 opt-in 스코프(등재 검증 선행·디렉토리 하위 오타
  rc=1·board 분리 리매핑·부분 전파는 baseline/마이그레이션 비발화).
- **라운드 장부 소유 PM 홈 앵커** — 외부리뷰 라운드 상한 장부가 diff 슬롯이 아닌 소유 PM 홈에
  쌓인다(스냅샷/새 worktree 로 상한이 리셋되던 창 폐쇄·기존 슬롯 장부는 1회 승계·차단 상태 유지). (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **worktree refresh `--onto` 명시 해소** — 명시 ref 를 그대로 해소한다(자동 origin 대체 제거·
  무인자 기본 경로는 origin 우선 유지·성공 메시지가 실측 ref/sha 표기).
- **codex 템플릿 wiki seed 대칭** — claude/opencode 와 동일한 13종 seed(architecture·status·
  decisions/ideas/specs 스캐폴드 등)를 codex 템플릿도 출하한다(README dangling 링크 0).
- **domain lint `history` 축** — 현재-진실 domain 페이지에 세션별 delta/변경 이력이 쌓이면
  advisory 로 검출한다(시점 스탬프 lead-in·라벨-only 헤딩·bold delta·never-block). 판정 기준은
  `wiki/domain/README.md` 가 문서화한다.
- **manifest 실재 일반화 가드** — 각 flavor manifest 의 비-`@source` 경로가 템플릿 트리에
  실재하는지 회귀로 단언한다(등록됐는데 안 실리는 클래스 기계 차단).
- **미등재 출하 wiki 원장 가드** — 출하되는 manifest 미소유 wiki relpath 집합을 원장으로 박제,
  신규 미등재 파일 유입 시 red.

### Changed
- **표기 렌더 폴백** — manifest 미소유 출하 wiki(인스턴스 seed)는 설치 하네스 전체 집합 표기로
  폴백 렌더한다(loud·조용한 skip 제거). add-harness/`--into` 도 기존 공유 문서·seed 를 복사 전
  계획 표시·중앙 백업·경로 안전 검증·개행 보존으로 재렌더하며(멱등), placeholder 치환·manual
  fill 은 이번 실행이 복사한 파일로 한정한다. 표기 독자 = dest 실설치 하네스 ∪ 이번 선택
  (`installed_harnesses` 단일 판정·`pm_update` 동일 소비).
- **외부리뷰 앵커 보안** — conf 소유자 강등 시 소유 PM 홈의 유효 범위·denylist 를 승계하고,
  승계 불가면 전송 전 차단한다(`--paths` 탈출구 유지). 소유자 conf 읽기 실패는 `--paths` 로도
  차단(fail-closed). 명시 앵커 실행은 선택-전 config 를 읽지 않는다(자기잠김 제거). diff 폭
  서술은 단일 표로 수렴하고 슬롯 소유 근거는 명시 base 로 한정한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **리뷰 raw 장부 앵커** — external_review 의 raw 기록이 diff 슬롯이 아니라 해소된 소유 PM 홈
  장부에 박제된다(`pm_delegate raw` 통합 조회 정합·슬롯 축적/오염 원천 제거).
- **gate_snapshot 정밀화** — 심링크 prefix-only 해소(false-red 제거), 출력 경로 거부를 git 공용
  디렉터리·타 등록 worktree 로 확장(prunable 등록 제외·`worktree prune` 처방), 성공 출력 검증
  파일 전량 열거, `worktree list -z` 파싱.
- **terminology 가드 변형 클래스 매칭** — 옛 표기 재유입 검사를 낱말 표기표+생성 정규식으로
  재편(구분자·대소문자·한영 혼용·합성어 경계). 항목별 변형 축 샘플·음역 선언을 메타 테스트로 강제.
- **failsoft 가드 self.<attr> 축** — 인스턴스 속성 바인딩 콜러블의 skew 전파를 스캐너가 추적,
  기존 fail-soft 가 삼키던 실 결함 4건을 재전파로 교정. 콘솔 worktree add 는 marked skew 시
  루프를 종료한다.
- **pm_import 쓰기 채널 TOCTOU 전면 폐쇄** — 복사·중앙 백업·재렌더·placeholder 치환·모델 해소·
  fill 전 채널의 dest IO 를 fd(`O_NOFOLLOW` 컴포넌트 순회·루트 (st_dev,st_ino) 신원 고정·기존
  파일은 단일 `O_RDWR` fd 로 백업+쓰기 결속·계획-상태 보존)로 전환한다. 루트 교체는 전체 중단
  (오염 차단), 파일 단위 경쟁은 loud 제외·rc 0 이다.
- **private-context 대장 라인 키 탈결합** — 검토 대장 키를 `(path, kind, match, surface, hash)`
  +count 로 재설계해 라인 이동만으로 재생성이 강제되던 결합을 제거한다(재생성 CLI 동반).
- **domain 현재-진실 규칙 출하** — 판정 요지를 `_template.md` 스캐폴드와 capture-draft 산출에
  싣고, 출하 wiki 전 파일의 링크 해소를 가드가 검증한다.

### Breaking (채택자 체감 동작 변경)
- 소유 PM 홈을 해소할 수 없는 위치의 무인자 `external_review` 실행은 경고 후 진행하지 않고
  rc=1 로 차단한다(문서화된 게이트 경로 `--ticket`/`--paths` 는 무영향). 커밋만 된 변경으로
  슬롯을 고르려면 `--base` 앵커 명시가 필요하다.
- wiki seed 를 symlink 로 운용하는 인스턴스는 add-harness/`--into` 가 rc=1 로 중단한다
  (이전: 해당 파일만 조용히 무시).

## [1.6.0] - 2026-08-06

**Compaction-native 컨텍스트 가드 릴리스.** 관통 성질: 컨텍스트 한계 대응을 차단(hard-stop)에서
"압축은 허용하고 서사를 미리 박제"로 전환한다. 가드 산출은 비차단 checkpoint 안내·log 박제·relay
회전으로 한정되며, 세 하네스(claude·opencode·codex)가 같은 규약을 공유한다. 전 경로 3하네스 실
LLM 라이브 검증 완료.

### Added
- **checkpoint 점진 박제** — `pm_log.py checkpoint` subcommand 신설. ticket 경계의 complete
  entry 에 task 태그가 붙고, compaction 경계에서 checkpoint entry 골격을 만들어 서사를 압축 전에
  보존한다. 연속성의 단일 진실은 파일(log·pm_state·board)이다.
- **relay 회전 가시화** — 세션 회전 시 loud 알림을 출력하고, 첫 spawn 의 bootstrap 응답도
  출력한다. ctx 예산의 유효 회전 임계가 bootstrap 실측 상한 이하이면 구동 전에 fail-loud 로
  거부한다(spawn-loop·토큰 무한 소모 차단).
- **import trust 상태 loud 화** — 신규 import 프로젝트가 trust 미승인이라 출하
  `permissions.allow` 가 무력한 상태(silent degrade)를 import 시점 안내와 콘솔 경고로 표면화한다.

### Changed
- **3하네스 ctx 가드 비차단 전환** — claude 훅은 stop 차단(deny/block)·핸드오프 allow-list·
  PreToolUse 등록을 삭제하고 stop 을 최종 넛지로 전환하며, compaction 후 밴드 marker 를
  재무장한다(멱등을 세션당 1회에서 사이클당 1회로 재정의). opencode plugin 은 stop 차단을
  삭제하고 임박 안내에 `session.compacted` 병행 신호를 추가한다(압축 후 checkpoint 안내 적재·
  plugin 재기동 시 압축 횟수 복원). codex PreCompact 훅은 compaction 차단을 제거하고 통과 +
  checkpoint 안내로 전환한다(README Context safety 전면 개정·headless 도달성 실측 명기).
- **relay 회전 판정을 driver usage 판정으로 일원화** — 세 driver 가 매 turn 후 wire usage 로
  post-turn marker 를 박제한다(claude stream-json usage / opencode step_finish tokens / codex
  누계 차분). pre-turn 의미론은 폐기. codex 는 thread 누계 usage 를 점유로 오독해 약 2.8배
  조기 회전하던 것을 turn 간 차분 판정으로 교정했다.
- **handoff 얇은 마감** — handoff entry 를 박제 entries 자동 목록 + 메타 학습 + pending intent +
  회귀 1줄로 축소했다(thread-tail 슬롯 폐지 — 서사는 ticket/checkpoint 경계 박제가 담당).

### Fixed
- **relay 파이프 입력이 조용히 사라지던 문제** — claude/opencode driver 가 child stdin 을
  격리하지 않아 supervisor 의 파이프 입력을 child 가 통째로 삼키던 것(첫 spawn 후 무출력
  정상종료)을 stdin 격리로 통일했다. 무출력 rc0 급 이상은 stderr 진단을 남긴다.
- **공유 log writer 의 동시 기록 lost-update** — log append 계열 writer 전부를 flock 직렬화
  seam 으로 수렴해 병렬 기록 유실 클래스를 닫았다.
- 옛 차단형(hard-stop) 표기 잔재 전수 정리 — README 3종 개정·전 타깃 전파·fresh-adopter 검증.

## [1.5.2] - 2026-08-03

**게이트 신뢰성 릴리스.** 관통 성질 — 게이트가 무엇을 검토했는지 기계가 보장한다: stale 스냅샷
검토(false-green), 프로세스 cwd 오해소, 엔진 사본 불일치의 조용한 흡수를 전부 fail-loud 로 닫았다.

### Added
- **게이트 격리 스냅샷 도구**(`gate_snapshot.py`) — 검토 대상 경로의 staged 내용이 working tree 와
  일치함을 검증하고 격리 스냅샷을 만든다. 미-stage 산출이면 생성을 거부한다(자동 stage 없음 — 병렬
  작업 오염 금지). HEAD·index 엔트리·실 파일 집합을 생성 전후 이중 대조하고, submodule 원본은
  건드리지 않으며, eol 속성 파일은 git 정규화 기준으로 비교한다. 병렬 작업에서는 `--paths` 를 파일
  단위로 지정한다. code-reviewer 위임 카드가 수동 절차 대신 이 도구 호출을 안내한다.

### Fixed
- **엔진 사본 불일치가 조용히 흡수되던 문제** — 넓은 예외 처리 경계 141곳을 전수 분류해 marked
  불일치는 재전파 또는 명시 종단 보고만 허용한다. AST 가드가 파라미터 전달·클로저·조건식 콜러블 등
  6개 축으로 새 흡수 유입을 차단하고, CLI 도구들은 불일치 시 traceback 대신 한국어 복구 안내
  (`pm-update 로 엔진 전체를 동기화한 뒤 다시 실행`)와 rc=1 로 끝난다.
- **위임·외부리뷰 도구가 프로세스 cwd 에 따라 조용히 오해소되던 문제** — 앵커를 명시 인자
  (`--cwd`·`--paths`·ticket touches)에서 파생한다. 외부리뷰 라운드 상한은 판정 라운드와 중단(kill)
  라운드를 구분해 센다(판정 4 + 미완 2). (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **보호훅이 `dirname` 부재 환경에서 무승인 push 를 통과시키던 문제** — 훅 경로 해소를 dirname
  비의존으로 바꾸고 해소 실패는 차단이다(fail-closed).
- **codex 채택자가 출하 문서 안내대로 스킬을 못 부르던 문제** — 스킬 진입 표기를 하네스별로 렌더한다
  (codex `$이름`·그 외 `/이름`). 실 설치본을 스캔하는 가드가 잔존 오표기를 차단한다.
- **다중 하네스 설치가 권장 경로에서 항상 경고 7건으로 시작하던 문제** — 템플릿 공유 파일을 byte
  동일로 정규화했다. 경고 채널은 실제 충돌에만 남는다.
- **부분 산출물 표시·실행 상한 경고가 도구마다 갈리던 문제** — 포맷터를 공용 seam 으로 단일화하고
  `--fill` 도 위임과 같은 상한 경고를 공유한다. 등록 하네스에 fill 실행기가 없으면 설치 전에 거부한다.
- 게이트 격리 스냅샷 안에서 board 앵커 의존 테스트가 항상 red 라 회귀가 완주하지 못하던 문제.
- 중앙 로더 전환 잔여(legacy 판정 경로·이중 traceback) 정리.

## [1.5.1] - 2026-08-01

**채널 신뢰성 릴리스.** 관통 성질 — 채널이 조용히 실패하지 않는다: 정상 작업을 죽이던 시간 판정,
산출물을 잃던 백그라운드 위임, 채택자 환경에서만 갈리던 설정·설치 형상을 라이브 실측 근거로 닫았다.

### Fixed
- **장시간 위임·외부 리뷰가 벽시계 타임아웃에 잘리던 문제** — 시간 판정을 "시작 후 경과"에서
  "마지막 진행 이후 침묵"으로 바꿨다. 진행 신호는 stdout·stderr 양쪽을 보므로 진행 로그가 전부
  stderr 로 나오는 평문 리뷰어도 살아남는다. 벽시계는 백스톱으로 남고 임계는 설정에서 조정한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **백그라운드 위임이 끊기면 산출물을 못 찾던 문제** — raw 파일 위치를 실행 *전에* 공유 장부에
  등재한다. 호출이 죽어 표준출력(그 안의 경로)을 잃어도 `pm_delegate.py raw --unfinished` 로
  조회하며, 미마감 레코드 자체가 중단 증거다.
- **폴백이 claude·opencode 실패에는 발동하지 않던 문제** — 인프라 실패 분류가 codex 표기만 담고
  있었다. 세 하네스 전부 라이브 실측·공식 문서 근거로 한도·인증 패턴을 편입했고, 연결 실패처럼
  진단이 아예 나오지 않는 침묵은 기존 무응답 판정이 담당한다. 설정 오류는 여전히 미분류로 남아
  폴백 없이 실패를 알린다.
- **위임 대상이 작업 워크트리에서 티켓 본문을 못 읽던 문제** — 조회 명령이 장부로 단일 소유 PM
  홈을 확정해 입력을 재앵커한다(쓰기는 여전히 PM 홈 전용).
- **두 하네스를 함께 쓰는 채택자의 opencode 어댑터가 갱신되지 않던 문제** — 설치 manifest 를
  선택한 트리들의 합집합으로 잡는다.
- **머신-로컬 파일이 채택자에게 출하되던 문제** — 출하 열거를 git 추적분으로 좁혔고, 열거 결과
  0건은 성공이 아니라 실패로 세운다.
- **엔진 사본마다 설정 해소가 갈리던 문제** — 위임·외부 리뷰가 어느 설정 파일로 나갔는지 실행마다
  표시하고, 트리별 설정이 실제로 다르면 경고한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **opencode 템플릿의 무시 규칙 파일이 자기 자신을 숨겨 출하되지 않던 문제**.
- **시크릿 스캔 승인이 폴백 수신자까지 승계되던 문제** — 승인은 해소된 수신자에 결속되며, 승인이
  붙은 실행은 폴백을 끄고 그 사유를 남긴다.
- **하네스 감지가 설정 경로 환경변수를 세션 표시로 오용하던 문제** — 실측한 세션 마커만 근거로
  삼는다.

### Added
- **세션 시작 로딩 축소** — 부트스트랩 사전 읽기 계약을 출하 문서 전 축에서 통일해, 새 세션이 대형
  문서를 통독하지 않고 진입 문서·현재 상태·부트스트랩 출력만 읽는다. 재유입은 테스트가 막는다.
- **어댑터 편집 금지의 기계 경고** — 위임이 어댑터 디렉토리(`.claude`·`.codex`·`.agents`·
  `.opencode`)를 건드리면 티켓 범위 판정과 무관한 축으로 경고한다.
- **출하 카드 구조 가드** — 코드펜스 파괴·하네스 전용 토큰 유입을 테스트가 잡는다.
- **문서 단위 재검증 채널** — `board.py verified-at-repin --page <경로>` 로 문서 하나만 재핀한다.
- **출하 엔진 커버리지 기계 집계** — `domain.py coverage` 가 담당 문서의 정성 주장과 실제를 대조한다.

### Changed
- 파일 열거·트리 순회 구현이 여러 도구에 흩어져 있던 것을 공용 진입점 하나로 모았다.
- 테스트가 실제 임시 디렉토리에 산출물을 쌓던 것을 정리했다(누적 파일이 실 산출물을 덮던 문제).

## [1.5.0] - 2026-07-28

**채택자 컨텍스트 감소 릴리스.** 관통 성질 — 출하물은 채택자가 쓰는 데 필요한 것만 담는다:
조회할 수 없는 개발 이력 포인터와 유지보수용 서사를 걷어내고, 재유입은 테스트가 기계로 차단한다.

### Changed
- **에이전트가 로드하는 문서 24% 경량화** — 스킬 카드 15장·에이전트 정의 4장·운영
  매뉴얼(pm_role/pm_playbook)·진입 문서(CLAUDE/AGENTS)를 지시 중심으로 압축 재작성
  (합계 293KB → 225KB). 커맨드·경고·Windows 노트·치환 토큰·절차 순서는 그대로다 —
  같은 행동을 더 적은 컨텍스트로 유도한다.
- **출하 소스·문서·런타임 메시지에서 내부 이력 참조 제거** — 채택자 환경에서 해소되지 않는
  내부 작업 번호·설계문서 절 참조·변경 경위 서사를 엔진 주석·docstring·CLI 출력·출하 md 에서
  정리했다. 이유 설명은 산문으로 자족하고, 이력은 git 히스토리가 담당한다.

### Added
- **사설 참조 재유입 가드** — 출하 표면(엔진 소스 산문·출하 md·templates)에 내부 작업 번호나
  설계문서 절 참조가 다시 들어오면 테스트가 실패한다. 마커류는 기준선 래칫으로 신규 발생만
  차단하고, 기준선이 실제보다 크면(정리 진행 미반영) 그것도 잡는다.
- **사설 참조 스트립 도구**(`scripts/strip_private_refs.py`) — 산문 토큰만 결정적으로 정리하는
  유지보수 도구. 코드 문자열·ID 예시 데이터는 건드리지 않으며, 삭제-only·들여쓰기 보존을
  스스로 검증한다.

## [1.4.5] - 2026-07-27

**Windows 실사용 마찰과 세션 격리 누수를 닫은 유지보수 릴리스.** 관통 성질 — 환경이 어긋나도
조용히 잘못된 상태로 가지 않는다: 콘솔 인코딩·인터프리터 버전·컨텍스트 보호·티켓 가시성이 전부
기계로 확인되거나 시끄럽게 실패한다.

### Added
- **Python 지원 하한(3.11) 선언과 검증** — 인터프리터 탐지·진입 파사드·보호 훅이 3.11 미만을
  후보에서 탈락시키고, 전부 미달이면 발견한 버전을 나열해 알린다. 도구 실행 방식과 동일한
  방식으로 버전을 확인하므로 런처가 다른 버전으로 우회 실행하는 창이 없다. 도입 진입점도
  불명확한 import 오류 대신 필요 버전을 명시해 중단한다.
- **위임 시크릿 차단의 건별 명시 승인** — 전송 전 스캔이 프롬프트를 막으면 탐지된 항목을 전부
  열거하고 승인 토큰을 함께 낸다. 사람이 발췌를 읽고 판단한 뒤 그 토큰으로 한 번만 통과시킬 수
  있으며, 승인은 그 프롬프트 전문과 해소된 수신자에 묶인다(한 글자만 바뀌거나 다른 모델로 보내면
  재승인). 상시 우회 설정은 만들 수 없다.

### Changed
- **커맨드 카드가 인터프리터 실값을 채운다** — 세션 시작 시 출력되는 명령 목록이 설정에 기록된
  검증된 인터프리터로 렌더링된다(해소 실패 시 기존 표기 유지). Windows 세션이 문서 표기를 그대로
  복사해 실행 실패·재시도로 도는 낭비가 줄어든다.
- **opencode 자동 컴팩션을 기본값으로 되돌림** — 전역으로 꺼두면 위임 서브에이전트가 컴팩션도
  컨텍스트 정지도 없이 죽는다(정지 기제는 메인 세션 전용). 메인 세션은 정지가 컴팩션보다 먼저
  발화해 그대로 보호되며, 그 선행 조건과 모델 설정 함정을 설정 주석에 명시했다.
- **세션 기본 화면이 사용자까지 구분한다** — 작업 이름이 겹치거나 작업공간을 넘겨받았을 때 다른
  사용자의 티켓이 섞여 보이던 문제를 닫았다. 숨겨진 항목이 있으면 이유를 알린다(조용히 빈 목록이
  되지 않는다).
- **작업공간 기준점을 슬롯 기록에서 읽는다** — 기준점을 지정한 작업공간은 그 기준으로 뒤처짐을
  판정한다(미지정이면 저장소 기본값). 판정에 쓴 기준의 출처도 함께 표시한다.

### Fixed
- **한국어 Windows 콘솔에서 명령이 죽던 문제** — 일부 명령이 출력 기호에서 인코딩 오류로 중단됐다.
  콘솔 인코딩 설정을 공용 진입 절차로 모으고 모든 명령줄 도구에 배선했으며, 새 도구가 이를
  빠뜨리면 테스트가 잡는다.

## [1.4.4] - 2026-07-27

**cross-harness 위임 채널을 실전 등급으로 끌어올리고(인프라 폴백·범위-밖 변경 가드·시크릿 스캔
정밀화), 좌표계·문서 신선도·출하 정합의 구조적 미탐/오탐을 닫은 릴리스.** 관통 성질 — 프롬프트
규율 대신 기계 판정: 위임이 만든 범위 밖 변경, 전파 안 된 어댑터, 좌표 불일치가 전부 기계로
표면화되거나 차단된다.

### Added
- **위임 인프라-실패 폴백** — 외부 하네스의 한도 소진·타임아웃·스폰 실패·응답 지연 시, 설정된
  폴백 하네스로 1단 loud 폴백한다(발동 사유·실행 provenance 를 결과에 명시). 발동은 인프라 실패의
  양성 분류만 — 모델 답변이 오류 문구를 *인용*해도 오발동하지 않고, 정상 완료(반려 포함)·전송-전
  차단은 폴백 대상이 아니다. **미설정이면 기존 fail-loud 그대로**(엔진 하드코딩 기본값 없음).
- **위임 범위-밖 변경 가드** — 위임 회수 시 작업 트리 전/후를 기계 비교해, 티켓 선언 범위 밖의
  신규/수정/삭제/이름변경, 이미 dirty 하던 파일의 재수정, 파일 모드 변경, 심지어 **위임 중 발생한
  커밋**까지 경고 블록으로 표면화한다(차단 아님 — 판정은 사람 몫). 읽기 전용 역할은 어떤 변경이든
  경고. 위임 시 `--ticket` 으로 허용 범위를 지정한다.
- **좌표 normalizer** — 멀티-worktree 형상에서 티켓 작업 범위 선언(홈 좌표)과 코드 저장소 상대
  좌표의 구조적 미매칭을 공용 seam 하나로 해소: domain 영향 페이지 소환이 실제로 동작하고(항상
  "없음" 이던 결함 폐쇄·디렉토리 선언도 매칭), 완료 부기의 stage 가 선언 경로를 실제로 싣는다.
  검증된 작업공간(장부+디렉토리 실재+git 저장소 정체성)일 때만 변환하고 불일치는 시끄럽게 거부.
- **읽기 조회의 앵커 표시** — board 조회 출력 첫 줄에 실제 측정한 저장소 경로와 역할 라벨을 명시해,
  다른 저장소 상태를 자기 것으로 오독하는 클래스를 닫는다.

### Changed
- **시크릿 스캔 양성매칭 전환** — 위임 프롬프트의 시크릿 차단을 문맥-무시 substring 에서 경로축
  (파일명·시크릿 디렉토리 정확-세그먼트)+값축(발급기관 prefix·개인키 블록·고엔트로피 할당·URL
  자격증명) 양성매칭으로 교체. 정상 설정 키명이 차단되던 오탐을 없애면서 이전엔 통과하던 실
  시크릿 형태(값·URL 내장 자격증명·`id_rsa` 류)를 신규 차단한다. 차단 메시지에 매칭 발췌를
  표시하되 값은 마스킹한다.
- **출하 정합이 blocking 으로 승격** — 프레임워크 루트의 토큰-form 어댑터 원본이 render-leak 으로
  오탐되던 12건을 출하-템플릿 미러 판정(전 타깃 byte 대조)으로 해소하고, 반대로 **어댑터 수정 후
  전 타깃 미전파**는 이제 lint 가 차단한다(어느 타깃이 뒤처졌는지 지목).
- **문서 신선도 시계 = 담당 코드 소유 저장소** — 페이지가 담당하는 코드가 다른 저장소(upstream)에
  있으면 그쪽 git 시계로 신선도를 판정한다(사본 시계의 조용한 오답 창 폐쇄). 검증 앵커 일괄 재핀
  CLI 동반.
- **외부 리뷰 타임아웃 실측 기반 900s** 기본 + 설정 채널(정상 라운드가 죽던 300s 대체)·실패 사유 병기. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **엔진 일괄 전파 `--all-targets`** — 존재하는 모든 출하 타깃에 한 번에 동기하고, 진입문서의 타깃
  열거와 실디렉토리 집합 일치를 기계 검증한다.
- **task 진입 좌표 단일화** — task 세션 진입은 `--task` 단독으로 고정하고 슬롯 좌표 혼합 지정은
  일괄 거부한다(어느 축이 이겼는지 모호하던 상태 제거). subprocess 텍스트 디코딩 UTF-8 명시 동반.

## [1.4.3] - 2026-07-25

**문서-신선도 판정의 거짓 green 을 근절하고, dual-harness 채택 채널과 장기 세션 안정성을
강화한 패치.** 관통 성질 — **판정할 수 없는 것은 조용히 통과시키지 않고 정직하게 알리며,
안전장치·리뷰 게이트는 자의 판단이 아니라 기계 한도로 멈춘다.**

### Added
- **문서 신선도 관찰불가 표면화** — 현재-진실 문서(architecture/status/domain)의 `verified_at`
  판정이 "판정 불가"를 구분해 advisory 로 알린다: 저장소에 없는 covers 경로, 다른 git 의 SHA,
  움직이는 ref(브랜치/태그·16진 이름 포함), 모호한 축약 SHA, 비-선조 커밋 — 전부 이전엔 조용한
  green 이던 클래스. anchor 는 canonical full OID 로 유일 해소해 기록·소비한다.
- **guest 어댑터 인스턴스 manifest 등재** — `add-harness` 가 설치한 guest 어댑터(dual-harness)를
  인스턴스 `engine.manifest` 마커 구획에 등재해, 채택자 형상에서도 렌더·잔존-토큰 검사가 guest 를
  커버한다. 업데이트는 guest 를 덮지 않고(구획 보존·plan 제외), 재실행(refresh)이 동기 채널.
  cross-harness 의존물(예: codex 호스트의 `.claude/skills`)도 정확히 따라온다.
- **외부 리뷰 라운드 상한** — 게이트별 라운드 장부·기본 4회 한도. 초과분은 실행 전 차단되고
  사용자 승인(`--ack-rounds`) 후에만 재개된다(호출 전 예약이라 타임아웃으로 우회 불가). (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **cross-harness 역할 위임 채널 (`pm_delegate`)** — PM 세션이 세션을 떠나지 않고 역할 노동
  (developer·researcher·architect·code-reviewer)을 **다른 하네스 CLI** 로 위임한다. 호출측 하네스
  조건 0(N×N 대칭) — claude·codex·opencode 세 드라이버를 지원하고, 역할→(하네스·모델·reasoning)
  매핑을 설정에서 **티어 세트 통째로** 해소한다(평시/난제 2티어·부분 상속 없음·미설정은 조용한
  폴백 대신 fail-loud). 역할축으로 권한을 강제하고(쓰기=developer·architect / 읽기=researcher·
  code-reviewer), 엔진 코드 쓰기 위임이 잘못된 저장소를 향하면 차단하며, 프롬프트 시크릿 스캔과
  하위 프로세스 환경변수 정제를 거친다. 결과는 최종 답변만 회수하고 원문은 별도 파일로 박제.
  **기본 OFF** — 외부 송신·과금 수용 opt-in 을 설정에서 켜야 동작한다. (T-0887 에서 폐지 — 이 축은 더 이상 없다)

### Changed
- **컨텍스트 가드 세션-스코프 분리** — hard-stop 은 메인 세션만(정제 handoff 강제), 서브에이전트는
  compaction 을 허용해 장기 위임 작업이 컨텍스트 한도로 죽지 않는다(auto-compact 재활성).
- **covers 글롭 판정을 원본 글롭으로** — 신선도·stale 판정이 손실 접두사 대신 원본 글롭
  (`:(glob)`·지원 문법 검증)을 git 에 직접 전달한다. 미지원 형태는 오번역 대신 advisory.
- **잔존-토큰(overlay)·테스트 게이트의 하네스 축 파생 전환** — 손-열거를 manifest/레지스트리
  파생으로 교체해 새 하네스가 자동 편입된다(닫힌 은퇴 채널은 명시 보존).

### Fixed
- 엔진 자기서술 stale 3건(pm_import docstring·`--task` help·주석 경로) 정정.
- add-harness/manifest 경로 안전(containment·symlink 거부)·TOML 사용자 override 보존 강화.

## [1.4.2] - 2026-07-24

**Codex 어댑터 첫 실전 운영에서 드러난 결함을 닫는 패치.** Codex 를 PM/개발 하네스로 실제
운영(도그푸딩)하며 발견된 권한·compaction·설정 보존 결함들과, 공유 board·엔진 갱신의 정직성
결함을 수정한다. 관통 성질 — **어댑터가 채택자 소유 설정을 덮지 않고, 안전장치는 차단 후
복구 경로까지 제공하며, 도구는 실패를 성공으로 위장하지 않는다.**

### Added
- **Codex 일상 작업 권한 정렬** — Claude `settings.json` 동급의 allow/deny 를 Codex
  execpolicy 로 제공. 일상 명령·통상 `git push` 는 무질의 실행, 파괴적 push
  (`--force`/`--delete`/`--mirror` 등)만 확인 질의로 분리.
- **Codex TUI auto-compaction 차단** — 프레임워크는 compaction 이 아닌 handoff 지향.
  PreCompact hook 이 auto-compaction 을 차단하고(내구 상태 보존), 차단이 반복되는 임계
  상황에는 **one-shot 복구 절차**(chat ID 확인 → hook 비활성 resume → handoff → 새 세션)를
  화면에 안내한다 — 영구 설정 약화 없이 빠져나오는 break-glass. footer 에 잔여 컨텍스트 표시.
- **Codex ticket scaffold·board 상태 디렉토리 자가복구** — 부분 손상 형상에서 조용히 실패하던
  경로를 자가복구로.

### Fixed
- **어댑터 refresh 가 채택자 소유 Codex 설정을 덮던 문제** — `add-harness`/재-import 가
  `config.toml`·`hooks.json`·`AGENTS.md`·agent 별 model override(명시 `model`/
  `model_reasoning_effort` 있는 TOML)를 보존한다(byte 보존·loud skip). native delegation
  override 도 보존.
- **board 가 local commit 실패를 "보존"으로 위장 보고하던 문제** — 실패를 정직하게 보고.
- **ticket 완료 stage 가 잘못된 저장소에 고정되던 문제** — task 모드에서 실제 작업
  worktree 기준으로 저장소별 stage.
- **무변경 엔진 갱신에서 revision 키가 수렴하지 않던 문제** — `pm-update` 반복 실행 멱등.
- **render-leak 백스톱의 확장자 열거 누락·어댑터 토큰 치환 오판** — 제외-판정 방식으로 역전해
  신규 확장자에도 안전.
- 네트워크 격리 환경에서 소켓 E2E 테스트가 EPERM 으로 깨지던 것을 capability 감지 skip 으로.

### Changed
- **PM 지시 문서의 git commit 을 경로 명시형으로** — bare `git commit` 이 공유 워킹트리에서
  타 작업의 stage 를 함께 싣던 클래스를 절차·가드로 폐쇄(디자인 stage/commit 스코프화 포함).
- **게이트 하네스 축을 파생으로 전환** — 하네스 목록을 단일 원천에서 파생해 신규 하네스 추가
  시 게이트 누락을 방지.
- README 를 발표 본편과 운영 레퍼런스로 분리·흐름 개편.

## [1.4.1] - 2026-07-21

**프레임워크가 자기 자신을 운영하다 드러난 결함 9건을 닫는 패치.** 세션 진입(부트스트랩 0단계)이
실행 불가능한 해소 커맨드를 주던 것에서 시작해, 보호 브랜치 규율의 기계 강제·보호목록 설정 채널
신설·공유 board 의 claim 차단 해소까지 이어졌다. **관통하는 성질 하나 — "값을 바꿨는데 그 값이
실제 동작에 도달하지 않는" 끊김과, "기계가 정합하다고 보고하는데 실은 아닌" 거짓 green 을 없앤다.**
전 변경 이중게이트(내부 reviewer + codex 외부·실결함 21건 다라운드 수렴)·회귀 4600.

### Added
- **보호 브랜치 목록 설정 채널** (ADR-0072·T-0417) — 지금까지 `areas.md` 표를 손으로 고치는 것이
  유일한 경로였고, 고쳐도 훅이 읽는 sidecar 는 stale 이라 사용자가 바꿨다고 믿는 값과 훅이 강제하는
  값이 갈렸다(silent). 기계 설정·조회 + 값-연결 폐쇄:
  - `pm-config repo add <name> --protected "main,develop"` — 등록 시점에 보호목록 지정
    (종전 `protected=""` 하드코딩 제거).
  - `pm-config repo protected <repo> [<목록>|default]` — 값 없으면 **조회**, 주면 **설정**
    (`upstream show|set` family 동형). `default` = 칼럼 비움 = main/master/develop 폴백
    ("보호 없음" 은 표현 불가 — 빈 문자열은 거부하고 `default` 로 안내).
  - 조회는 **실효값 + 출처(명시/기본값 폴백/미등록) + 훅 sidecar 정합/drift** 3줄 — "빈 값이라
    기본값으로 도는 중"·"이 clone 의 훅만 옛 목록" 을 각각 구별해 보여준다.
  - 설정은 **areas.md → sidecar 순서 고정**(역순이면 비준되지 않은 목록을 훅이 강제) + board-git
    best-effort 동기. 다른 clone 은 `/pm-bootstrap` 0단계가 **drift 일 때만** sidecar 를 재설치한다
    (정합이면 subprocess 0·fail-soft).
  - `pm-config repo list` — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner). 빈
    `protected` 는 "기본값" 을 명시해 "보호 없음" 오독을 막는다.
- **`board.areas_set_cell(repo, column, value)`** (ADR-0072) — areas.md 를 "등록(행 추가)은
  append-only, **기존 셀 변경은 `board_lock()` 하 비파괴 in-place 재기록**" 으로 재규정한 범용
  백엔드. 줄 종결자(CRLF)·주석·타 행 보존, 구 헤더는 canonical 8칼럼으로 업그레이드, wider-row 는
  canonical 인덱스로 매핑 — 인덱스 해소는 `_migrate_areas_text` 와 **공용 헬퍼**로 공유한다
  (두 벌로 갈라지면 T-0168 칼럼 오매핑 재발). 대상 repo 행이 2개 이상이면 fail-loud(부작용 0).
- **`board lint` `areas-duplicate-repo` 권고** (ADR-0072·advisory·never-block) — 중복 repo 행이
  first-match 로 조용히 굳는 것을 상시 표면화한다(자동 병합 없음·사람 판정).

### Changed
- **보호 브랜치에서의 `git commit` 이 차단된다** (ADR-0071·T-0415) — **동작 변경**: 지금까지
  통과하던 커밋이 막힌다. 풀 슬롯(worktree)에서 보호 브랜치(`main`/`master`/`develop`·per-repo
  override)를 체크아웃한 채 커밋하면 pre-commit 훅이 거부한다. 종전엔 "보호 브랜치에 자율
  commit 하지 않는다" 가 문서 규율일 뿐이었고 강제 수단은 *push* 단계뿐이었다.
  - escape 는 `PM_ALLOW_PROTECTED_COMMIT=1`(예: `PM_ALLOW_PROTECTED_COMMIT=1 git commit ...`) —
    `PM_ALLOW_PROTECTED_PUSH` 와 동형 시맨틱. detached HEAD 는 통과한다.
  - **비커버**(우발 방지 가드이지 적대적 통제가 아니다): `git commit --no-verify` · merge 커밋
    (`pre-merge-commit` 소관) · rebase/cherry-pick/revert. 하드 백스톱은 pre-push 훅(라이브 게이트 포함).
  - 릴리즈 flow 는 그대로다 — 릴리즈 커밋은 release 브랜치에서 하고 `main` 은 merge 로 받으면
    escape 없이 통과한다.
  - 훅은 우리 bare 미러(`.repos/<repo>.git`)의 `core.hooksPath` client-side 가드다 — **회사
    repo 서버 ref·사용자 클론 무영향**(pre-push 훅과 같은 배선·sidecar 공용). 그 미러를 쓰지 않는
    clone(예: PM 홈 자신)은 영향 없다.
- **`pm-update` 가 매 실행마다 보호 훅 정합을 확인하고 어긋나면 다시 깐다** (T-0415) — 훅은 엔진
  코드에서 생성되는 런타임 산출물이라 **파일 복사만으론 새 훅이 배포되지 않는다**(설치 트리거가
  `repo add`·`worktree add` 뿐이라 엔진만 올린 인스턴스는 새 가드를 못 받았다). 이제 설치된 훅
  본문·보호목록 sidecar·`core.hooksPath` 배선을 현 엔진과 대조해 **정합이면 조용히 넘어가고**
  어긋나면 재설치한다(훅 디렉토리가 통째로 지워진 clone 도 자가치유). 실패는 loud 경고 + 재설치
  커맨드(update 종료코드는 불변).
  - **업그레이드 노트**: 이 버전을 흡수하는 그 `pm-update` 실행은 *갱신 전* 엔진이 수행하므로
    훅이 아직 안 깔린다. **흡수 직후 `pm-update` 를 한 번 더 실행**하면(변경이 없어도 정합
    확인이 돌아) pre-commit 가드가 배포된다. `pm-config repo add <repo>`(멱등)로도 즉시 깔린다.
- **board 의 미커밋 변경이 더 이상 무관한 티켓의 claim 을 막지 않는다** (ADR-0073·T-0419) —
  공유 board(별도 git) 형상에서 파일 하나가 uncommitted 라는 이유로 **모든** claim 이 전면
  차단됐고, 안내는 `add -A`(= 남의 미완성 편집까지 대신 커밋)였다. 조율 권위는 로컬 워킹트리의
  clean 여부가 아니라 **원격 ref 의 fast-forward push** 라는 원칙으로 재정렬했다:
  - **선점 감지가 읽기 전용**이 됐다 — `fetch` + 원격 트리 직접 조회(`ls-tree`)로 그 티켓이
    이미 claimed/done/blocked 인지 본다(로컬 변경 0·통합 성공에 의존하지 않음). claim 의
    `pull --rebase` 는 **원격이 앞섰을 때만** 시도한다. 단일 clone 다중 슬롯은 같은 로컬
    브랜치를 공유해 behind=0 이므로 잔여 차단이 구조적으로 발생하지 않는다.
  - **커밋 스코프 = 그 mutation 이 만진 경로만** — claim 과 best-effort 6곳
    (new/promote/complete/block/unclaim/unblock) 전부. 공유 워킹트리의 무관한 미커밋 작업이
    board 커밋에 실려 push 되던 누출이 닫힌다(`.gitattributes` 는 **엔진이 이번에 보강한
    경우에만** 함께 실린다 — 사용자가 편집 중인 그 파일을 대신 커밋하지 않는다).
  - **롤백이 비파괴** — `reset --hard` 폐기. `reset --soft` + 그 티켓 파일 역이동·원본 복원 +
    **index 스냅샷 복원**만 한다(무관한 미커밋 작업 불변). 스냅샷은 claim 직전 그 두 경로의
    index 항목을 그대로 뜬 것이라, 대상 티켓이 unstaged/untracked/staged 어느 상태였든 롤백 후
    `git status` 가 claim 직전과 **같다**(대상 파일만 staged 로 바뀌던 사각 폐쇄). push 가
    timeout 으로 끝나면 원격
    브랜치가 그 claim 커밋을 **포함하는지** 재확인해(fetch + 조상 판정) 이미 반영됐으면
    롤백하지 않는다(원격 claimed·로컬 open 인 고아 claim 폐쇄). 롤백 뒤엔 winner 를 로컬에
    반영하고, 미커밋 변경 때문에 못 당기면 "로컬 board 뷰 stale" 을 loud 하게 알린다.
  - **잔여 차단은 사유가 정확하다** — `원격 앞섬 ∧ 통합 불가` 일 때만 막고, 사유를 갈라
    (미커밋 파일 / rebase 충돌 / offline / upstream 미설정 / 원인 미상) `behind N` + **막고 있는
    파일 목록**(최대 5건 + 총계) + 그 경로만 커밋하는 안내를 낸다. mid-rebase 는 `rebase
    --abort/--continue` 를, detached 는 `checkout` 을 안내한다(옛 2단 오진 폐쇄). 분류는 git
    메시지 문자열이 아니라 **관측 상태**로 한다(로케일·git 버전 무관) — 특히 원격에서 들어오는
    파일과 같은 경로의 **미추적 파일**이 pull 을 막는 경우를 더 이상 offline 으로 오진하지
    않고, 그 파일을 커밋하거나 옮기라고 안내한다.
  - **board-git mutation 직렬화 락 신설** — 같은 clone 의 두 슬롯이 commit→push→rollback 을
    인터리브하던 창을 닫는다. board-git 미분리(legacy·솔로) 채택자는 **100% 무변경**.
- **보호 브랜치 훅 설치 실패가 이제 loud 하다** (T-0417) — `pm-config repo add`·`worktree add` 는
  훅 설치 결과(False)를 조용히 삼켜서, 훅이 안 걸렸는데 사용자는 걸린 줄 알았다(보호 가드 침묵
  무력화). 이제 실패 시 stderr 경고 + 재실행 커맨드가 나간다(성공 출력·종료코드는 불변 — 보호 훅은
  추가 가드라 등록/슬롯 생성을 깨지 않는다). 같은 이유로 이미 등록된 repo 에 `--protected` 를 주면
  "반영되지 않았다" 를 loud 하게 알리고 `repo protected` 로 안내한다.

## [1.4.0] - 2026-07-21

**codex CLI(OpenAI Codex 0.144.x)가 세 번째 지원 하네스로 추가**(ADR-0070) — claude_code·opencode
와 동급 풀 파리티(위임 4축·스킬·ctx 가드·relay·라이브/릴리즈 게이트). 동반해 **진입 doc 을
"얇은 harness-neutral 공통 코어 + 하네스별 네이티브 채널"로 재편**(ADR-0069). 전 변경 이중게이트
(내부 reviewer + codex 외부·실결함 10건 다라운드 수렴) + 라이브 실측(실 codex·gpt-5.5) 통과·회귀 4302.

### codex 하네스 어댑터 (ADR-0070)
- **`templates/codex/` 신설** — `pm-import.sh --new <dest> --harness codex` 로 채택, 기존 인스턴스엔
  `pm-config add-harness codex` 로 비파괴 추가(공존 조합은 add-harness 채널로 통일). 어댑터 구성:
  `.codex/agents/` 위임 4축 TOML(developer/architect/code-reviewer/researcher·`model` 생략=사용자
  config 상속)·`.agents/skills/` canonical 스킬 15종 remap 사본·`.codex/config.toml`+`hooks.json`
  (auto-compact 상향·PreCompact 핸드오프 tripwire·instance-owned)·relay driver `pm_orch_codex.py`.
- **PM = 메인 세션** — codex 전용 정적 진입 doc 없음. 공통 코어 `AGENTS.md`(자동 로드) + 부트스트랩
  커맨드 카드의 codex 절(env `CODEX_THREAD_ID`/`CODEX_CI` 기계 감지) + 스킬이 운영 지침을 전달.
- **trust 2단계 필요(codex 플랫폼 제약)** — 첫 진입 시 ① 대화형 `codex` 로 프로젝트 trust 수락
  ② `/hooks` 로 hook trust 승인. import/add-harness 가 loud 안내(`-c` CLI trust override 는 무효·실측).
- `--fill auto` codex 지원(fill runner) + 미지원 하네스 silent 폴백을 fail-loud 로 전환.

### 진입 doc 공통 코어 + 조건부 자동 마이그레이션 (ADR-0069)
- **opencode `AGENTS.md` 재편** — 자족 매뉴얼(22.6KiB)→harness-neutral 공통 코어(codex 와
  byte-parity). opencode-고유 실행모델·위임규약은 `.opencode/pm-instructions.md`(자동 로드·전파
  채널 등재 — 방법론 갱신이 채택자에 도달)로 이관.
- **기존 opencode 채택자는 pm_update 흡수 시 조건부 자동 전환** — 진입 doc/`opencode.jsonc` 가
  출하 원본과 byte-일치(미수정)면 자동 전환+백업(`.pm_import_backups/`), 수정 흔적 있으면 무손 +
  loud 안내·1회 마이그레이션 절차 제시. 재실행 멱등.
- add-harness 경로 백업 디렉토리 gitignore 위생 배선(main import 와 대칭).

### 라이브/릴리즈 게이트 codex 축
- `PM_ORCH_LIVE=1` 에 codex 축(adr/ticket 발행 flow 실 LLM 검증) + relay live smoke. **릴리즈
  livegate 수집 pin 16→17** — codex 라이브 green 없이는 release push 가 차단된다.
- relay 신뢰성: codex usage wire 정규화·ctx stop **marker 계약 이원화**(pre-turn=재전송/post-turn=
  무재전송 — 이미 실행된 turn 의 이중 실행 방지)·sandbox `workspace-write` 명시 핀·stdin 무기한
  대기 방어.

### 운영 규율
- 병렬 wave 의 내부 리뷰 게이트용 **격리 스냅샷 절차**(staged-only worktree) 표준화 — 리뷰↔dev
  편집 경합 클래스 폐쇄(pm-dev-delegate 스킬).

## [1.3.5] - 2026-07-20

task 세션 표면의 **슬롯-집합 1급화**(ADR-0068) + 엔진 업데이트-채널 견고화. adopter#0 도그푸딩
(PM 78 task 모드 실전 + 회사 채택자 흡수 실패)이 실측으로 드러낸 두 결함군을 한 릴리즈로 닫는다.
전 변경 이중게이트(내부 reviewer + codex, 여러 건 codex 다라운드 수렴) 통과·회귀 4152·사이클 e2e
릴리즈 게이트 신설.

### task 세션 슬롯-집합 1급화 (ADR-0068)
task 는 슬롯의 묶음인데 세션 lifecycle 표면들이 "세션=슬롯 1개" 문법을 상속해 묶음 처리가 끊기던
것을 불변식 4개(집합 변경=재열거·진입 전수 열거·alloc 항상-신규·지목은 실행 순간만)로 재설계.

- **부트스트랩 진입 = 보유 집합 전수 열거+검증** (T-0399) — task 모드가 보유 슬롯 전체를 행렬로
  열거하고 슬롯별 0단계 검증(기록↔live·점유·보호브랜치·stale/creating). fault 1+ = 진입 차단(전
  fault 일괄 표시+해소 커맨드). 0슬롯=no-op.
- **task alloc = 항상 신규 대여** (T-0398·BREAKING(task 축)) — `alloc --task` 의 멱등(기존 슬롯을
  신규처럼 반환하던 silent aliasing) 폐기 → 항상 idle 신규 대여(같은 repo 복수 보유 지원). 집합
  변경 연산(alloc·release·task end·add)은 결과 집합 재열거. `worktree add <repo> --task <이름>` =
  생성 직후 그 슬롯 task 명의 대여. task-명의 lease 도 reclaim/재부착 보호(tasks 장부 조인).
- **핸드오프 퇴장 = 집합 전체 두고-가기** (T-0393) — 재스냅은 보유 전 슬롯, 회귀는 변경 흔적 있는
  슬롯만(stale 슬롯 fail-loud). task 회귀 cwd 미배선(REPO 폴백 red)도 해소.
- **인계 트리거 = `--task` 앵커** (T-0394) — task 모드 핸드오프 트리거가 `/pm-bootstrap --task
  <이름>`(슬롯 자동 수령). 엔진층 task명 validator choke.
- **task 사이클 e2e = 릴리즈 게이트** (T-0400) — 생성→편입→작업→핸드오프→재개 완주를 실 엔진/실
  git 로 검증하는 기계 e2e 를 livegate 수집 pin 에 편입. "단위게이트 green·사이클 단절" 클래스를
  릴리즈마다 차단.

### 엔진 업데이트-채널 견고화
회사 채택자가 pm-update 후에도 신규 엔진 파일이 안 와 AttributeError 로 깨진 사건의 근본 해소.

- **pm_update manifest 자기치유** (T-0396·amends ADR-0032/T-0142) — self-update 가 upstream
  manifest 를 계획 기준으로 승격(2-pass 단일 실행)해 구형 로컬 manifest 라도 한 번에 신규 등재분이
  도달. self-prop `@source` 따라 flavor↔flavor 대조(클로버 차단).
- **manifest skew 탐지 + baseline 억제** (T-0395) — 로컬 manifest 가 upstream 신규 등재분을 놓친
  상태에서 upstream_rev baseline 을 최신으로 찍어 drift-lint 가 침묵하던 false-최신 차단(loud
  경고+갱신 억제).
- **엔진 버전 스탬프 정합 fail-loud** (T-0397) — 도구 사본 skew(신 도구+구 sibling)를 로드 시점에
  "엔진 사본 불일치·pm-update" 명시 에러로(baked `ENGINE_REV` 대조·부분복사도 검출).
  `engine_rev.py --bump vX.Y.Z` 로 버전 일괄 갱신·릴리즈 태그와 정합 가드.

### BREAKING
- **`alloc --task <이름>` 이 항상 신규 idle 슬롯을 대여**한다(T-0398) — 기존 멱등(같은 repo 보유 시
  기존 슬롯 반환)에 의존하던 스크립트는 거동이 바뀐다. task 축은 v1.3.x 신설이라 실사용 채택자
  영향은 없다고 본다.

## [1.3.4] - 2026-07-20

세션/task 라이프사이클 장부 정합 4결함 폐쇄 + 표면(메시지) 개선 — 전부 additive·BREAKING 없음.
adopter#0 도그푸딩(PM 78 task 모드 첫 실전)이 실측으로 발견한 것들. 전 변경 이중게이트(내부
reviewer + codex·2건은 codex must-fix 재작업 수렴) 통과·회귀 4047.

### Fixed
- **pm_handoff 종료 git 재스냅 배선** (T-0388) — 핸드오프가 "두고 간 상태"를 lease 장부에 재기록하지
  않아(도착 스냅만 잔존), 세션 중 브랜치가 바뀌면(릴리즈 등) 차기 부트스트랩 0단계가 `diverged`
  외부-개입 **오경보**로 FAIL-LOUD 하던 갭 폐쇄. 부기 완료 후 `record_git_snapshot(slot)` 호출
  (base 보존·`--done` 경로 제외·fail-soft loud).
- **사람 bind lease 의 reclaim/재부착 보호 — `Lease.bound` 마커** (T-0389) — `bind_slot` 의 pid 는
  즉사하는 bootstrap subprocess pid 라, 타 명의 `alloc` 진입 `reclaim_stale` 이 stale 오판으로 남의
  세션/task 바인딩을 회수(정체성 탈취)할 수 있었다(T-0074 가 dormant 로 박제한 cross-path 를 F2 task
  alloc 출하가 live 화·PM 78 실측). `bound: true`(additive·구 장부=false·마이그레이션 0)를 bind 가
  기록, reclaim 과 alloc branch/resume 재부착 경로가 제외. 해제는 명시 lifecycle 전이에서만.
- **부트스트랩 `--task` 동반 `--repo/--slot` = task 명의 원스텝 바인딩** (T-0390) — 종전엔 세션 명의
  bind + task 작업공간 0개라 직후 F6 이 loud 차단하고 `pm-config alloc` 별도 스텝을 요구. 이제 지정
  슬롯을 `bind_slot(session=<task명>)` 로 리스해 부트스트랩 한 줄로 작업 가능(멱등 재진입·타 명의
  점유는 거부·슬롯-only/task-only 경로 100% 불변). 카드 슬롯 번호는 슬롯 식별자에서 파생(task명
  session 오염 기계 차단).
- **task 정상-종료 기록 — 재개 crash 오탐 제거** (T-0392) — task 장부 pid 는 즉사하는 bootstrap
  subprocess pid 라, 핸드오프가 정상-종료를 기록하지 않으면 **모든 재개**가 "회수(이전 세션 crash·
  다른 창 작업중일 수 있음)" 경고로 표시됐다. 핸드오프 완료 단계가 `release_task_pid`(pid=0)를
  기록하고 재개가 clean resume 으로 분류된다 — 진짜 crash(pid>0 잔존)·산 pid 거부(2창)는 현행 유지.

### Added / Changed (표면·거동 변경 0)
- **메시지 3건** (T-0391) — ① 신규 task 첫 부트스트랩: `task 1차` + "🆕 신규 task — 복구할 인계
  없음" 사유 명시(종전 "(log/current.md 없음 또는 entry 파싱 실패)" 오독 제거) ② 0단계 diverged
  FAIL-LOUD: head 관계 판정 근거 + 정당 판단 시 재동기 커맨드 실값 제시 — `worktree_pool.py record
  <slot>` CLI 신설(`record_git_snapshot` thin 노출·자동 실행 없음·해소 주체=사용자 불변) ③ 핸드오프
  재스냅 출력: 실갱신 vs 무변경(스냅 불가·기존 유지) 구분. pm-worktree 스킬 열거에 `record` 반영.

## [1.3.3] - 2026-07-19

세션 뷰의 축을 "생성 세션" 하나로 통일하는 뷰 계층 변경 — 타 세션 티켓 정보는 기본 출력에서 완전히
사라진다(카운트 줄 포함). 전 변경 이중게이트(내부 reviewer + codex 3라운드 수렴) 통과·회귀 3996.

### BREAKING
- **`board list` 세션 기본 뷰 = 생성-세션 스트림·타 세션분 완전 비노출** (ADR-0067·ADR-0066 amend) —
  무인자 `board.py list`(및 부트스트랩 dump)와 명시 세션 뷰 `board list --repo X --slot N` 이 이제 **그
  세션이 생성한 open(`created_by` 세션 일치) + 그 세션 claim** 만 출력한다. ADR-0066 의 **"그 외 open
  N건" 접힘 카운트 줄은 제거**됐고(타 세션 정보의 기본 노출도 컨텍스트 누수로 간주), open 카운트 모수도
  내 스트림으로 좁혀졌다. 전체/타 세션은 명시 `board list --all`(경합 가시·backlog 확인·타 PM 열람) 몫.
  - **스트림 판정 = 생성 세션**(uniform·슬롯/task 공통) — ADR-0066 의 **task-prefix 스트림 판정을 폐기**
    (prefix 는 ID 라벨일 뿐). 같은 사용자의 타 슬롯 생성 open 도 명시 세션 뷰에서 비노출(PM 77 누출 fix).
  - **`board new --repo X --slot N`** 추가 — created_by 에 `<user>/<repo>_<N>` 생성-세션을 기록(claim 과
    동일 identity 해소 경로 재사용). 미명시 시 현행(user-only / 유도 세션) 유지.
  - **`--mine`(user-wide·전 세션)·`--all`·strict-exclude(타 사용자 차단) 의미론 불변.** 솔로/무바인딩
    (세션 미해소)은 user-단위(--mine) 폴백(solo=subset·N=1 이면 user 스트림=세션 스트림이라 등가).
  - **세션 뷰의 claim 판정도 session 라벨 축** — `claimed_by` 의 세션 토큰 일치로 판정(user 무관·open
    의 생성-세션 판정과 대칭). user 미해소(git email 부재) 상태에서도 자기 세션 claim 이 보인다. user
    축 조회는 `--mine` 몫.
  - **부트스트랩 dump 정합** — "그 외 open N건" 줄·open 전용 backlog 라벨(`_OPEN_SCOPE_LABEL`)·`--all`
    전량 재조회(접힘 모수)·"타 세션 진행(claimed)" 현황 줄 제거. "다른 활성 PM" 슬롯 레지스트리(환경
    정보·leases 유래)는 유지. 무인자 출력·부트스트랩 dump 를 파싱/의존하던 스크립트·문서는 `--all` 로
    갱신 필요.
  - 발단: fresh 슬롯/task 세션 첫 화면·기본 조회에 타 세션 티켓(그 세션이 안 만든 것)이 섞여 컨텍스트
    누수(사용자 재문제제기·PM 77). 유실 방지는 카운트 상시 노출이 아니라 명시 조회·핸드오프 인계로 이동.

## [1.3.2] - 2026-07-19

세션 기본 뷰를 "내 스트림"으로 좁히는 뷰 계층 변경(무관 backlog 는 카운트 접기) + 릴리즈 절차 문서
결함 수정. 전 변경 이중게이트(내부 reviewer + codex 3라운드 수렴) 통과·회귀 4004·실데이터 3표면
실출력 검증.

### BREAKING
- **`board list` 무인자 기본 뷰 = 내 스트림 스코프** (ADR-0066) — 무인자 `board.py list`(및 부트스트랩
  dump)가 이제 **내 세션 claimed + 내 task prefix 의 open** 만 상세로 보이고, 그 외 open backlog 는
  "그 외 open N건 — 전체는 `board.py list --all`" **카운트 1줄로 접는다**(유실 방지 유지·N>0 항상
  표시). 기존 무인자 전체 뷰는 신설 **`board list --all`** 로 이관 — 무인자 출력을 파싱/의존하던
  스크립트·문서는 `--all` 로 갱신 필요. `--mine`/`--task`/`--repo`+`--slot` 렌즈·strict-exclude·open
  데이터 모델(슬롯 무소속·claim 조정)은 불변. 접힘/스트림/카운트의 모수는 공유 풀 전량(소유 무관 —
  strict-exclude 는 `--mine` 렌즈 한정). solo 특례 없음(단일슬롯 솔로도 open 은 접힘).
  - 발단: fresh 슬롯/task 세션 첫 화면에 무관 backlog 가 상세로 섞여 오독·노이즈(실측). 티켓은
    사용자/스트림 소속이지 슬롯 소속이 아니므로, 기본 화면은 자기 스트림만 — 공유 backlog 의 존재는
    카운트 줄이 승계한다.

### Fixed
- **릴리즈 절차 태그 push 실패** — 릴리즈 안내(pm-release 스킬)의 태그 push 명령이 브랜치명=태그명
  컨벤션에서 refspec 모호("src refspec matches more than one")로 실패하던 것(v1.3.1 릴리즈 실측) →
  `git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z` 명시형으로 교체.

## [1.3.1] - 2026-07-19

릴리즈 브랜치명=태그명 컨벤션에서 세션 시작이 가짜 "외부 개입" 경보로 차단되던 결함 수정(브랜치명
해소 full-ref 전환·전 소비자) + `regression run --cwd` 핀의 모호 오발화 수정 + `board list` 에
board-git 최신성 1줄 표면화. 전 수정 이중게이트(내부 reviewer + codex) 통과·회귀 3982.

### Fixed
- **동명 태그+브랜치에서 세션 시작 가짜 차단** — 릴리즈가 브랜치명을 그대로 태그로 찍으면(예
  `v1.3.0` 브랜치+태그) git 브랜치명 조회(`symbolic-ref --short`)가 모호성 회피 `heads/<name>` 을
  돌려줘, 슬롯 git 기록-대-실측 비교가 "외부 개입" FAIL-LOUD 로 부트스트랩을 차단했다(매 릴리즈
  재발 구조). 브랜치명 해소를 full ref(`symbolic-ref HEAD` → `refs/heads/` 정확 제거)로 전환해
  모호성을 원천 제거 — 진짜 이름이 `heads/x` 인 브랜치도 오인하지 않는다. 같은 클래스의 잔존
  소비자(부트스트랩 freshness 표시·repo 등록 기본브랜치 해소 2곳)도 동일 패턴으로 마감.
- **`regression run --repo <r> --cwd <경로>` 모호 오발화** — readonly 슬롯 추가 등으로 repo 활성
  슬롯이 2개 이상이면 `--cwd` 로 실행 위치를 핀했는데도 actor 슬롯 특정이 모호 오류로 죽던 결함.
  v1.3.0 의 livegate `--cwd` 수정과 동형으로 폐쇄 — `--cwd` 명시 시 모호하면 조용히 repo/local
  test_cmd 폴백(명시 `--slot` 의 슬롯 test_cmd 유도는 유지). push 게이트(check 경로)는 불변.

### Added
- **`board list` board-git 최신성 표면화** — 모든 list 변형이 board-git freshness 1줄(`최신`/
  `판정불가 — 스냅샷일 수 있음`/behind ⚠)을 **stderr** 로 advisory 표기 — stale board 스냅샷을
  최신으로 오독하는 클래스의 잔여 표면 폐쇄(부트스트랩 표면화와 같은 판정·오프라인 시 "최신"
  오단정 없음). 매 호출 원격 조회 비용은 60s TTL 가드+5s 상한으로 완화 — 비-git(solo) 형상은
  표기·조회 모두 없음.

## [1.3.0] - 2026-07-19

task-단위 PM(named task 정체성·readonly 공유 슬롯·슬롯 git 엔진화) + PM 운영 명령어化(티켓 authoring·codex 게이트·ADR 발행·릴리즈) + 진실유지 기계화(verified_at sha lint·contradiction lint) + opencode 스킬 단일 소비 전환. 전 티켓 이중게이트(내부 reviewer + codex) 통과·회귀 3952·라이브 dual-harness 실측.

### BREAKING
- **부트스트랩 0단계 main-참조 슬롯 진입 거부** (T-0360) — 슬롯 HEAD 가 **보호 브랜치(main 등) 직접 checkout** 또는 **보호 브랜치 원격(origin/main 등)을 upstream 으로 추적** 상태면 `/pm-bootstrap` 0단계가 **부분 dump 도 없이 진입 거부**한다(기존 = 경고 후 통과). 정상 작업 슬롯의 feature upstream(예 `origin/<feature>`) 추적은 거부 대상이 아니다. main 직접 checkout·수동 tracking 은 `--no-track`(신규 파생 경로에만 적용)이 못 막는 무방비 구멍이었고, 방어가 pre-push 훅(T-0076) 하나뿐이라 커밋이 다 된 뒤 push 순간에야 막혔다 — 진입 시점으로 앞당겨 canonical/보호 브랜치 오염 커밋을 원천 차단한다. readonly 공유 슬롯(detached·role=readonly)은 예외(브랜치 자체가 없음).

  - **채택자 영향**: main-checkout(또는 origin-추적) 슬롯을 쓰던 채택자는 갱신 즉시 부트스트랩이 거부된다 — 부분 dump 도 안 나온다(세션 진실 오염 방지).
  - **해소 절차 (택1)** — 거부 메시지가 실값 커맨드로 안내한다:
    - (a) 코드 읽기 기준면이 필요하면 readonly 슬롯을 만든다: `pm-config worktree add <repo> --readonly`
    - (b) 이 슬롯을 작업 브랜치로 전환한다(main 직접 checkout 이탈): `git -C work/<repo>_<N> switch -c <repo>_<N>`
  - **릴리즈 절차 재배선**: 릴리즈 livegate `--cwd`·codex `--paths` 가 지목하던 "origin/main 이 도는 slot-1" 역할은 readonly 공유 슬롯(detached·origin/main 기준면)으로 이전됐다(pm-release 스킬/커맨드 §2). readonly 슬롯은 무리스라 `--slot` 로 안 잡히므로 릴리즈는 `livegate record --cwd <readonly 슬롯 절대경로>` 로 핀한다.
  - **다운스트림 lockstep** (finance/회사 등 채택자): BREAKING 이라 main-checkout 슬롯 운용은 갱신 후 거부된다. `pm_update`(엔진 흡수) 후 위 해소 절차대로 각 슬롯을 readonly 또는 작업 브랜치로 정리하고, 릴리즈 절차를 쓰는 채택자는 livegate 핀을 readonly 슬롯 `--cwd` 로 갱신한다.

- **opencode `command/*.md` PM-workflow 사본 채널 은퇴 — canonical 스킬 단일 소비** (T-0364·ADR-0065) — opencode ≥**1.17.19** 가 `.claude/skills/*/SKILL.md` 를 네이티브 스캔하고 슬래시(`run --command <스킬명>`)로 호출하므로(라이브 실측), 수기 command 사본 11종을 제거하고 opencode 템플릿도 canonical `SKILL.md` 미러(`.claude/skills`·bare @render)를 싣는다. 수기 사본 pair-pin 재-pin 수고·silent-drift 채널 소멸.
  - **채택자 영향**: opencode 1.17.19 미만은 스킬 스캔이 없어 지원하지 않는다(폴백 없음). `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` 미설정 전제. 기존 인스턴스의 `.opencode/command/*.md` 잔재는 무해하나 갱신 후 제거를 권장(엔진 lint 는 legacy-compat 로 계속 스캔).

### Added
- **task-단위 PM (named task 정체성 축)** (T-0350~T-0357) — 슬롯 축과 직교하는 사람-명명 작업 단위: `--task <이름>` 귀속(`claimed_by=<user>/<task>`·예약 패턴 `<repo>_<N>` 금지·단일 validator 깔때기)·task prefix 설정·부트스트랩 0단계 preflight(엔진 앵커/작업공간/불완전 생성/타 점유/기록-live 정합 기계 검증·실패 시 부분 dump 금지)·슬롯 기준점(base) 장부 기록(set-base)·`board list --task` 렌즈(T-0365·read 경로도 검증 깔때기).
- **readonly 공유 슬롯** (T-0358) — research/기준면 전용 detached 슬롯: `pm-config worktree add <repo> --readonly`·배타 대여 없음·mutation/lease-op 거부·`refresh`(fetch→detach 이동+submodule 재동기·dirty 거부).
- **슬롯 git 운영 엔진화** (T-0359·T-0361) — `pm-worktree status`(단일/일괄·submodule pin/drift·dirty)·`rebase`(선-검사 4종·충돌=그대로+loud·`--abort` 미호출·장부 원자 갱신)·`pm-config status` 2축 cockpit(task 축+slot 풀·branch@head·base 대비 behind).
- **PM 운영 명령어化 4종** (ADR-0049) — `/pm-ticket`(티켓 authoring: draft→5절 fill 검증→promote·placeholder/절-삭제 게이트)·`/pm-review`(codex 게이트 규율: worktree 앵커·stage 선행·경로 핀 + PM 홈 앵커 능동 차단)·`/pm-adr`(ADR 발행 원자화: 채번·lifecycle back-ref 발행시점 부기·README 색인 이동·log entry·YAML 안전 직렬화)·`/pm-release`(릴리즈 순서 고정·main push 는 승인 게이트 유지). 전 스킬 frontmatter `audience` 라벨(user-entrypoint/pm-internal) 완비(T-0370).
- **진실유지 기계화** — 현재-진실 문서 freshness 를 `verified_at: <sha>` 이후 매핑경로 커밋 유무의 이진 판정으로 교체(T-0363·date 근사 폐기·`verified-at-backfill` 1회 커맨드·sha 실존 검증) + **contradiction lint**(T-0369·ADR-0064): ADR 개정(amends/supersedes) 순간 옛 결정을 참조하는 문서의 잔여 모순 후보를 advisory 표면화(탐지 LLM DI·기본 dry·판정 사람).
- **커맨드 카드 파서-생성화** (T-0362) — 부트스트랩 카드가 공용 정의서에서 모드별(task/slot/readonly) 렌더·카드↔CLI (tool,render)급 정합 가드.

### Changed
- **보호브랜치 커밋 차단이 진입 시점으로** — 위 BREAKING(T-0360) 참조.
- **promote/발행 게이트 강화** (T-0366) — 본문 5절(목표/인터페이스/결정/DoD/참고) 존재+절별 placeholder 잔존을 authoring 게이트에서 차단(전역 lint 는 3절 불변·레거시 무영향).
- **external_review 안전화** (T-0367) — adopter#0 PM 홈 앵커에서 `--paths` 없이 실행 시 diff 추출 전 fail-loud+worktree 재지정 안내(빈-diff 사후 차단의 사전 승격).

## [1.2.4] - 2026-07-17

cwd 오실행 stray 클래스 폐쇄 patch — 단일 티켓(T-0345). 이중게이트(내부 reviewer + codex 반려 1건 재작업 수렴) 통과.

### Fixed
- **PM-홈 worktree cwd 오실행 fail-loud 가드** (T-0345) — board 쓰기-경로(mutation subcommand 전수)와 `ticket_finish` 를 PM 홈의 등록 worktree(코드 전용·board 미소유) cwd 에서 실행하면, worktree 트리에 stray 티켓/log 를 **조용히 만들던 것**을 실행 전 fail-loud(실제 PM 홈 경로 안내)로 차단. 감지 = 3중 conjunction(실-board 미소유 ∧ linked git worktree ∧ 조상 PM 홈 board 소유) — 솔로/standalone·비-git·PM 홈 cwd 는 무영향(오탐 0). 읽기 경로(list/show/lint)는 무-게이트 유지. 실측 재현 픽스처(stray 티켓·stray log·오경보) 포함.

## [1.2.3] - 2026-07-17

v1.2.2 잔여 소진 patch — "버전 N 작업 중 발견분은 버전 N 에 탑재" 규율 아래 신뢰성 감지·worktree 안전성·부트스트랩 freshness·문서 정합을 일괄 마감. 이중게이트(내부 reviewer 5 PASS + codex 반려 3건 재작업 수렴) 통과. 파일-전달 규약(v1.2.2 T-0337)의 edit-denied 에이전트 실행 가능성도 라이브 확증 완료(T-0338 — opencode `safe_write` 는 permission deny 와 무관하게 가용[tools 맵=override 실측]·claude 는 Bash 경로·갭 0).

### Added
- **opencode 32k cap-hit detector** (T-0339) — 출력 상한(32k tok) silent 절단("stop" 위장)을 호출층에서 감지: relay 출력 소비 지점에서 응답이 cap 근방(char 임계 34560 = 32000×1.2char/tok 하한×0.90·정확 토크나이저 미의존 보수 근사·오탐 0 마진)이면 loud advisory(파일-전달 규약 안내 포함)·**never-block**(emission try/except)·env `PM_OC_CAP_HIT_THRESHOLD`. claude 는 `stop_reason=max_tokens` 네이티브 노출이라 범위 밖.
- **opencode command 수기 사본 silent-drift 가드** (T-0344) — claude 스킬(`.claude/skills/*/SKILL.md`)과 opencode command 사본(`.opencode/command/*.md`·자동 전파 채널 없는 수기 적응)의 대응쌍을 pair-pin(정규화 content hash) 테스트로 잠금 — 어느 한쪽 변경·신규 스킬 사본 누락 시 red + 정합 지시(재-pin 은 테스트 파일 직접 실행으로 drop-in 출력). canonical 갱신이 사본에 조용히 누락되는 클래스 제거(full 자동 생성기 전까지의 forcing function).
- **부트스트랩 board freshness·슬롯 시대차 경고** (T-0341) — offline(fetch 실패) 시 "최신" 오단정 제거("판정불가 — 스냅샷일 수 있음" fail-soft) + 슬롯 worktree HEAD 가 base 브랜치 대비 behind N 커밋이면 identity surface 에 경고(lean/alloc/JSON 3표면·advisory·areas 등록 base 만 신뢰·기존 freshness fetch 재사용=신규 fetch 0). 다중 사용자/슬롯 공유 board 의 stale 스냅샷 오신뢰 방지.

### Fixed
- **worktree 슬롯 재생성 충돌 fail-loud** (T-0335) — remove 가 보존한 미머지 브랜치와 같은 번호 재생성이 cryptic `already exists`(rc255) + orphan-worktree 오귀인으로 죽던 것 → `SlotBranchExists` **선-검출**(base·else 경로 공통)·명확한 두 갈래 안내(정리 후 재시도 / 수동 checkout 재개).
- **create_slot `branch=` 데이터-유실 클래스 종결** (T-0343) — `-B`(create-or-reset)가 기존 브랜치를 리셋해 보존 커밋을 잃을 수 있던 것 → 기존 브랜치는 **리셋 없는 checkout** 분기(신규만 `-B`)·존재 판정은 color-safe helper(`git branch --list --format=%(refname:short)`·ambient `color.branch=always` ANSI 오염 방어·실 git 백스톱 테스트).
- **opencode 문서·카드 정합** (T-0342) — config(opencode.jsonc·plugin·agent frontmatter)는 **프로세스 시작 시 로드·캐싱**(변경=완전 재시작 필요) 을 AGENTS.md 에 명시 · read-only 에이전트 카드의 "write 16KB 거부" 문구를 실체(write/edit 전면 deny → `safe_write`·bash)로 정정(가드 read-only-aware 분리·write-capable 요구 무약화) · 옛 `.nudge` 잔재 전수 점검(cruft 0 확인).

## [1.2.2] - 2026-07-17

opencode 문제해결 + 누락-기능 수정 patch — 대용량 write/edit silent-truncation 어댑터 가드(upstream 미해결 실측·라이브 재현) + worktree 슬롯 제거 단일 원자 커맨드. 이중게이트(내부 reviewer + codex 다라운드) 통과·라이브 매트릭스 실측 포함. (계획됐던 opencode subagent 기본 background 화는 sentinel 라이브 실측에서 headless 결과+작업 유실이 확정돼 **drop** — 상세는 PM 홈 log verify entry.)

### Added
- **opencode safe-write 가드 3층** (T-0334) — ① `tool.execute.before` **deny-and-redirect**: write/edit args 16KB(기본·`PM_SAFE_WRITE_DENY_BYTES`) 초과 시 throw + 모델-facing 유도(기존 파일 재작성→`edit` 분할·신규 대형 파일→`safe_write`). ② **`safe_write` custom tool**: 8KB chunk(기본·`PM_SAFE_WRITE_CHUNK_BYTES`) 강제 create/append — root containment 커널 강제(create=`openSync "wx"`·append=`O_NOFOLLOW`·절대경로/lexical/realpath-symlink 3중 거부). ③ opencode.jsonc 모델 `limit { context, output }` 명시(1.17.19 config 검증이 둘 다 요구·미명시 시 output 32000 hardcoded fallback). 근거 실측: 무가드 237KB 단일 write = **silent 실패**(파일 미생성·tool call 미성립·finish "stop" 위장)·무툴 대형 응답 = 정확히 32000 토큰 절단. 한계 명기: tool-call JSON 자체 절단은 plugin 이 못 잡음(upstream #18108·#19604·#17471 추적).
- **대형 산출물 파일-전달 규약** (T-0337) — 모델 응답(서브에이전트 최종 보고 포함)이 출력 상한(32k 토큰)에서 finish "stop" 위장으로 **조용히 절단**되는 축(도구로 인터셉트 불가)을 규약으로 우회: 출하 서브에이전트 카드 8종(양 하네스 × researcher/developer/architect/code-reviewer)에 "보고가 ~200줄/8KB 초과 예상이면 본문은 파일로(opencode 는 `safe_write` 8KB 청크)·응답엔 절대경로+핵심 요약 ≤10줄" 절 명시. read-only 역할은 산출-아티팩트 예외로 양립. 기계 가드 40 케이스(section-slice·하네스 관용 검증).
- **opencode stall 워치독 — 무한 hang 종결** (T-0336) — `opencode run` 이 스타트업 network fetch stall(간헐·자체 회복 없음·라이브 규명: brownout 창에서 시작한 프로세스는 창이 끝나도 미회복)에 빠지면 **첫-이벤트 워치독**이 kill+재시도: 첫 json 이벤트 `PM_OC_FIRST_EVENT_TIMEOUT`(기본 90초) 무소식 → 프로세스 그룹 kill → 재시도 `PM_OC_STALL_RETRIES`(기본 2) → 소진 시 fail-loud. 적용 3표면 = relay driver(턴 fail-soft·600s 가드 보존)·`pm_import --fill auto`·release 라이브 테스트(fail-loud). provider `headerTimeout`/`chunkTimeout` 노브는 무응답-서버 결정 실험에서 무효 실측이라 미채택. 무응답 소켓 픽스처 e2e 로 실 바이너리 kill+재시도 실증.
- **`pm-config worktree remove <slot> [--force]`** (T-0333) — 슬롯 통째 제거 단일 원자 커맨드: 리스/장부 확인→dirty·활성 리스 거부(`--force`=stash 보존+**stash-후 dirty 재검사**로 submodule 내부 변경 유실 차단)→`git worktree remove`(+prune)→전용 브랜치 정리(`-d` 고정=머지 시에만 삭제·미머지 보존·공유 브랜치 스킵)→장부 삭제. **장부까지 지워 `add` 가 빈 번호 재사용** — 수동 제거→dangling 장부→번호 skip footgun 종결. 제거 3분법: 등록 슬롯=`remove` / dangling 장부=`prune-stale` / orphan worktree=사용자 `git worktree remove`. pm-env 스킬/커맨드 3표면 동기.

## [1.2.1] - 2026-07-16

v1.2.0 직후 backlog 소거 patch — 게이트 무결성·부트스트랩 오독 방지·nudge 능동화·문서 정합. 이중게이트(내부 reviewer + codex·codex 반려 2건 재작업 수렴) 통과·라이브 검증 포함.

### Fixed
- **external_review 빈-diff fail-loud** (T-0326) — 빈/공백 diff 를 외부 리뷰어 호출 전에 exit 1 로 차단(원인·조치 안내 포함). 분리 형상(adopter#0 등)에서 stale 사본 실행이 "변경 없음 통과"로 위장하던 false-green 원천 차단. dry-run 포함 무조건 fail. (T-0887 에서 폐지 — 이 축은 더 이상 없다)
- **pm_bootstrap `--branch`/`--resume` repo-가드 순서** (T-0327) — 가드를 auto-resolve 앞으로 이동, `--repo` 없는 호출이 자동바인딩 슬롯에 silent 부착되던 edge 차단(에러 문구 불변).
- **pm_bootstrap 보드 요약 open 라벨 오독 방지** (T-0331) — open 카운트 라벨을 `(공유 backlog·슬롯무관)` 으로 정정(보드 섹션+첫-turn 요약 양쪽·done/claimed 는 슬롯-스코프 유지) + **타 세션 진행(claimed) 현황 1줄** 병기(전용 무렌즈 조회·fail-soft) + pm-bootstrap 카드에 "board 숫자는 스냅샷 — 옵션 제시 전 `list --mine` 교차 확인" 지침. claimed 행 파서는 고정폭 컬럼 위치 기반(제목/tags 내용 불독·cmd_list 실행 통합 가드).
- **pm_import 치환-제외 목록 하드코딩 제거** (T-0329) — 방법론 문서 제외 집합을 치환 시점 dest 인스턴스 manifest 에서 파생(신규 방법론 문서 자동 편입·`--from` 흡수 경로 정합) + broken-manifest 폴백 floor. identity_args 로더 관용구 통일·pm_playbook 라벨 정렬 동반.

### Changed
- **graceful nudge 2단 강화** (T-0328·ADR-0037 확장) — hard-stop 직전 strong 밴드(`min(stop+3%p, nudge)` 파생·노브 추가 없음) 신설: "지금 즉시 `/pm-handoff`" 능동 유도·단계별 멱등·claude/opencode 파리티·statusline 빨강 "정지 임박" 표시.
- **nudge 주입-도달 라이브 durable 테스트** (T-0286) — on-demand(`PM_ORCH_LIVE=1`) probe 기반 시나리오 2건(claude 라이브 1회 실통과·opencode 는 upstream tool-loop hang 확증 후 skip 박제). CI 기본 skip·release pin 불변.
- **dual-harness guest 어댑터 갱신 채널 정식화** (T-0330·ADR-0058) — "엔진+host 어댑터=`pm_update` / dual-harness 로 얹은 guest 어댑터=`add-harness <harness>` 재실행(refresh·live-safe)" 를 출하 문서 5표면에 명시(guest-flavor 채택자는 기존대로 pm_update 전파·ADR-0054).

## [1.2.0] - 2026-07-16

슬롯 정체성 CLI 플래그를 **decomposed `--repo`/`--slot`** 단일 방식으로 통일한다([[ADR-0057]] supersede [[ADR-0043]]). 여러 세대에 걸쳐 누적된 `--session`(actor)·`--worktree-slot`·`--session-num` 별칭을 **BREAKING 제거**하고, 전 도구가 공용 `identity_args` 모듈로 수렴한다. T-0313 슬롯-모호 remedy 오안내가 근본 소멸. 이중게이트(내부 reviewer + codex) 통과.

### BREAKING
- **정체성 CLI 플래그 통일 → `--repo`/`--slot`** (ADR-0057) — 아래 구 별칭을 **제거**했다(back-compat 없음). 정체성 인자를 받는 전 도구(board·pm_bootstrap·pm_handoff·ticket_finish·pm_config·worktree_pool)가 공용 `identity_args` 로 수렴.

  | 구 (제거됨) | 신 (canonical) |
  |---|---|
  | `--session <repo>_<N>` (actor) | `--repo <repo> --slot <N>` |
  | `--worktree-slot work/<repo>_<N>` | `--slot <N>` (`--repo` 와 함께) |
  | `--session-num <N>` (pm_handoff 차수) | `--session-seq <N>` |
  | bare `--slot <N>` (`--repo` 없음) | `--repo <repo> --slot <N>` (단독 `--slot` 은 fail-loud) |

  - `--repo X --slot N` → 슬롯 정체성 `<repo>_<N>`. `--repo X` 단독(actor) → 그 repo 활성 슬롯이 정확히 1개면 자동 해소·≥2 또는 0 이면 fail-loud. `--slot` 단독(`--repo` 없음) → fail-loud. 인자 전무 → 기존 해소 체인(`$PM_SESSION_NAME` > 활성 슬롯 lease 1개 > `local.conf session=`)은 **불변**.
  - **free-form `--session <name>` CLI 제거** — 커스텀 세션명은 `$PM_SESSION_NAME` 환경변수(또는 `local.conf session=`)로 바인딩한다. `board.py claim` 은 이제 `--repo`/`--slot`(+ `--user`)만 받는다.
  - `--session-seq`(handoff 차수·뷰-무관)·하니스 `--session-id`(대화 연속성)는 정체성과 무관해 **유지**한다.
  - **채택자 마이그레이션**: `board.py claim --session myproj_1` → `board.py claim --repo myproj --slot 1` · `pm_handoff --session-num 19` → `--session-seq 19` · `--worktree-slot work/myproj_1` → `--repo myproj --slot 1`.
  - **다운스트림 lockstep** (finance/회사 등 채택자): BREAKING 이라 미갱신 어댑터/스크립트의 구 플래그 호출은 깨진다. `pm_update`(엔진 흡수) + `add-harness` refresh(어댑터 표면)로 흡수한 뒤 위 매핑대로 호출 표기를 갱신한다.

### Changed
- **공용 `identity_args` 모듈** (T-0322) — 정체성 인자 파싱(`add_identity_args`/discriminated `parse_identity`)과 리스 원장 읽기를 전 도구 단일 진실로 수렴(도구별 복붙 제거·DRY).
- **docs/skill/command-card + 어댑터·템플릿 전수 sweep** (T-0320) — 25개 shipped 표면(pm_role·pm_playbook·skills·CLAUDE.md·AGENTS.md·opencode command·tickets/README + templates)을 새 표기로 정합(drift-0·byte-identical). parity 가드(T-0319·28 테스트)가 도구-간 semantics 동형 + shipped old-flag 부재를 steady-state 로 잠근다(재발 시 red).

### Fixed
- **슬롯-모호 remedy 오안내 근본 소멸** (T-0313 → 통일 흡수) — handoff/ticket_finish·pm_config 의 fail-loud 가 실재하지 않는 플래그를 가리키던 오안내가, 통일된 `--repo`/`--slot` 로 근본 해소.

## [1.1.4] - 2026-07-15

채택자(v1.1.0) 버그 wave — prefix 대소문자 + 세션/슬롯 뷰 격리를 채택자 관점으로 정합. 다 실버그·v1.1.3 재현 확인. dual gate(내부 reviewer + codex) 통과.

### Fixed
- **prefix 대소문자 허용** (ADR-0055 amends ADR-0042) — 생성/rename 검증이 소문자-only 라, 보드에 대문자 prefix 티켓(`T-AAA-*`)이 존재·정상 list 되는데도 `board new --prefix AAA` 를 자기 도구가 거부하던 등록측(`_REPO_NAME_RE`)·파싱측(`_TICKET_PREFIX_RE`)과의 3중 문법 불일치를 정합. prefix 동일성 = **case-insensitive fold**(대문자 허용)·canonical(등록/최초-사용) case **보존**(저장 소문자 강제·ID 재번호 없음 → 기존 대문자 보드 무손실·마이그 0). 등록/rename/merge/delete/repo-add 는 case-only 근접중복 fail-loud. (T-0311)
- **세션/슬롯 뷰 user-first** (ADR-0056 refines ADR-0053) — 필터 뷰(`list --mine`/`--session`/`--slot`)의 querying identity 를 area_owner-derived 에서 **현재 사용자**(`user_name()`)로 고정하고, `--session`/`--slot` 을 **내 것 ∩ 그 슬롯**(claim: user AND slot)으로 좁힌다. **타 사용자는 어떤 필터 뷰에도 안 나온다**(전체는 무필터 `list` 전용). bootstrap `--slot N` 카운트가 슬롯 정체성으로 조회(라벨 "(slot N)")되고 커맨드 카드가 `--mine`(전 슬롯)/`--session`(∩ 이 슬롯) 을 구분한다. legacy 슬롯-only claim 은 진짜 solo(distinct ticket-user AND distinct area_owner 둘 다 ≤1)에서만 slot 매칭 포함(multi-user 는 strict-exclude·`migrate-identity` backfill). 채택자 실측 S1(bootstrap 카운트)·S2(claim 가시성)·S3(필터 축) 종결. (T-0312)

## [1.1.3] - 2026-07-14

multi-PM 다중사용자 격리 robustness 완결 — 값-연결(격리·전파·표기·livegate) 근본 재설계 + 라이브 게이트. 다중사용자 공유 board 에서 타 사용자 미claim 티켓이 세션 뷰에 유출되던 격리 깨짐을 근본 fix 하고, 어댑터 safety-훅이 조용히 낡던 전파 갭을 닫았다.

### Added
- **멀티유저 세션 뷰 격리** (ADR-0053) — 다중사용자 공유 board 에서 세션 뷰(`list --mine`/`--session`/`--slot`)가 타 user 미claim open 을 열람하지 않는다(소유 = area_owner ?? created_by · `_distinct_ticket_users`≥2 strict-exclude · solo 는 all-open degrade 보존). 단일 predicate `_ticket_is_mine`. (T-0302 core · T-0304 기계 격리 게이트[각 슬롯 실 생성→뷰 섞임 검증])
- **라이브 멀티유저 composite 게이트** — release-marked opencode 라이브 테스트가 2 user(alice/bob) 공유 board 에서 각자 티켓 생성 후 세션 뷰 섞임 격리를 실증(비-공허 가드). (T-0309)
- **어댑터 hook/driver 전파** (ADR-0032 Q3 · ADR-0054) — engine-mirror 훅/드라이버(ctx-guard·hard-stop·statusline·회귀 게이트·relay 드라이버)를 `@source` source-remap 채널로 framework-owned 전파 + manifest 자기전파(신 엔트리가 기존 채택자에 도달). 엔진 safety-훅 fix 가 채택자에 닿는 채널을 신설(frozen 근절). (T-0303 core · T-0305)
- **anti-degrade 진단 surface** — `board list` 가 다중사용자 strict-exclude/정체성 미해소 시 stderr loud-warn(remedy 포함·stdout 무오염) · `pm-config status` 가 정체성·isolation posture(registry 기준 · 실 격리는 `board list --mine` 이 authoritative). (T-0307)
- **fresh opencode 채택자 drift-0 e2e** — pm_import↔pm_update 렌더 drift-0(byte-identical) + hook/driver 채택자 도달을 machine e2e 로 박제. (T-0308)

### Fixed
- **opencode self-update @render 비대칭 근본fix** — opencode 없이 import 한 채택자(모델 미해소)의 self-update(`pm_update`)가 `@source` 재렌더에서 미해소 `{{OPENCODE_PRO_MODEL}}` 을 leak 으로 rc-fail 시켜 **엔진 update 까지 전멸**하던 회귀(위 hook/driver `@source` 전파가 유입)를 근본 fix. 줄-중화 로직을 단일 진실(`pm_render.neutralize_model_todo`)로 추출해 import↔self-update 대칭화 — 미해소 모델은 graceful TODO placeholder 로 넘겨(부분-graceful·엔진/타 어댑터 정상 update) self-update 가 성공한다. 렌더러 로드 실패는 fail-loud, `opencode_pro_model=` 빈값(오설정)은 leak 으로 표면화(false-green 근절). 릴리즈 라이브 게이트가 포착한 blocker. (T-0310)
- **livegate check↔record 단일소스** — `livegate check` 도 record 와 동일한 engine-root sidecar 해소를 공유해, 어느 board.py 사본/cwd 로 check 하든 push 보호훅이 기록한 파일을 읽는다. wrong-copy stale 오독(false-green/false-red)을 원천 차단. (T-0306)
- **settings.json auto-compact 토글 중복** — 정본 top-level `autoCompactEnabled` 로 단일화(env `DISABLE_AUTO_COMPACT` 중복 제거) + 출하 template critical env 존재를 검증하는 guard 테스트(권한-승인 재직렬화 드롭 fail-loud). (T-0300)
- **livegate cwd fail-loud + slot-key 표기 정합** — 다중슬롯에서 livegate cwd 해소 모호를 fail-loud, slot-key 표기 sweep. (T-0298 · T-0299)

## [1.1.2] - 2026-07-14

worktree add 타임아웃 false-kill 제거(3-layer) + worktree/lease 견고성(중단-안전·정합) + board submodule 자동 셋업.

### Added
- **`pm-import --new --board-submodule --board-remote <url>`** — `--new` 의 board(tickets+areas)를
  별도 git submodule(`.project_manager/board`)로 자동 셋업한다(두-git 분리·multi-PM 공유 board·ADR-0033).
  빈 remote 는 tickets 폴더구조+areas.md 를 seed(commit+push)하고, 기존 board remote 는 재사용(합류)하며,
  `submodule.<path>.ignore=all` 을 설정한다. 잘못된(비-board) remote 는 명확히 fail-loud. inline 기본은
  완전 무변경. (T-0297)
- **worktree add 타임아웃 튜닝 노브** — 엔진 `PM_GIT_TIMEOUT`(초·`none`=무제한)·claude 하네스
  `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS`·opencode 하네스
  `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` 를 노출한다(기본 30분·`/pm-env` 문서화). (T-0292·T-0293)

### Fixed
- **worktree add false-kill (타임아웃 3-layer)** — 대형 repo 의 `worktree add`(로컬 bare→full checkout·
  느린 디스크/VPN/Windows)가 *진행 중인데도* 짧은 고정 타임아웃(120초)에 죽던 것을 고쳤다. 엔진은
  console-visible 러너 + 관대한 튜닝 가능 타임아웃으로, 그걸 호출하는 하네스(claude·opencode) bash 툴
  타임아웃도 30분으로 상향해 3층 모두 정상-느린 op 을 false-kill 하지 않게 했다. (T-0292·T-0293)
- **부분·깨진 bare mirror 의 조용한 통과** — `repo add`/`worktree add` 가 bare mirror 를 *경로 존재*로만
  판정해, 중단된 clone(하네스 타임아웃·Ctrl-C)이 남긴 부분/빈 bare 를 조용히 재사용하고 나중 `worktree add`
  가 날 git 에러로 죽던 것을, 실 bare 검증(`rev-parse --is-bare-repository` + HEAD 해소)으로 fail-loud
  진단하게 고쳤다(파괴적 자동삭제 없음). (T-0294)
- **create_slot 중단 시 orphan + status 미탐지 + 번호 충돌** — worktree 생성이 외부 중단(Ctrl-C/kill)되면
  장부에 없는 orphan worktree 가 남고 `status` 가 이를 못 보며 다음 슬롯 생성이 번호 충돌하던 것을 —
  provisional lease(중단-안전) + git↔장부 reconcile(orphan/stale/incomplete surface·조회 전용) + 안전
  cleanup(`worktree prune-stale`)로 고쳤다. (T-0295)
- **worktree add → 슬롯 바인딩 안내 누락** — `worktree add` 성공 출력이 다음 필수 스텝
  (`/pm-bootstrap <repo> --slot <N>` 바인딩)으로 이어주지 않던 것을 안내 한 줄로 보강했다. (T-0296)

## [1.1.1] - 2026-07-13

라이브 도그푸딩·채택자 실사용에서 드러난 출하 결함 수정 (버그 wave).

### Fixed
- **opencode 서브에이전트 보고 절단** — opencode 전역 `tool_output` 기본 상한(2000줄/50KB)이
  서브에이전트(task) 반환도 절단해, researcher/reviewer 의 큰 보고가 오케스트레이터에 온전히
  도달하지 못하던 것을 상한 상향으로 해소했다. (T-0289)
- **출하 스킬/커맨드 Windows 표기 파리티** — PM 스킬(claude)·커맨드(opencode)에 Windows 런처
  `py`·`.cmd` 파사드 표기를 통일했다. Windows 세션이 literal `python3`/`.sh` 를 그대로 실행해
  가짜 shim 실패·재시도로 시간을 낭비하던 것을 막는다. (T-0288)
- **livegate record 기록 위치 seam** — 두-git(홈 + worktree) 토폴로지에서 `livegate record` 가
  push 보호훅의 read 위치와 다른 곳에 기록될 수 있어 거짓 성공을 찍고 push 순간에야 드러나던 것을,
  기록 위치를 훅 read 위치와 단일-소스로 정렬하고 불일치 시 fail-loud 로 거부하도록 고쳤다.
  단일-repo 채택자는 무변경. (T-0287)
- **릴리즈 절차 GitHub Release 단계 강제화** — 릴리즈 절차에서 GitHub Release 생성을 필수 단계로
  승격하고 완결 확인 단계를 추가했다. 태그만 push 되고 Release 객체가 누락되던 것을 막는다. (T-0290)
- **공유 채택 폴더의 다중 사용자 repo hydrate** — 하나의 채택 폴더를 여러 사람이 clone 해 쓸 때,
  레지스트리(`areas.md` · git-tracked · 공유)엔 repo 가 등록돼 있으나 bare mirror(`.repos/` · gitignore ·
  per-clone)가 없어 2번째 사용자가 repo 를 받지도 추가하지도 못하던 것을 고쳤다. `pm-config repo add
  <repo>` 가 `--git` 없이도 `areas.md` 에 기록된 URL 로 mirror 를 hydrate 하고(불일치 시 등록 URL 우선),
  `worktree add` 의 mirror-부재 에러가 그 해법을 안내한다. (T-0291)

## [1.1.0] - 2026-07-13

dual-harness(claude·opencode 병행) 채택 지원, worktree/submodule 풀 관리 도구,
스킬-우선 PM 운영 규율(ADR-0052), 그리고 라이브 도그푸딩에서 발견한 출하 버그 수정.

### Added
- **dual-harness 채택** (`pm_config add-harness <claude|opencode>`) — 이미 채택한 인스턴스에 두 번째
  하네스 어댑터를 나란히 추가한다. claude·opencode 를 한 인스턴스에서 병행 운영하고(엔진 공유·어댑터층만
  분기), imported 인스턴스는 소스를 upstream fallback + `--from` 으로 해소한다. (T-0269/0270/0271/0282)
- **worktree/submodule 풀 관리** — `pm-worktree` 스킬 + `worktree_pool.py`(`dev`/`sync` 서브커맨드):
  pool 의 submodule 을 selective 재동기하고, 작업 중 submodule 을 dev 브랜치로 지정해 재동기로부터
  보호하며, drift 난 detached submodule 을 pin 으로 수동 재동기한다. 부트스트랩이 슬롯 브랜치·upstream·
  submodule status 를 surface 한다. (ADR-0049/0050/0051·T-0275/0276/0277/0278)
- 릴리즈 라이브 게이트에 worktree/dual-harness 시나리오를 반영하고 케이스 수집 pin 을 cascade 했다. (T-0278)

### Changed
- **스킬-우선 PM 운영 규율** (ADR-0052) — PM 운영단계(claim/finish/qa/dev-delegate/handoff)는 스킬로
  invoke 하고 backbone CLI 직접 우회를 금지한다. `pm_role` 에 규율을 명문화하고, 부트스트랩 커맨드 카드가
  스킬-우선을 반영하며, durable 회귀 가드로 못박았다. (T-0279/0280/0281)
- 용어 정합 sweep — 잔여 표현을 표준으로 통일했다. (T-0268)

### Fixed
- **opencode ctx-guard 플러그인 로드** — 플러그인 export 를 함수로 교정(ESM shim + `lib/` core 분리)해
  실 opencode 세션에서 정상 로드되도록 했다(이전엔 유닛만 green·라이브 세션에선 로드된 적 없음). (T-0283)
- **부트스트랩 fresh-slot self-sufficiency** — 새 슬롯 부트스트랩 출력의 스크램블 placeholder 를 제거해
  첫 세션이 자족적으로 시작하도록 했다. (T-0284)
- **ticket_finish 두-git seam** — 다중슬롯에서 회귀 cwd 해소가 모호하던 것을 `--session`/`--no-pytest`
  로 해소했다(ADR-0027). (T-0285)
- **worktree/repo origin-freshness** 2건 — 슬롯·repo 의 upstream 신선도 판정 버그를 고쳤다. (T-0273/0274)

## [1.0.6] - 2026-07-10

세션 정체성 인자의 canonical 통일, 멀티-PM 차수·워크스페이스의 슬롯별 격리,
부트스트랩 커맨드 카드, 채택자 진입문서·어댑터 정합 수정.

### Added
- **`board.py reid <OLD-ID> <NEW-ID>`** — 오발행 티켓의 ID(번호·prefix 부여/변경/제거)를 무손실
  재부여한다. 파일명·frontmatter 는 물론 전 참조(`depends_on`/`blocks`·본문 wikilink·slug
  파일명·`wiki`/`log`)를 토큰 단위로 정확히 rewrite 한다. collision 시 중단, `--dry-run` 미리보기,
  board-git 백업, 홈 git clean 요구, 다른 세션이 claim 중이면 중단, 멱등.
- **부트스트랩 커맨드 카드** — 부트스트랩이 이 세션이 쓸 커맨드를 정체성 실값으로 채운 완성형
  카드로 dump 한다(남는 자리는 사용자가 넣을 `T-NNNN`·`<PFX>` 같은 값뿐). 숨은 전제(claim 은
  promote 선행 · prefix 조작은 홈 git clean · livegate record 는 케이스 수 pin · migrate-identity 는
  단일 세션) 경고와 '정체성이 필요 없는 커맨드' 목록, '상황→소스' 포인터를 담아 `--help` 없이
  바로 칠 수 있다.
- **`pm_handoff --normalize-session-anchors [--dry-run]`** — `pm_state` 의 차수 앵커 오형식(`N차차`)을
  `N차` 로 정규화하는 멱등·비파괴 유지보수 도구. 파서를 관대하게 만드는 대신 데이터를 원천에서
  정규화한다.
- **멀티-PM slot 대시보드** `wiki/log/dashboard.md` — 핸드오프가 자기 섹션(키=세션 정체성)만
  overwrite 하고(3~5줄 상한·다른 섹션은 byte 불변·append 아님), 부트스트랩이 '다른 활성 PM' 섹션을
  가볍게 dump 한다. 런타임 파생물이라 gitignore 되고 출하물에 포함되지 않는다. 솔로면 건너뛴다.
- 릴리즈 라이브 게이트(`PM_ORCH_LIVE_RELEASE=1`)에 **커맨드 카드 기반 사용성 시나리오** 2건 추가 —
  실 LLM 이 카드만 보고 첫 시도에 커맨드를 성공시키는지 두 하네스에서 확인한다(라이브 케이스 7→9).

### Changed
- **세션 정체성 인자를 canonical 하나로 통일** — 정체성을 받는 커맨드가 `--session <repo>_<N>`
  (정체성) · `--session-seq N`(차수) 표기로 일원화됐다. `pm_handoff` 에 `--session`·`--session-seq` 를
  신설했고, 솔로(미지정) 경로는 동작이 바뀌지 않는다. canonical 과 구형 alias 를 함께 주고 값이
  다르면 명확히 실패한다.
- **차수(PM N차)를 전역 카운터에서 슬롯별 시퀀스로 격리** — 핸드오프 로그 헤더에 정체성 태그
  `PM N차 (<repo>_<N>)` 가 붙고(솔로는 태그 생략 = 기존 헤더와 byte 호환), 부트스트랩이 자기 슬롯
  태그 entry 만 필터해 차수·인계 본문·`pm_state`·reattach 를 복원한다. 멀티-PM 두 슬롯이 같은
  N차 를 주장하던 문제가 사라진다. 식별 불가 시 기존 전역 동작을 보존한다.
- **멀티-PM 기본 규율을 '자기 공간 우선' 으로 확정** — 자기 티켓은 `board list --mine`, 상태는
  per-slot `pm_state`, 인계는 자기 슬롯 태그 handoff entry 로 운영하고, 다른 PM 과는 대시보드로만
  공유한다.
- **`pm_role.md` 축약** — 커맨드 표기를 부트스트랩 카드에 위임하고, 필독 문서를 `CLAUDE.md` +
  per-slot `pm_state` + `/pm-bootstrap` dump 셋으로 줄였다(`status`·`architecture` 는 '필요시 조회').
  '찾아가는 법' 절을 신설했다.
- opencode 라이브 모델 예시를 `ollama/glm-5.2:cloud` 로 교체 — `pm_import` 의 seed 주석과
  `--opencode-model` 도움말 예시 문자열.

### Deprecated
- `pm_handoff --worktree-slot` → **`--session`**. 구형 플래그는 무기한 alias 로 계속 수용된다(기존
  스크립트 무파손). canonical 과 값이 다르면 명확히 실패한다.
- `pm_handoff --session-num` → **`--session-seq`**. 무기한 alias 로 수용, 불일치 시 실패. 차수 인자를
  rename 한 것은 정체성 `--session` 과의 명명 충돌을 피하기 위함이다.

### Fixed
- `pm_bootstrap` 이 `/pm-bootstrap <repo> --slot N` 의 positional `<repo>` 를 수용한다 — 핸드오프가
  찍어주던 커맨드와 raw CLI 의 불일치를 수리했다(`--repo` 와 alias·둘 다 주면 값 일치 필수·
  무인자 자동바인딩은 그대로).
- 진입문서와 어댑터 카드·위임 스킬의 세션명 지시를 canonical `<repo>_<N>`(솔로는 `--session` 생략)로
  정합 — opencode 의 하드코딩 `--session pm`·산문형 `` `pm` 세션 `` 과 claude 진입문서의 자유형
  `--session session-B` 를 제거했다. 하드코딩 세션명은 repo 유도를 조용히 건너뛰게 만든다. 재유입은
  표기 형태(인자형·산문형·괄호형)에 무관하게 한 규칙으로 막는 테스트로 봉인했다.
- 진입문서와 티켓 안내의 세션 식별 우선순위 서술을 실제 코드 동작에 맞췄다 — 없어진 `<hostname>-<pid>`
  정체성 폴백을 빼고, 활성 슬롯이 하나면 그 세션으로 해소하는 단계와 슬롯이 여럿이면 저장값을 건너뛰어
  오귀속을 막는 규칙을 반영했다.
- `board.py list` 와 보드 렌더가 숫자 태그(`tags: [2026, cleanup]`)에 크래시하던 것을 고쳤다 — 태그를
  문자열로 안전하게 처리한다. `--tag` 필터도 숫자 태그를 매치한다.
- board CLI `--help` 위생 — ticket 인자 metavar 를 `T-NNNN` 으로 표기하고, `new --prefix` 도움말을
  작업 카테고리 재정의에 맞게 갱신했다. `list --session`(뷰 렌즈)이 쓰기 주체 `--session` 과 별개라는
  주의문도 다시 썼다. 핸들러 동작은 바뀌지 않았다.
- `pm-config init`/`update` 의 usage 줄이 내부 파일명(`board.py`·`pm_update.py`) 대신 실제 커맨드명
  (`pm-config`)으로 표기된다 — 에이전트가 칠 커맨드를 오인하지 않게.
- 채택자 진입문서·스킬 정합 — pm-regression 스킬이 존재하지 않는 커맨드(done→open 복구)를 안내하던
  것을 정직하게 표기하고, pm-wave-claim 의 필수 섹션을 6→3(목표·완료 조건·참고)으로 정정했다.
  ADOPT 하네스 기본값을 `claude` 로 명시하고, 하네스별 ctx 예산 키를 진입문서에 반영했다.
- opencode 어댑터 정합 — researcher 출하 파일 말미의 스트레이 태그 2줄 제거, README 의 서브에이전트
  개수 undercount 정정, 인스턴스 소유 루트 `.gitignore` 신설(claude 파리티). opencode 의 `--opencode-model`
  예시와 `spike-new` 커맨드의 설계 스파이크 생애주기(초안 편집 → 봉인 → 이후 불변) 서술을 claude 쪽과
  맞췄다 — opencode 채택자가 옛 모델 예시·옛 봉인 모델을 받던 것을 정정했다.

## [1.0.5] - 2026-07-07

하네스별 ctx 예산 분리, 티켓 prefix 의 작업-카테고리 재정의와 관리 도구, 채택자 제보 결함 수정.

### Added
- **`board.py prefix` 관리 도구** — `list`(현황: prefix 별 개수·번호 범위) ·
  `rename <A|none> <B|none>`(카테고리 개명·이름 씌우기/지우기) · `strip <A>`(=rename A none) ·
  `merge <A> [B...] --into <T|none> [--reorder-chronological]`(created 순 통합·기본 append) ·
  `delete <A>`(빈 prefix 등록 제거). 전 동사 `--dry-run` 규모 미리보기, 참조 rewrite 는
  전 표기형(frontmatter·wikilink·본문·파일명) 토큰 단위 정확 치환, collision 시 중단,
  board-git 백업 커밋, 티켓 물리 삭제 없음(무손실 relabel). 혼재 보드(legacy `T-NNNN` +
  prefixed)를 시간순으로 합칠 수 있다.
- ctx 예산 **하네스별 오버라이드 키** — `ctx_window_tokens_claude` / `ctx_window_tokens_opencode`.
  한 repo 를 claude·opencode 로 동시 운용할 때 하네스별로 다른 예산을 준다. 미설정 시
  generic `ctx_window_tokens` → 200000 순으로 해소된다.
- `pm_log.py archive --keep-last N` — 날짜 대신 개수 기준으로 최근 N entry 만 남기고 봉인.
- prefix 사용 가이드(`pm_role.md`) — 언제 prefix(배타 카테고리)/tag(겹침 속성)/none(기본)을
  쓰는지, 남발 방지 수칙, 어댑터 마이그레이션 절차.

### Changed
- **티켓 prefix 의 의미를 재정의** — repo 네임스페이스 전용에서 **작업 카테고리**(M 무관·
  자유 입력·티켓당 1개)로. `repo add` 의 repo 명 prefix 자동 시드를 폐지하고, 명시
  `--prefix` 의 "등록값 강제"를 제거했다(형식 sanity `^[a-z0-9][a-z0-9_]*$` 와 예약어
  `none` 거부만 유지).
- **ctx 정지/넛지의 분모를 해소된 예산 하나로 통일** — claude statusLine(물리 window %
  표시 폐기)·claude hook·opencode plugin(`modelLimit()` 물리한도 조회 폐기)이 전부 같은
  예산을 쓴다. 표시와 정지가 같은 숫자로 움직인다. 큰 window 는 예산 키를 명시한다.
- domain 스캔이 frontmatter 없는 `.md`(tmp·메모)를 개별 경고 없이 조용히 건너뛰고
  디렉토리별 개수 요약 1줄만 남긴다(malformed 는 개별 경고 유지).

### Fixed
- 채택자 제보 결함 — `external_review.py`/`ticket_finish.py` 의 repo 루트 하드코딩
  (`.project_manager` 마커 상향 탐색으로 교체·venv 부재 폴백 명문화) · `pm_handoff` step3
  앵커 정확-일치 실패 시 핸드오프 전체가 죽던 것(정규화 부분일치 + fail-soft 로 완주) ·
  prefixed 티켓 ID lint 정합(회귀-lock).
- non-UTF-8 파일이 domain 스캔·참조 rewrite 를 크래시시키던 경로(graceful skip + 경고).
- relabel(대량 ID 변경)의 동시성 — 스캔·충돌 검사·적용 전체를 board 락 안 fresh snapshot
  으로 직렬화하고, 적용 직전 대상 경로 점유를 재검증해 덮어쓰기를 원천 차단.

## [1.0.4] - 2026-07-03

세션 정체성 유도 전환과 사람-친화 문서 개편.

### Added
- CHANGELOG(이 파일)와 GitHub Releases — 릴리즈 절차에 노트 단계가 포함된다(v1.0.0~1.0.3 소급).
- MIT 라이선스.
- README 전면 개편 — 문제(compaction)와 해결을 앞세운 사람-친화 구성, 절별 프롬프트 예시,
  Mermaid 다이어그램. 기계 절차 reference 는 `docs/` 4파일로 분리.

### Changed
- **세션 정체성·티켓 prefix 를 저장값에서 유도값으로 전환** — `local.conf` 의
  `session=`/`prefix=` 는 solo 전용 legacy 로 강등. 활성 슬롯이 정확히 1개면 그 세션으로
  자동 유도되고, 여러 개면(멀티 홈) 명시 없는 귀속 조작이 명확히 실패한다(silent 오귀속
  차단). 멀티 홈은 두 키를 제거해도(남아도 무시) 동작이 같다.
- 멀티 홈의 push 회귀 게이트가 **전 활성 슬롯 all-or-nothing** 으로 동작한다 — 기록 확인
  우선(저비용)·미검증 슬롯만 실행·하나라도 red 면 차단. 게이트 좁히기는 CLI `--session`
  명시로만 가능하고 환경변수로는 좁혀지지 않는다.
- 티켓 prefix 는 areas.md 의 repo 등록이 단일 진실 — 등록 repo 가 1개면 자동 적용되고,
  여러 개면 세션에서 유도하며, 모호하면 발행이 명확히 실패한다.
- Windows PowerShell 안내 정합 — `.\pm-config.cmd`/`.\pm-update.cmd` 진입을 문서에 표기하고,
  PowerShell 5.x 의 `&&` 체이닝 미지원(ParseError) 주의를 진입 문서에 추가.

### Fixed
- 멀티 슬롯 홈에서 비바인딩 세션이 `local.conf` 의 세션명을 물려받아 남의 세션으로
  자기 식별하던 문제.
- 미바인딩 상태의 `repo add` 가 등록 owner 를 문자열 "None" 으로 기록할 수 있던 경로 —
  부작용 전에 명확히 중단한다.

## [1.0.3] - 2026-07-03

게이트 하드닝 + 릴리즈 단일 라이브 게이트(기계 강제).

### Added
- `board.py livegate record` / `check` — 릴리즈 라이브 테스트 wave 를 실측하면 특정 엔진
  rev 에 pin 된 기계 검증 green 마커가 기록되고, 보호 브랜치 pre-push 훅이 릴리즈 push 전에
  이를 소비한다.
- 부트스트랩이 git freshness 를 surface — fetch 해서 upstream 대비 얼마나 뒤처졌는지 보고하고
  안전한 fast-forward 동기를 안내한다. fresh-clone 연속성 포함.

### Changed
- 라이브 테스트를 릴리즈 단일 게이트로 통합 — 별도 "shipping" tier 는 폐지. 보호 브랜치 push 는
  push 대상 rev 에 기록된 라이브 green 을 요구한다(라이브-무관/핫픽스 변경만 문서화된 우회 허용).
- 핸드오프가 더 이상 차단형 shipping 테스트를 돌리지 않는다 — 비차단 1줄 안내로 대체되어
  핸드오프가 다시 빨라졌다.
- 렌더된 어댑터 파일이 머신-불변이 됐다(인터프리터·테스트 명령 placeholder 중립화) —
  clone 간 재렌더가 파일을 더 이상 뒤흔들지 않는다.

### Fixed
- `pm_render` 가 누락된 `local.conf` 키를 빈 문자열로 조용히 치환하지 않는다 — 명확히
  실패하거나 경고한다.
- 수집된 테스트가 0개일 때 회귀 실행이 false pass 를 기록하지 않는다.

## [1.0.2] - 2026-07-02

Windows 전체 지원.

### Added
- Windows 지원 — 프레임워크가 Windows(네이티브·Git Bash)에서 동작한다: 인터프리터 해소가
  `py` 런처를 우선하고, UTF-8/cp949 인코딩을 엔진 코드가 처리하며, 셸 facade 와 git 훅이
  정확히 실행된다.

### Changed
- ticket claim 이 Windows 에서도 직렬화된다 — 동시 세션 간 claim 배타성 보존.
- context-guard 임계를 상향해 hard stop 전 여유를 늘렸다.

### Fixed
- board git-sync 가 detached HEAD 를 오프라인으로 오진하지 않고, orphan 커밋을 조용히
  누적하지 않는다.
- hard-stop 이 핸드오프 자체를 잠그지 않는다.
- Windows 경로 처리 정규화(render·update 의 백슬래시 경로).

## [1.0.1] - 2026-07-01

### Added
- graceful handoff nudge — 컨텍스트 예산 nudge 임계에서 비차단 안내가 현재 단계 마무리와
  핸드오프를 유도한다(hard stop 전·Claude Code + opencode).

### Changed
- hard-stop 이 새 작업만 정지한다 — 핸드오프 도구는 예외 통과라 세션이 항상 깨끗하게
  인계할 수 있다.
- 엔진 freshness 는 git rev-baseline 단일 추적 — 중복 버전-파일 마커와
  `pm_update --version` 플래그 제거.

### Fixed
- `board init` / `pm-config init` 재실행이 `local.conf` 를 덮어쓰지 않는다 — 사용자·운영
  설정을 비파괴 병합한다.
- multi-PM 핸드오프 프롬프트에 대상 슬롯이 포함된다 — 다음 세션이 모호함 없이 worktree 를
  해소한다.

## [1.0.0] - 2026-06-28

첫 안정 릴리즈.

### Added
- PM 오케스트레이션 프레임워크: ticket 보드 + wiki 지식 베이스 + ADR(Architecture Decision
  Records) + 살아있는 domain 지식 레이어.
- 멀티-하니스 지원 — Claude Code·opencode 어댑터가 하나의 엔진을 공유.
- 멀티-PM 운용: 여러 저장소 × 여러 PM 세션, worktree 풀 + 슬롯 lease 기반.
- self-sufficient 부트스트랩·핸드오프 — 새 세션이 자기 컨텍스트(슬롯·차수·직전 인계)를
  자동 해소해 컨텍스트 한계를 넘어 연속성이 유지된다.
- 이중 게이트 코드리뷰 + 3-tier 테스트 워크플로(단위 회귀·smoke·라이브 릴리즈 wave).
