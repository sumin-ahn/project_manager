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
- **`AGENTS.md`** (공통 코어·instance-owned) — PM 부트스트랩·엔진 호출(인코딩)·완료
  기록·결정 권한·안전 가드와 `$pm-…` 진입 표기를 제공한다. 같은 파일을 공유하는 다중 하네스 설치는
  설치기가 선택된 하네스 표기만 병기해 어느 한쪽도 침묵 오표기하지 않는다.
- **codex 전용 정적 진입 doc 없음** (ADR-0070 D3 C-v2) — `AGENTS.override.md`·`.codex/AGENTS.md`
  같은 codex 전용 진입 문서를 두지 않는다. codex 방법론 전달 채널 3개:
  - **위임 4축** = `.codex/agents/{architect,code-reviewer,developer,researcher}.toml` (아래 §위임).
  - **운영 규율** = canonical 스킬(`.agents/skills`·아래 §스킬).
  - **실행 모델·위임 규약** = `$pm-bootstrap` 커맨드 카드가 하네스 감지(`CODEX_THREAD_ID`/`CODEX_CI`
    env)로 codex 절을 발화(아래 §PM 시작).
  - **부수 이득**: 방법론이 전부 pm_update 갱신 도달 채널(TOML @source·스킬·엔진 카드)에 실려 —
    instance-owned 진입 doc 의 drift 표면이 codex 엔 애초에 생기지 않는다.
- **`.codex/config.toml`·`.codex/hooks.json`** (instance-owned·채택자 소유) — ctx checkpoint 안내
  (문서화된 `model_auto_compact_token_limit` + 비차단 PreCompact 구조화 메시지). manifest 밖이라
  byte-copy 전파를 타지 않고, 대신 전용 채널(아래 §어댑터 config 도달 채널)로 도달한다
  (settings.json/opencode.jsonc 대칭).

## Context safety: direct TUI vs. relay

`model_auto_compact_token_limit`은 auto-compaction을 유발하는 숫자 threshold일 뿐 off 스위치가 아니다.
`hooks.json`도 compaction을 차단하지 않는다. `auto`와 `manual` PreCompact matcher는 checkpoint
골격을 자동 생성한 뒤 JSON `systemMessage` 안내를 내고 transaction을 통과시킨다. Codex CLI 0.147.0
로컬 바이너리의 hook event enum에서 `PostCompact` 지원을 확인했으므로 같은 두 matcher를 배선했고,
이 이벤트는 `pm_log.py snapshot --json`의 엔진 소유 최종 텍스트를 `systemMessage` 엔벨로프로
출력한다. 실측된 범위는 이 출력 채널까지이며, 그 엔벨로프의 모델 도달은 direct TUI에서 미검증이고
headless exec에서는 미도달이다(아래 두 항목).
compaction 횟수를 세는 영속 상태는 두지 않는다. 각 handler는 POSIX `command`와 native Windows
PowerShell-safe `commandWindows`에서 동일한 checkpoint 의미의 JSON 하나만 stdout으로 낸다. checkpoint
subprocess stdout/stderr는 전량 폐기하며 PowerShell 5.x 호환을 위해 명령은 `;`로 분리한다. Windows payload는
PowerShell 5.1 리다이렉션의 cp949 기본값에서도 JSON이 깨지지 않도록 ASCII 안내문을 쓴다.

- direct TUI: 메인테이너 실측(2026-08-06, codex-cli 0.146.0)에서 `^manual$`은 TUI의
  `/compact` 전용임을 확인했다. `systemMessage`의 direct TUI 표시는 미검증이다. trusted project와
  `/hooks` 승인이 없으면 PreCompact 자체가 조용히 발화하지 않는다.
