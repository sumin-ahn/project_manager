---
name: pm-review
description: "추가 리뷰어(additional reviewer) 교차검증 게이트 실행 규율 명령어化 — worktree cwd 앵커 + stage 선행(git add) + --paths/--ticket 경로 핀. backbone = additional_reviewer.py(opt-in). 앵커는 명시 selector 기반 diff_root 해소(등록 슬롯 lease 장부 자동 파생)·소유 PM 홈 해소 불가 무인자 실행은 rc=1 차단. 외부 전송은 load-bearing 게이트에만(사소 docs 는 self/내부 리뷰). Triggers: '추가 리뷰어', 'codex 게이트', '추가 교차검증', 'additional review 돌려', 'pm-review'."
audience: pm-internal
---

# $pm-review — 추가 리뷰어 교차검증 게이트

backbone은 `.project_manager/tools/additional_reviewer.py`(opt-in)이며, PM이 추가 리뷰어 게이트를 실행할 때 사용한다. 역할 이름은 **추가 리뷰어(additional reviewer)** 이고 설정 키도 `additional_reviewer.enabled`·`additional_reviewer.*` 로 통일돼 있다. 신규 CLI·모듈·설정·raw 표면의 canonical은 `additional_reviewer`다. 개칭 전 `external_review`는 구 설정·raw header/prefix·round role 판독과 퇴역 이주를 위한 read-only 호환 이름으로만 남으며, 구 실행 파일은 다시 만들지 않는다. 구키를 쓰는 `local.conf` 는 실행 시 안내 1줄을 받는다(마이그레이션 절차는 README).

수신자 프로필은 `local.conf` 의 원자적 튜플 하나다.

```
additional_reviewer.enabled=true
additional_reviewer.harness=codex
additional_reviewer.model=gpt-5.6-sol
additional_reviewer.reasoning=max
```

opt-in 질문은 **첫 1회**뿐이다. `additional_reviewer.enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 **지속 동의**이므로, PM은 리뷰마다·라운드 상한 재개마다 사용자에게 비용을 다시 묻지 않는다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 운영 상세는 [references/operational-details.md](references/operational-details.md)를 해당 상황에서 읽는다.

## 사용 시점

- **묶음 code-reviewer 라운드와 같은 시점**에, **실질 코드·설계**(엔진/알고리즘·비파괴 동작·파서·보안·ADR/설계)에 추가 리뷰어 교차검증을 병행한다. 이 채널의 대상 지정은 티켓별(`--ticket`)이며 묶음 인자를 받지 않는다 — 묶음이면 멤버마다 실행하거나 `--paths` 로 묶음 변경 경로를 핀한다.
- **사소한 docs/prose/자명한 편집에는 실행하지 않는다**. self/내부 리뷰로 끝낸다.
- 코드 리뷰 라운드는 1회다. reviewer는 severity와 무관하게 모든 finding에 fix가 실행할 수정·테스트 계약을 채운다.
- 수렴 불변식은 [`pm_principles.md`](../../../.project_manager/wiki/pm_principles.md) §"티켓과 위임"이 단일 진실이고, 실행 절차는 `pm_playbook.md` §"라운드 프로토콜"을 따른다.

## 실행 규율

아래 순서를 지킨다. 위반하면 false-green(빈 diff 통과) 또는 stale 결과가 난다.

### 1. worktree cwd 앵커

실 코드가 변경된 worktree cwd의 canonical `additional_reviewer.py`로 실행한다(엔진 갱신 동기 전 PM 홈 import 사본은 stale 일 수 있다). 앵커 해소는 명시 selector 기반이다 — `--ticket`/`--paths` 를 주면 엔진이 lease 장부에서 diff worktree 를 자동 파생하므로 PM 홈 cwd 에서도 올바른 슬롯으로 해소된다(provenance 첫 줄의 `diff_root` 로 확인).

```bash
# canonical 코드 worktree 에서 실행 (PM 홈 import 사본 금지).
cd <worktree-canonical-경로>     # 예 work/project_manager_1
```

소유 PM 홈을 해소할 수 없는 위치의 **무인자 실행은 rc=1 로 차단**된다(경고 후 진행 없음). 커밋만 된 변경으로 슬롯을 고르려면 `--base` 앵커를 명시한다. 복구 채널은 `--paths`(절대경로)·유효한 `--ticket` 이다.

### 2. stage 선행

additional_reviewer는 `git diff` 기반이라 untracked(신규) 파일을 보지 못한다. 검토 전에 스테이징한다.

```bash
git add <신규/변경 경로>     # untracked 파일이 diff 에 포함되게
```

### 3. 경로 핀

ticket의 `touches`로 정하려면 `--ticket`, 직접 지정하려면 `--paths`로 리뷰 대상을 핀한다.
`--paths` 실 전송에는 게이트 지정(`--gate <게이트>`) 또는 명시적 `--no-gate`가 필요하다.

```bash
# ticket touches 로 경로 결정 (권장 — DoD/touches 와 정합 · 게이트는 티켓으로 자동 유도·기록)
python3 .project_manager/tools/additional_reviewer.py --ticket T-NNNN

