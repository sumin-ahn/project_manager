# Claude Code 어댑터 타깃

Claude Project Framework 의 **Claude Code 어댑터** 타깃. 루트 엔진(`.project_manager/`)을 공유하고
어댑터층(`.claude/`·`CLAUDE.md`)만 이 타깃에서 다르다. (ADR-0005)

> 프레임워크 **전체 가이드**(네 기둥·도입 절차·placeholder 표·워크플로·이식성 등급·계보)는
> 하니스 무관 공통 문서 — **루트 [`README.md`](../../README.md)**. 이 문서는 *Claude Code 어댑터
> 고유분*만 담는다 (opencode 타깃 [`../opencode/README.md`](../opencode/README.md) 과 대칭).

## 어댑터층

claude_code LLM 세션이 `CLAUDE.md` 를 진입으로 PM 을 구동한다 — opencode 의 `AGENTS.md`+`.opencode/`
에 대응하는 Claude Code 등가물. 엔진은 루트와 공유하고 여기 어댑터만 타깃 고유다.

- **`CLAUDE.md`** (진입) — 세션 부트스트랩·작업 원칙·자주 쓰는 명령. Claude Code 가 자동 로드.
- **`.claude/agents/`** — researcher · architect · developer · code-reviewer 서브에이전트 정의.
- **`.claude/skills/`** — PM workflow slash command (`/pm-bootstrap` · `/pm-wave-claim` · `/pm-dev-delegate` ·
  `/pm-wave-finish` · `/pm-handoff`). 목록·역할 단일 진실 = `pm_role.md` §"skill 카탈로그".
- **`.claude/settings*.json`** · **`run_tests_hook.sh`** — PM 세션 권한 + 파일 편집 시 회귀 hook.

### 위임 기제 = `Agent` 툴 `subagent_type`

PM(메인 세션)이 `Agent` 툴을 `subagent_type=developer|code-reviewer|architect|researcher` 로
호출하면 `.claude/agents/*.md` 정의가 별도 자식 세션에서 구동된다. 표준 위임 프롬프트는
`/pm-dev-delegate` skill. (위임 *개념*·generate≠evaluate 는 루트 README 사용법·특징 절.)

## 채택 (pm_import — 정규 경로)

manager 루트의 `pm-import.sh`(`/.cmd`) 파사드로 한다 (default harness = all;
`--harness`를 생략하면 등록된 어댑터 전체를 채택):

```bash
<manager>/pm-import.sh --new <dest>             # 신규 프로젝트 + 전체 어댑터 (default all)
<manager>/pm-import.sh --into <dest>            # 기존 프로젝트에 전체 어댑터 도입 (비파괴·충돌 백업)
<manager>/pm-import.sh --new <dest> --dry-run   # 전체 어댑터 적용 계획만 — 파일 미변경 (권장)
```

(Windows 는 `pm-import.cmd`. `--from` 은 manager 루트 auto-default.) 파사드 없이 푸는 수동
longhand·placeholder 표는 루트 [`docs/manual-import.md`](../../docs/manual-import.md)·[`docs/placeholders.md`](../../docs/placeholders.md).

### trust 확인 (미승인 시)

이 디렉토리를 아직 trust 승인하지 않았다면 출하 `.claude/settings.json`의
`permissions.allow`가 적용되지 않아 전역 설정에 의존한다. 콘솔의
`Ignoring N permissions.allow entries` 경고가 이 상태의 실측 신호다. 첫 대화형 `claude`
세션에서 trust 다이얼로그를 수락하면 적용된다. import는 이 보안 경계를 자동 승인하거나
`~/.claude.json`을 조작·검사하지 않는다.

## Context safety: native compaction + checkpoint

Claude Code의 native auto-compaction을 켠 채 사용한다. 메인 PM 세션의
`PreToolUse`/`UserPromptSubmit` 훅은 컨텍스트가 nudge·강화·final 밴드에 들어갈 때
`additionalContext`로 checkpoint 안내를 사이클당 한 번씩 주입한다. 모든 밴드는 비차단이며 prompt,
도구 실행, compaction을 거부하지 않는다. `PreCompact`는 durable breadcrumb와 checkpoint 골격을
만들고, `PostCompact`는 엔진 `pm_log.py snapshot`의 최종 텍스트를 payload marker에 저장한다. 직후
첫 `PreToolUse`/`UserPromptSubmit`이 이를 `additionalContext`로 한 번 주입하고 marker를 소거한다.
서브에이전트는 checkpoint 안내 대상에서 제외되고 native compaction으로 독립 정리된다.

자동 골격의 구간·서사 불릿은 PM이 채운다. `ctx_stop_hook`과
`precompact_capture_hook.sh`는 framework-owned(`@source`)라 `pm_update`가 갱신한다. instance-owned는
`settings.json`뿐이므로 기존 채택자는 템플릿의 비차단 `PreCompact`/`PostCompact` 설정 델타를 자기
파일에 병합한다.

## 엔진 동기화 (메인테이너 · 루트 → 이 타깃)

엔진 경로만 덮어쓴다 — 어댑터·CLAUDE.md·README 는 보존(manifest 밖). 전체 엔진 변경은 이 타깃만
손으로 골라 실행하지 말고, 루트에서 `--all-targets`로 `templates/` 아래의 **존재하는 모든 타깃**에
전파한다. 아래 `--target claude_code`는 이 타깃만 의도적으로 재동기화할 때 쓴다.

```bash
# 루트에서 전체 전파, 또는 이 타깃만 의도적으로 갱신 (--target)
python3 .project_manager/tools/pm_update.py --from . --all-targets --dry-run
python3 .project_manager/tools/pm_update.py --from . --target claude_code --dry-run

# 타깃 내부에서 (self-location)
cd templates/claude_code && python3 .project_manager/tools/pm_update.py --from ../../ --dry-run
```

## 참고

- `CLAUDE.md` — 채택자 세션 진입(부트스트랩·작업 원칙·명령) 단일 진실 (= opencode 의 `AGENTS.md`).
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).
- ADR-0005 — 모노레포 multi-target 구조.