- headless exec: 같은 메인테이너 실측의 `--oss` 프로브(`reach-probe/`)에서 `^auto$` 비차단
  훅 marker 발화를 확인했고, `turn_aborted` 0건·`context_compacted` 기록과 후속 turn 정상 계속으로
  compaction 통과를 확인했다. 단, compaction 훅의 `systemMessage`는 `codex exec`의
  stdout JSONL·stderr·rollout·`CODEX_HOME` 전수 grep 어디에도 나타나지 않았고 모델 자기보고도 음성이었다. 따라서
  **exec 경로에서 `systemMessage` 안내는 모델에 닿지 않는다(관측만 가능)**.
  도달 여부는 **채널별로 다르다** — 진입점 훅(`PreToolUse`·`UserPromptSubmit`)의
  `hookSpecificOutput.additionalContext`는 **모델에 닿는다**. 격리 `CODEX_HOME` 라이브 실측
  (codex-cli 0.147.0)에서 세션 안 ctx 넛지 문구가 rollout에 `role:"developer"` 입력 레코드로
  남고 모델이 그 문구를 verbatim 인용했다. 그래서 세션 안 안내는 `systemMessage`가 아니라
  이 채널을 쓴다.
- relay: `codex exec --json`의 `turn.completed.usage` 누계를 매 turn 파싱하고 직전 누계와의 차분을
  보수적 점유 상한으로 쓴다. rollout `token_count.last_token_usage`는 같은 이벤트의 누계 input이 방금
  받은 wire 누계 input과 일치할 때만 더 정밀한 1순위 신호로 채택한다. 이 판정값이 예산의 STOP 경계에
  닿으면 relay driver가 turn 완료 회전 신호를 남기고 Supervisor가 세션을 교체한다. exec에서 소실되는
  compaction 훅 `systemMessage` 안내 대신 driver 회전 선점이 relay 경로를 실보호한다. 이것이 장기 경로의 **proactive** 기계 가드다.

2026-07-22~23 장기 TUI rollout에서는 `context_compacted`가 네 번 기록됐고, 해당 event stream에
`hook_started`/`hook_completed` 및 기존 echo tripwire 출력은 없었다. 따라서 이전 echo-only tripwire는
false-green이었다. direct TUI rollout의 token_count는 관측되지만 stable post-turn usage callback은 없으므로,
장기 PM은 relay의 `turn.completed.usage` 가드를 사용한다.

## 훅 범용 진입점 (기능 추가가 config를 안 건드린다)

`PreToolUse`·`UserPromptSubmit`·`PostToolUse`는 이벤트당 진입점을 **하나씩만** 연다 — `matcher`는
`.*`이고 실행 대상은 manifest 등재 디스패처 `.codex/pm_orch_codex.py --hook-dispatch <이벤트>`다.
"이 payload에 어떤 가드를 돌릴지"의 판단은 그 코드 안의 registry가 쥔다. native spawn 위임 채널도
그 registry의 한 항목이고, 옛 `^collaborationspawn_agent$` matcher 판정은 값 그대로 진입점 뒤
분기로 옮겨 왔다.

그래서 **가드 기능 추가는 엔진 코드 변경뿐**이다. 채택자는 `.codex/hooks.json`을 다시 고치지
않고 `/hooks` 재승인도 다시 하지 않는다. 등록된 기능 목록은
`python3 .codex/pm_orch_codex.py --hook-features`가 JSON으로 낸다.

진입점 집합 자체는 릴리즈 간 불변이다. 늘리려면 채택자 config 변경 + 재승인이 다시 필요하므로
1회 마이그레이션으로만 바꾼다. 진입점이 빠진 채택자는 `pm_update`가 이벤트 이름을 지목해
알린다(advisory — 훅을 의도적으로 끈 채택자를 차단하지 않는다). 자식 가드가 못 답하거나 설치된
디스패처가 구세대여도 훅은 rc0 + 완전한 엔벨로프로 끝나고 폴백 사실이 `adapter-fallback`
마커로 남는다. 도구 호출은 어느 경우에도 막히지 않는다.

`SubagentStart`(관측 전용)와 `PreCompact`/`PostCompact`(checkpoint·snapshot)는 값 공간을 전수
덮는 matcher를 이미 갖고 있어 이 진입점 밖에 남는다. 그 세 이벤트에 두 번째 기능을 얹으려면
그때 config 변경 + 재승인이 한 번 더 필요하다.

## 어댑터 config 도달 채널 (managed / report)

