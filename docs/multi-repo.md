# multi-repo (N×M) 운용 — `pm-config` 파사드 (ADR-0011·0014·0016)

> 루트 [README](../README.md) 의 *여럿이 같이 쓰면* 절의 상세 운용. PM 홈을 만든 뒤(5분 여정),
> 여기서 프로젝트 repo 를 attach 한다 — *홈 생성 → 프로젝트 attach* 단일 채택 서사(ADR-0026 비임베드).
> 홈은 M=1 이어도 worktree 로 프로젝트를 잡는다.

모드 = **multi-PM(N 세션 × M repo·ADR-0016)** 한 개념. N=1·M=1 도 그 부분집합이다(슬롯 오버헤드 0). 한
*사용자*가 여러 repo 를 묶어 운용할 때(M>1 = **single-user multi-repo** 로 재정의·ADR-0016
가 ADR-0011 amend), 셋업·조회·진단은 루트의 `pm-config.sh`(`/.cmd`) 한 파사드로 한다:

```bash
<manager>/pm-config.sh repo add <name> [--git <url>] [--test "<cmd>"]  # repo 등록 + .repos clone (신규=--git 필수 / 기등록 repo 는 --git 없이 areas URL 로 mirror hydrate)
# 등록 owner 는 --owner, 없으면 local.conf identity.user=, 그것도 없으면 git user.email 이다.
#   셋 다 없으면 아무것도 하지 않고 멈춘다.
<manager>/pm-config.sh worktree add <repo> --user-ack <repo>       # 사용자 승인값에 결속된 새 worktree 슬롯 + submodule init
<manager>/pm-config.sh alloc <repo> --task <task>                  # idle 최소 번호 슬롯을 task 명의로 대여
<manager>/pm-config.sh status | whoami                             # 풀/리스 + 이 세션 repo/슬롯/branch
<manager>/pm-config.sh release <slot> [--force]                    # 작업완료 반납 / 수동 강제(백스톱)
<manager>/pm-config.sh update [--from <upstream>]                  # 엔진 갱신 (pm-update 흡수)
```

`worktree add`는 `--readonly`·`--task` 변형을 포함해 물리 슬롯을 늘리므로 항상 사용자가 직접
승인하고, `--user-ack` 값은 대상 repo와 정확히 같아야 한다. 세션은 풀 소진 시 이 플래그를
자가 부착하지 말고 먼저 사용자에게 슬롯 생성 승인을 요청한다.

슬롯 대여는 `pm-config alloc <repo> --task <task>`, 반납은 `pm-config release <slot>`(`--force` 는 강제
반납)이다. 부트스트랩·핸드오프는 슬롯을 빌리거나 반납하지 않는다. 슬롯에 새 브랜치는
`pm-config task branch`, 있는 브랜치로 전환은 `worktree_pool.py switch` 다. 저장소가 1개여도 작업
폴더는 이 파사드(`worktree add`·`alloc`)로 만든다.
