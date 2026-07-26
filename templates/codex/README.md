# codex 어댑터 타깃

Claude Project Framework 의 **codex(OpenAI Codex CLI) 어댑터** 타깃. 루트 엔진(`.project_manager/`)을
공유하고 어댑터층(`.codex/`·`.agents/skills`·`AGENTS.md`)만 이 타깃에서 다르다. (ADR-0005·ADR-0006·ADR-0070)

> 프레임워크 **전체 가이드**(네 기둥·도입 절차·placeholder 표·워크플로·이식성 등급·계보)는
> 하니스 무관 공통 문서 — **루트 [`README.md`](../../README.md)**. 이 문서는 *codex 어댑터
> 고유분*만 담는다 (claude_code [`../claude_code/README.md`](../claude_code/README.md)·opencode
> [`../opencode/README.md`](../opencode/README.md) 타깃과 대칭).

## 어댑터층

codex CLI 세션(`codex` 대화형·`codex exec` 비대화)이 `AGENTS.md`(공통 코어)를 진입으로 PM 을
self-driven 으로 구동한다. claude_code 의 `CLAUDE.md`+`.claude/`·opencode 의 `AGENTS.md`+`.opencode/`
에 대응하는 codex 등가물 — 엔진은 루트와 공유하고 여기 어댑터만 타깃 고유다.

- **PM = 메인세션** (ADR-0070 D1) — codex 세션이 `AGENTS.md` 를 자동 로드해 그 세션이 곧 PM 이다.
  codex 는 headless 명명-agent 타깃(`codex exec --agent` 플래그 부재)이 없어, opencode 의
  `pm.md`(mode: primary) 에 해당하는 파일이 **없다**(load-bearing 부재). relay(supervisor 위임)는
  `codex exec --json` 으로 세션을 열어 `thread.started.thread_id` 를 파싱하고 `codex exec resume <id>`
  로 이어간다(`.codex/pm_orch_codex.py`).
- **`AGENTS.md`** (공통 코어·harness-neutral·instance-owned) — PM 부트스트랩·엔진 호출(인코딩)·완료
  부기·결정 권한·안전 가드. `templates/opencode/AGENTS.md` 와 **byte-identical**(공통 코어 수렴·가드) →
  opencode 와 dual-harness 로 공존해도 진입 doc 충돌이 byte-identical git-safe skip 으로 자연 소멸한다.
- **codex 전용 정적 진입 doc 없음** (ADR-0070 D3 C-v2) — `AGENTS.override.md`·`.codex/AGENTS.md`
  같은 codex 전용 진입 문서를 두지 않는다. codex 방법론 전달 채널 3개:
  - **위임 4축** = `.codex/agents/{architect,code-reviewer,developer,researcher}.toml` (아래 §위임).
  - **운영 규율** = canonical 스킬(`.agents/skills`·아래 §스킬).
  - **실행 모델·위임 규약** = `/pm-bootstrap` 커맨드 카드가 하네스 감지(`CODEX_THREAD_ID`/`CODEX_CI`
    env)로 codex 절을 발화(아래 §PM 시작).
  - **부수 이득**: 방법론이 전부 pm_update 갱신 도달 채널(TOML @source·스킬·엔진 카드)에 실려 —
    instance-owned 진입 doc 의 drift 표면이 codex 엔 애초에 생기지 않는다.
- **`.codex/config.toml`·`.codex/hooks.json`** (instance-owned·채택자 소유) — 대화형 ctx 가드
  (문서화된 `model_auto_compact_token_limit` + PreCompact 구조화 경고). 트리에 실재하되 pm_update 미전파
  (settings.json/opencode.jsonc 대칭·hook trust 재승인 churn 회피).

## Context safety: direct TUI vs. relay

`model_auto_compact_token_limit`은 auto-compaction을 유발하는 숫자 threshold일 뿐 off 스위치가 아니다.
대신 `hooks.json`은 `auto`와 `manual` PreCompact를 구분해 JSON `continue:false`로 compaction
transaction을 hard-stop하고 `/pm-handoff`를 요구한다. codex-cli 0.145.0의 trusted disposable probe에서
두 matcher 모두 `PreCompact (stopped)`·matcher별 stopReason·`turn_aborted(reason=interrupted)`를 남겼고,
`context_compacted`는 만들지 않았으며 abort 뒤 canary turn이 원문 연속성을 회수했다. 각 handler는 POSIX
`command`와 native Windows PowerShell-safe `commandWindows`에서 같은 JSON을 stdout으로 낸다.