instance-owned 어댑터 config는 manifest 밖이라 byte-copy 전파를 타지 않는다. 대신 상류 fix가
도달하는 전용 채널이 분류별로 갈린다.

| 파일 | 분류 | 도달 방식 |
| --- | --- | --- |
| `.codex/hooks.json` | `managed` | 무편집이면 `pm_update`가 백업 후 자동 갱신 + `/hooks` 재승인 안내 |
| `.codex/config.toml` | `report` | 갱신 0 · drift 한 줄 보고 |
| `.claude/settings.json` | `report` | 동상 |
| `.opencode/opencode.jsonc` | `report` | 동상 |

`report` 세 파일에는 권한 allowlist·모델·threshold 같은 채택자 노브가 실재해 자동 갱신이 그
값을 지울 수 있다. 그래서 **이 세 파일의 이벤트 배선은 수동 1커맨드로 남는다** —
`./pm-config.sh sync-adapter-config --accept <경로>`(백업 후 이 엔진 세대의 값으로 교체). 자동화
대상이 아니라는 것이 결정이며, 상류가 그 파일의 배선을 바꿔도 채택자가 이 커맨드를 칠 때까지
반영되지 않는다. 편집분(`edited`)은 어느 분류에서도 자동 갱신되지 않고 보존된다.

