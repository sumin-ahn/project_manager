# $pm-worktree 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## ⚠ rebase 선행조건 (활성 위임 중 금지)

**활성 백그라운드 위임(dev 서브에이전트)이 돌고 있는 슬롯은 rebase 하지 마라.** 하네스 안 프로세스라
엔진이 감지하지 못하며 working tree 이동이 위임 작업을 깨뜨린다. PM은 실행 전 활성 위임이 없는지
확인하고, 대여 슬롯이 dev 위임 중이면 종료 후 rebase한다. 엔진은 dirty·rebase-진행중만 기계로
스킵한다.

## 결정 (모델)

- submodule 역할은 live git HEAD로 판별한다: on-branch=dev(보호), detached=consume(재동기 대상).
- 전역 `submodule.recurse=true`를 쓰지 않고 selective resync와 dirty 가드로 작업을 보호한다.
- base는 rebase로만 바뀌는 기대 축이다. 미기록이면 추론하지 말고 사용자에게 확인해 `set-base`한다.
- rebase는 base가 없으면 거부한다. `--onto`의 base 기록과 base·head·recorded_at 원자 갱신은 성공
  시에만 하며, 충돌 상태는 abort하지 않고 사용자가 해소한다.
- readonly 공유 슬롯은 detached·배타 대여 없음·session/pid 없음이며 읽기 기준면이다. 소비자는 슬롯을
  읽고 쓰기는 PM 홈 wiki에 한다. 슬롯 git mutation은 거부하고 `refresh`(fetch→detach 이동,
  dirty=거부)만 허용한다.

## 잔여 PM 손

- 각 backbone 명령의 stdout(실행·skip 사유·경고·조회·rebase 요약·재기록 branch/head·전환 형태)을
  읽고 사용자에게 보고한다.
- `dev` 지정 뒤 실제 submodule 편집·커밋은 PM/사용자가 한다.
- 부트스트랩 0단계의 base 후보를 사용자에게 전달하고, 선택된 기준만 `set-base`로 기록한다.
- rebase 전 활성 dev 위임이 없는지 확인한다. 충돌 시 상태를 그대로 두었음을 알리고
  continue/abort 해소를 사용자에게 위임한다.
- readonly 슬롯 생성(`$pm-env worktree add <repo> --readonly`)은 코드 전체 사본을 만드는 사용자
  승인 flow이므로 PM이 자율 생성하지 않는다.

## 참고

- backbone: `.project_manager/tools/worktree_pool.py`(`dev`/`sync`/`_resync_submodules_selective`/
  `set_base`/`slot_git_status`/`status`/`resolve_rebase_base`/`rebase`/`refresh`/`create_slot(readonly=)`/
  `record_git_snapshot`/`switch`).
- 라이브 하네스 테스트 = (실 LLM 시나리오 → 실 git 단언).