- direct TUI: PreCompact hard-stop은 **reactive 최후 방어선**이다. manual hard-stop이면 먼저 같은
  thread에서 `/pm-handoff`를 실행한다. auto 임계 초과로 다음 model turn도 반복 차단되면 hard-stop
  가이드대로 `/status`에서 chat ID를 확인하고 `/quit`한 뒤
  `codex resume --disable hooks <CHAT_ID>`로 해당 invocation만 hooks 없이 재개해 `/pm-handoff`하고,
  fresh normal session을 시작해 hooks를 다시 활성화한다. 이 break-glass는 compaction을 허용하므로
  handoff가 lossy summary 기반일 수 있다. project config의 hooks를 영구 비활성화하지 않는다.
  hook trust가 없으면 이 방어선도 실행되지 않는다.
- relay: `codex exec --json`의 `turn.completed.usage`를 매 turn 파싱한다. 누적 usage가 예산의 STOP
  경계에 닿으면 relay driver가 post-turn STOP marker를 남기고 Supervisor가 세션을 회전한다. 이것이
  장기 경로의 **proactive** 기계 가드다.

2026-07-22~23 장기 TUI rollout에서는 `context_compacted`가 네 번 기록됐고, 해당 event stream에
`hook_started`/`hook_completed` 및 기존 echo tripwire 출력은 없었다. 따라서 이전 echo-only tripwire는
false-green이었다. direct TUI rollout의 token_count는 관측되지만 stable post-turn usage callback은 없으므로,
장기 PM은 relay의 `turn.completed.usage` 가드를 사용한다.

## 채택 (pm_import — 정규 경로)

채택은 **manager 루트의 `pm-import.sh`(`/.cmd`) 파사드**(= `pm_import.py` 호출)로 한다 — 어댑터
복사·placeholder 치환·board init·git init(`--new`)까지 한 번에 처리한다.

```bash
# 신규 프로젝트 (디렉토리 생성 + git init)
<manager>/pm-import.sh --new <PATH> --harness codex

# 기존 프로젝트에 도입 (비파괴·충돌 파일 백업)
<manager>/pm-import.sh --into <PATH> --harness codex

# 적용 전 계획만 미리보기 (파일시스템 미변경) — 권장
<manager>/pm-import.sh --new <PATH> --harness codex --dry-run
```

> Windows 는 `pm-import.cmd`. codex 는 opencode 와 달리 모델 placeholder 해소가 **불요**하다 —
> `.codex/agents/*.toml` 에 `model` 키가 없어 사용자 config 기본(gpt-5.5)을 상속한다(D5). 기존
> 인스턴스에 codex 를 얹으려면 `add-harness codex`(opencode 공존 시 공통 코어 `AGENTS.md` 가
> byte-identical → git-safe skip·무충돌).

## trust 선행 (codex 고유·2단계·import 직후 1회)

codex 의 `.codex/config.toml`·`.codex/hooks.json`·`.codex/agents/*.toml` 은 **trusted project + hook
trust 승인** 후에만 로드·발화한다. import 는 완료 시 이 2단계를 loud 하게 안내한다 — 미승인 상태로
두면 위임 subagent 스폰·PreCompact ctx tripwire 가 조용히 발화하지 않는다. import/add-harness 직후
1회 수동으로 밟는다:

1. 이 디렉토리에서 대화형 `codex` 를 한 번 열어 **프로젝트 trust 를 수락**한다
   (`.codex/agents/*.toml`·`config.toml` 은 trusted project 한정 로드).
2. codex 안에서 `/hooks` 로 **hook trust 를 승인**한다 (PreCompact ctx tripwire 발화 전제).
3. 검증 — 위임 스폰 대상 목록에 `architect`/`code-reviewer`/`developer`/`researcher` 가 보이면 로드 성공.

> ⚠️ `-c projects.<path>.trust_level=trusted` CLI override 는 **먹지 않는다**(실측) — user config
> `[projects]` 영속 trust 가 있어야 project-level `.codex/agents`·hooks 가 로드된다. 위 ① 대화형
> 수락이 유일 경로다.

## PM 시작

trust 2단계를 마쳤으면 프로젝트 루트에서 codex 를 연다 — 그 세션이 곧 PM 이다.

```bash
codex          # 대화형 — AGENTS.md(공통 코어)를 자동 로드해 그 세션이 PM 으로 부트스트랩
```

- **AGENTS.md 자동 로드** — codex 가 git root→cwd 의 `AGENTS.md` 를 진입으로 병합 로드한다(공통
  코어·`CLAUDE.md` 는 codex 미로드·아래 §주의). 세션은 이 문서대로 board·wiki 를 파악해 PM 을 운영한다.