`managed` 갱신 뒤에는 trusted project에서 `/hooks` 승인을 다시 확인한다 — hook trust는 현재 hook
정의의 hash에 결속되므로 정의가 바뀌면 재승인 전까지 발화하지 않는다.

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
trust 승인** 후에만 로드·발화한다. hook trust는 **현재 hook 정의의 hash**에 결속되므로 새 hook이나
변경된 hook은 다시 검토할 때까지 skip된다. 재검토가 필요하면 Codex host가 시작 시 경고하고 `/hooks`에서
source·상태를 확인해 review/trust하거나 개별 hook을 disable할 수 있다. hook command 자신은 미승인일 때
아예 실행되지 않으므로 자기 미승인을 감지하거나 차단할 수 없지만, host 측 감지 표면(startup warning·
`/hooks`)은 존재한다. 승인 전에는 위임 spawn 가드와 PreCompact ctx checkpoint가 설치돼 있어도 무력하다.
([공식 review/trust 계약](https://developers.openai.com/codex/hooks#review-and-trust-hooks))
import/add-harness 직후 1회 수동으로 밟는다:

1. 이 디렉토리에서 대화형 `codex` 를 한 번 열어 **프로젝트 trust 를 수락**한다
   (`.codex/agents/*.toml`·`config.toml` 은 trusted project 한정 로드).
2. codex 안에서 `/hooks` 로 현재 hook 정의와 상태를 확인하고 **hook trust 를 승인**한다
   (위임 spawn 가드·PreCompact ctx checkpoint 발화 전제).
3. 검증 — 위임 스폰 대상 목록에 `architect`/`code-reviewer`/`developer`/`researcher` 가 보이면 로드 성공.

격리 `CODEX_HOME`에서 hook 발화만 재현할 때는 `--dangerously-bypass-hook-trust`로 승인 축을 우회할 수
있다. 이는 개인 설정을 건드리지 않는 실측용 절차이며, 실제 채택 환경의 `/hooks` 승인을 대신하지 않는다.

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
- **부트스트랩 카드가 위임 지침** — codex 세션에서 `$pm-bootstrap`을 부르면
  엔진이 하네스를 감지(`CODEX_THREAD_ID`/`CODEX_CI` env)해 카드 끝에 **codex 절**을 발화한다 — 실행
  모델·위임 규약·trust 힌트가 여기로 전달된다(정적 진입 doc 이 없는 C-v2 구조의 유일 전달 채널).

## 위임 (in-session spawn · 4축)

위임은 codex **multi_agent in-session spawn** 이다 — PM(메인세션)이 세션 *안에서* 명명 custom
agent 를 스폰한다(부모 sandbox 상속·`codex exec --agent` 플래그 부재라 **외부 프로세스 위임 없음**).

- 4축 = `.codex/agents/{architect,code-reviewer,developer,researcher}.toml`. 각 TOML 은 필수 필드
  `name`/`description`/`developer_instructions`(≈system prompt) + `sandbox_mode`(developer/architect=
  `workspace-write`·code-reviewer=`workspace-write`(지정된 라운드 파일만 write·코드/board/git 금지)·
  researcher=`read-only`)를 담는다. `model` 키는 없다 — 사용자
  config 기본 상속(D5).
- 표준 위임 프롬프트는 `$pm-dev-delegate` 스킬. (위임 *개념*·generate≠evaluate 는 루트 README.)
- **native spawn 채널 가드 실측**(codex-cli 0.147.0): 라이브 `PreToolUse` payload의 native spawn
  tool 이름은 `collaborationspawn_agent`이고 선택 역할은 `tool_input.task_name`에 실린다.
  spawn payload에는 `tool_input.agent_type`이 없다. cross-harness deny만
  `hookSpecificOutput.permissionDecision="deny"` + `permissionDecisionReason` 및
  `decision="block"` + `reason`을 출력한다. 정상 allow는 빈 객체 `{}`라 host 판정에 무개입이다.
- **deny host 실효성 실측**(codex-cli 0.147.0): 격리 `CODEX_HOME`에서
  `--dangerously-bypass-hook-trust`를 주고 `collaborationspawn_agent`의 `PreToolUse`가 현행 deny
  5필드 envelope(`decision:"block"` + `reason` +
  `hookSpecificOutput.permissionDecision:"deny"` + `systemMessage` + `suppressOutput:false`)를
  출력하게 한 뒤 spawn을 지시했다. 재시도로 `PreToolUse` 4건과 `error` 2건이
  기록됐지만 `SubagentStart`는 0건이라 실제 spawn이 차단됐다. 같은 지시를 allow-only tee hook으로
  바꾼 대조군(현재 출하 allow와 같은 빈 객체 `{}`)은 `SubagentStart` 1건이 발화했다. 출하 wrapper는
  이 exact deny 5필드·빈 allow·2필드 fail-open 경고만 통과시키며 다른 shape은 경고로 강등한다.
  현재 공식 문서는 PreToolUse의 `suppressOutput`을
  미지원으로 적고 그런 출력은 hook failure 후 tool을 계속한다고 설명하지만, 0.147.0 host 실동작은
  deny를 먼저 적용했다. 따라서 문서 schema만으로 차단 실패를 추론하지 말고 버전별 host를 끝까지
  재실측하며, 이 envelope의 block+deny 쌍은 다음 host 재검증 전까지 유지한다
  ([공식 PreToolUse 출력 계약](https://developers.openai.com/codex/hooks#pretooluse)).
  2026-08-12에 동일한 격리 `CODEX_HOME`으로 deny/allow를 재실행했으나 현재 executor의 네트워크
  sandbox가 WebSocket과 HTTPS 모두 `Operation not permitted`로 막아 두 실행이 모델 응답·hook 전에
  rc=1로 끝났다(`PreToolUse=0`, `SubagentStart=0` 각각). 이 0/0은 deny 효력이나 allow 대조값이
  아니며, 네트워크가 허용된 릴리즈 환경에서 재측정해야 한다.
- **matcher drift 관측**: `SubagentStart`는 deny 필드가 없는 관측 전용 hook이다. 두 hook은 소비
  가능한 receipt를 만들지 않고 append-only JSONL만 남긴다.
  `PreToolUse`는 처리 결과를, deny 필드가 없는 `SubagentStart`는 실제 start를 기록한다.
  `SubagentStart.agent_type`의 실값 `default`는 역할이 아니므로 대조에 쓰지 않는다. parent spawn과
  child start의 `turn_id`가 서로 다르고 spawn에는 `agent_id`가 없으므로, `board.py lint`는 같은
  `session_id`의 선행 allow-spawn을 start와 1:1 대조하고 start 자체는
  `(session_id, turn_id, agent_id)`로 식별한다. 대조가 없는 start를 PM이 읽는 advisory 한 줄로
  표면화하며, 같은 바이트를 재스캔해도 rename/소비/삭제 없이 같은 결과다. 기록 채널은
  `.project_manager/.local/delegate-channel/codex-observations.jsonl`이고 세그먼트당 256 KiB·활성 파일
  포함 최대 4개로 회전해 디렉터리 크기와 JSONL 개수를 제한한다. hook의 `systemMessage`는 기록 실패
  때의 부가 시도일 뿐 TUI 도달 보장이 아니며, matcher-miss 보장 표면은 `board.py lint`다.
- 대기 도구 `collaborationwait_agent`와 `agent_id`가 동반된 서브에이전트 자신의 도구 호출은 spawn이
  아니므로 matcher와 가드 내부 판정 모두 통과한다.
- execpolicy는 argv-only라 동적 `task_name` 판정을 구조적으로 수행할 수 없으며, hooks의
  `PreToolUse`만 차단 채널이다. 각 hook command는 10초 외부 제한보다 짧은 8초 subprocess 제한으로
  가드를 감싸 가드 파일 부재·비정상 종료·정지에도 유효한 allow JSON을 출력한다.
  단, POSIX shell 자체의 기동 실패나 command 문자열 파싱 실패는 이 wrapper가 실행되기 전이라
  JSON fallback을 출력할 수 없다. 이 최외곽 실패가 Codex hook host에서 fail-open인지 live probe로
  입증하지 못했으므로 보장하지 않는다.

## 스킬 (canonical `.agents/skills` · `$` 멘션 · auto-trigger)

PM workflow 스킬(pm-bootstrap·pm-ticket·pm-dev-delegate·pm-review·pm-qa·pm-handoff·pm-release·
spike-new … 전체는 `.agents/skills/` 디렉토리)은 codex 가 `.agents/skills/*/SKILL.md`(project·cwd→root
스캔)를 **네이티브 소비**한다 — `$<스킬명>` 멘션(예 `$pm-bootstrap`) 또는 description 매칭 auto-trigger.

- **canonical `SKILL.md` 단일 소비**(ADR-0065) — 방법론 소스는 root `.claude/skills`(claude/opencode
  와 동일 단일 진실)이고 `@source` 가 codex 네임스페이스 `.agents/skills` 로 remap 한다(ADR-0054).
  단, 실행 도구 schema가 다른 `$pm-dev-delegate`만 Codex template의 file-level override가 단일 진실이며,
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

- `AGENTS.md` — PM 부트스트랩·엔진 호출(인코딩)·완료 기록·결정·안전 가드 공통 코어
  (= claude_code 의 `CLAUDE.md`·opencode 의 `AGENTS.md`·byte-identical).
- `.codex/agents/*.toml` — codex 위임 4축 custom agent (in-session spawn·`developer_instructions`).
- `.codex/pm_orch_codex.py` — relay 드라이버 (`codex exec --json` thread_id 파싱·`exec resume`).
- ADR-0070 — codex 어댑터 타깃 + 어댑터 구성 단일 진실 · ADR-0069 — 진입 doc 공통 코어 + 하네스별
  전달 채널 · ADR-0054 — @source 전파 채널 · ADR-0065 — 스킬 단일 소비.
- 루트 [`README.md`](../../README.md) — 프레임워크 전체 가이드(네 기둥·도입·워크플로·이식성·계보).

#### deny envelope 3셀 재측정 (2026-08-12 · codex-cli 0.147.0)

격리 `CODEX_HOME` + `--dangerously-bypass-hook-trust` + tee 훅, 셀당 실 스폰 1회.

| 변형 | PreToolUse(spawn) | SubagentStart |
| --- | ---: | ---: |
| deny + `suppressOutput: false` (출하 형상) | 1 | 0 |
| deny (`suppressOutput` 제거) | 1 | 0 |
| allow 대조군 (훅 무출력) | 1 | 1 |

`suppressOutput` 유무는 차단 여부와 무관하다. deny 는 두 변형 모두에서 스폰을 막고, 대조군만
`SubagentStart` 에 도달한다. 출하 형상을 5필드로 유지하는 이유는 그 bytes 가 측정된 값이기 때문이다.
공식 문서 서술과 설치 호스트의 실동작이 갈릴 수 있으므로, 호스트 버전이 오르면 end-to-end 로 재측정한다.