# 또는 경로/base 직접 지정 (실 전송 회계 선택: 여기서는 --gate)
python3 .project_manager/tools/additional_reviewer.py --base main --paths src/ tests/ .project_manager/tools/ --gate T-NNNN
```

- `--gate T-NNNN`: 게이트 명시 override(자동 유도보다 우선). `--ticket` 실행은 생략 시 티켓으로 유도·기록된다.
- `--no-gate`: 게이트 회계 opt-out(장부 미기록·예산 미소모·loud 표기). `--gate` 와 함께 쓰지 못한다.
- `--adr ADR-NNNN …`: 관련 ADR을 프롬프트에 포함.
- 외부 전송 없는 미리보기: `--dry-run`.
- 비활성 상태 1회 강제: `--force`.

### 4. Codex egress 건별 승격 (load-bearing)

Codex 출하 기본은 `workspace-write` + `network_access=false`다. 추가 리뷰어 호출도 같은 sandbox를 상속하므로, Codex PM의 실 전송은 아래 두 계층을 **항상 동반**한다. 전역 `sandbox_workspace_write.network_access=true`로 이 문제를 회피하지 마라 — 전역 egress는 계속 꺼진 채로 둔다.

1. 먼저 일반 sandbox에서 위 명령의 `--dry-run`을 실행한다. 해소된 추가 리뷰어 프로필·앵커·전송 대상 diff를 확인하고, `Codex egress: escalation required`가 표시되는지 본다. dry-run은 외부 송신·라운드 예약·과금을 하지 않는다.
2. 승격이 필요한 실 명령은 Codex `exec_command` 호출에 `sandbox_permissions="require_escalated"`와 기술적 network `justification`을 주고, 명령 argv에 `--codex-egress-escalated`를 동시에 추가한다.

```text
exec_command(
  cmd="python3 .project_manager/tools/additional_reviewer.py --codex-egress-escalated --ticket T-NNNN",
  workdir="<worktree 절대경로>",
  sandbox_permissions="require_escalated",
  justification="설정된 추가 리뷰어 호출에 필요한 network를 sandbox 밖에서 허용합니다.",
  prefix_rule=["python3", ".project_manager/tools/additional_reviewer.py"],
)
```

`--codex-egress-escalated`는 권한을 만드는 플래그가 아니라 호출층 attestation이다. 단독으로 샌드박스 명령에 붙이지 말고 반드시 위 `sandbox_permissions` 메타데이터와 같이 쓴다. 최초 승인은 위의 좁은 reusable `prefix_rule`로 기억할 수 있다. Python 전체나 인자 전체를 prefix로 승인하지 마라. Windows의 동일 좁은 prefix는 `prefix_rule=["py", ".project_manager/tools/additional_reviewer.py"]`이며, 복사용 재실행 명령도 같은 `py + script` 2 token으로 시작해야 한다.

`additional_reviewer.enabled=true`는 설정된 추가 리뷰어의 외부 전송과 통상 과금에 대한 지속 의사표시이므로 PM은 후속 호출마다 비용을 다시 묻지 않는다. 승인이 거절되거나 실행이 `rc!=0`으로 끝나면 그 게이트는 실패다 — 사용자에게 보고하지 않고 native Codex/GPT 자평으로 무음 대체하지 마라.