- **부트스트랩 카드가 위임 지침** — codex 세션에서 `$pm-bootstrap`(또는 `/pm-bootstrap`)을 부르면
  엔진이 하네스를 감지(`CODEX_THREAD_ID`/`CODEX_CI` env)해 카드 끝에 **codex 절**을 발화한다 — 실행
  모델·위임 규약·trust 힌트가 여기로 전달된다(정적 진입 doc 이 없는 C-v2 구조의 유일 전달 채널).

## 위임 (in-session spawn · 4축)

위임은 codex **multi_agent in-session spawn** 이다 — PM(메인세션)이 세션 *안에서* 명명 custom
agent 를 스폰한다(부모 sandbox 상속·`codex exec --agent` 플래그 부재라 **외부 프로세스 위임 없음**).

- 4축 = `.codex/agents/{architect,code-reviewer,developer,researcher}.toml`. 각 TOML 은 필수 필드
  `name`/`description`/`developer_instructions`(≈system prompt) + `sandbox_mode`(developer/architect=
  `workspace-write`·code-reviewer/researcher=`read-only`)를 담는다. `model` 키는 없다 — 사용자
  config 기본 상속(D5).
- 표준 위임 프롬프트는 `$pm-dev-delegate` 스킬. (위임 *개념*·generate≠evaluate 는 루트 README.)

## 스킬 (canonical `.agents/skills` · `$` 멘션 · auto-trigger)

PM workflow 스킬(pm-bootstrap·pm-ticket·pm-dev-delegate·pm-review·pm-qa·pm-handoff·pm-release·
spike-new … 전체는 `.agents/skills/` 디렉토리)은 codex 가 `.agents/skills/*/SKILL.md`(project·cwd→root
스캔)를 **네이티브 소비**한다 — `$<스킬명>` 멘션(예 `$pm-bootstrap`) 또는 description 매칭 auto-trigger.

- **canonical `SKILL.md` 단일 소비**(ADR-0065) — 방법론 소스는 root `.claude/skills`(claude/opencode
  와 동일 단일 진실)이고 `@source` 가 codex 네임스페이스 `.agents/skills` 로 remap 한다(ADR-0054).
  단, 실행 도구 schema가 다른 `pm-dev-delegate`만 Codex template의 file-level override가 단일 진실이며,
  manifest의 구체 경로 우선순위로 shared directory 전파 뒤에도 보존된다.

## 주의 (codex 고유)

- **`CLAUDE.md` 미로드** — codex 는 `AGENTS.md` 를 진입으로 읽고 `CLAUDE.md` 는 기본 로드하지 않는다.
  PM 방법론·프로젝트 정체성은 전부 `AGENTS.md`(+ 부트스트랩 카드 codex 절 + 스킬 + TOML)로 전달된다.
- **`-c` trust override 무효** — `-c projects.<path>.trust_level=trusted` CLI 플래그는 먹지 않는다
  (실측). project-level `.codex/*` 로드는 user config `[projects]` 영속 trust 가 필요하다(위 §trust 선행).

## 엔진 동기화 (메인테이너 · 루트 → 이 타깃)

루트에서 이 타깃으로 엔진을 동기화한다 (엔진 경로만 덮어씀 — 어댑터 보존·flavor manifest·ADR-0054).
전체 엔진 변경은 이 타깃만 손으로 골라 실행하지 말고, 루트에서 `--all-targets`로 `templates/` 아래의
**존재하는 모든 타깃**에 전파한다. 아래 `--target codex`는 이 타깃만 의도적으로 재동기화할 때 쓴다.

**루트에서 전체 전파, 또는 이 타깃만 의도적으로 갱신 (`--target`):**
```bash
# 루트 repo 에서
python3 .project_manager/tools/pm_update.py --from . --all-targets --dry-run
python3 .project_manager/tools/pm_update.py --from . --target codex --dry-run
python3 .project_manager/tools/pm_update.py --from . --target codex
```

**타깃 내부에서 실행 (self-location):**
```bash
cd templates/codex
python3 .project_manager/tools/pm_update.py --from ../../ --dry-run
python3 .project_manager/tools/pm_update.py --from ../../
```

## 참고

- `AGENTS.md` — PM 부트스트랩·엔진 호출(인코딩)·완료 부기·결정·안전 가드 공통 코어
  (= claude_code 의 `CLAUDE.md`·opencode 의 `AGENTS.md`·byte-identical).
- `.codex/agents/*.toml` — codex 위임 4축 custom agent (in-session spawn·`developer_instructions`).
- `.codex/pm_orch_codex.py` — relay 드라이버 (`codex exec --json` thread_id 파싱·`exec resume`).
- ADR-0070 — codex 어댑터 타깃 + 어댑터 구성 단일 진실 · ADR-0069 — 진입 doc 공통 코어 + 하네스별
  전달 채널 · ADR-0054 — @source 전파 채널 · ADR-0065 — 스킬 단일 소비.
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).
