"""설치 공통 install_rules (domain)."""
from __future__ import annotations

from pathlib import Path
import re

class _AdapterRegistrySchemaError(RuntimeError):
    """실제 등록부의 `(tuple[str, ...], str)` 값 계약이 깨짐."""


def adapter_directories() -> tuple[str, ...]:
    """실제 등록부 값의 형상을 검증하고 어댑터 루트를 순서대로 반환한다."""
    registry = ADD_HARNESS_ADAPTER

    if not isinstance(registry, dict) or not registry:
        raise _AdapterRegistrySchemaError(
            "ADD_HARNESS_ADAPTER는 비어 있지 않은 "
            "dict[str, tuple[tuple[str, ...], str]]여야 한다"
        )

    directories: list[str] = []
    for harness, value in registry.items():
        if (
            not isinstance(harness, str)
            or not harness
            or not isinstance(value, tuple)
            or len(value) != 2
        ):
            raise _AdapterRegistrySchemaError(
                f"ADD_HARNESS_ADAPTER[{harness!r}] 값은 "
                "(adapter_dirs: tuple[str, ...], root_doc: str)여야 한다"
            )
        adapter_dirs, root_doc = value
        if (
            not isinstance(adapter_dirs, tuple)
            or not adapter_dirs
            or not all(isinstance(item, str) and item for item in adapter_dirs)
            or not isinstance(root_doc, str)
            or not root_doc
        ):
            raise _AdapterRegistrySchemaError(
                f"ADD_HARNESS_ADAPTER[{harness!r}] 값은 "
                "(adapter_dirs: tuple[str, ...], root_doc: str)여야 한다"
            )
        directories.extend(adapter_dirs)
    return tuple(dict.fromkeys(directories))


# import 어댑터 registry의 단일 진실. 공개 ``--harness``는 이 키들의 콤마 집합 또는 ``all``을
# 받는다. ``all``은 별도 튜플을 갖지 않고 항상 이 mapping의 키 전체에서 파생된다. 값은 template
# 디렉터리 튜플 shape를 유지해 기존 소비자/테스트 축이 새 하네스를 자동 발견하게 한다.
HARNESS_TEMPLATE_DIRS = {
    "claude": ("claude_code",),
    "opencode": ("opencode",),
    "codex": ("codex",),
}


REGISTERED_HARNESSES = tuple(HARNESS_TEMPLATE_DIRS)


HARNESS_ALL = "all"


# legacy harness alias seam — v1.5.x 의 `both`(=claude,opencode)는 예약대로 v1.6.0 에서
# 제거됐다(실사용 0 + v1.5.x 경고 한 주기 유예·아래 ratchet 이 기한을 기계 강제한 결과).
# 새 legacy alias 를 들일 땐 여기에 (alias → 정식 하네스 튜플)로 등록하고 제거 기한을 아래
# 상수로 예약한다 — 기한 도달 시 _enforce_legacy_harness_alias_deadline 이 설치 CLI의 인자 해석을
# 막아 제거가 사람 기억에 의존하지 않는다. 파서 확장 분기는 이 dict 로 구동되는 범용 기계다.
LEGACY_HARNESS_ALIASES: dict[str, tuple[str, ...]] = {}


LEGACY_ALIAS_REMOVAL_VERSION = "v1.6.0"


HARNESS_CHOICES = (*REGISTERED_HARNESSES, HARNESS_ALL, *LEGACY_HARNESS_ALIASES)


WEIGHT_CHOICES = ("full", "lite")


def _engine_rev_tuple(value: str) -> tuple[int, int, int]:
    """정규 engine rev(``vMAJOR.MINOR.PATCH``)를 비교 가능한 정수 튜플로 바꾼다."""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise RuntimeError(f"기계 비교할 수 없는 ENGINE_REV 형식: {value!r}")
    return tuple(int(part) for part in match.groups())


def _enforce_legacy_harness_alias_deadline(
    engine_rev: str,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """제거 버전에 도달한 legacy alias가 사람의 기억에만 기대어 잔존하지 않게 한다."""
    active_aliases = LEGACY_HARNESS_ALIASES if aliases is None else aliases
    if (
        active_aliases
        and _engine_rev_tuple(engine_rev)
        >= _engine_rev_tuple(LEGACY_ALIAS_REMOVAL_VERSION)
    ):
        raise RuntimeError(
            f"ENGINE_REV {engine_rev}에서는 legacy harness alias를 제거해야 합니다: "
            f"{', '.join(active_aliases)} (기한 {LEGACY_ALIAS_REMOVAL_VERSION})"
        )


def parse_harness_selection(value: str) -> tuple[str, ...]:
    """콤마 선택을 registry 순서의 중복 없는 하네스 튜플로 정규화한다.

    입력 순서는 결과/충돌 우선순위에 영향을 주지 않는다. ``all``은 호출 시점 registry의 키
    전체로 확장돼 네 번째 하네스가 추가돼도 자동 편입된다. ``LEGACY_HARNESS_ALIASES`` 에
    등록된 alias 도 집합 원소처럼 확장된다(현재 빈 seam — `both` 는 v1.6.0 에서 제거).
    """
    registry = HARNESS_TEMPLATE_DIRS
    if not isinstance(value, str):
        raise ValueError(f"--harness 값은 문자열이어야 합니다: {value!r}")
    raw_tokens = value.split(",")
    tokens = [token.strip() for token in raw_tokens]
    if not tokens or any(not token for token in tokens):
        raise ValueError(
            "--harness 선택에 빈 항목이 있습니다 — 예: claude,codex 또는 all"
        )
    known = set(registry) | {HARNESS_ALL} | set(LEGACY_HARNESS_ALIASES)
    unknown = sorted(set(tokens) - known)
    if unknown:
        raise ValueError(
            f"미지원 harness: {', '.join(unknown)} — 지원: "
            f"{', '.join(registry)}; 조합은 콤마 구분, 전체는 all"
        )

    selected: set[str] = set()
    for token in tokens:
        if token == HARNESS_ALL:
            selected.update(registry)
        elif token in LEGACY_HARNESS_ALIASES:
            aliases = LEGACY_HARNESS_ALIASES[token]
            missing = [harness for harness in aliases if harness not in registry]
            if missing:
                raise ValueError(
                    f"legacy harness alias {token!r}가 미등록 하네스를 가리킵니다: "
                    f"{', '.join(missing)}"
                )
            selected.update(aliases)
        else:
            selected.add(token)
    if not selected:
        raise ValueError("--harness 선택 결과가 비었습니다.")
    return tuple(harness for harness in registry if harness in selected)


FILL_CHOICES = ("auto", "manual")


# fill 단계가 채우는 자유서술 placeholder 3종 (sed 로 못 채움 — repo 분석 필요).
# operational(보존) 토큰과 달리 board init 후 하니스 구동(auto) 또는 TODO 표시(manual)로 처리.
FREE_FORM_TOKENS = (
    "{{PROJECT_CONSTRAINTS}}",
    "{{PROTECTED_PATHS}}",
    "{{USER_GATE_ITEMS}}",
)


# opencode 어댑터 고유 harness 설정값 — operational/자유서술 어디에도 안 속하는 모델 ID.
# `.opencode/agents/*.md` 의 `model:` 필드로 등장.
# *아니라* `opencode models` 결정적 조회로 해소한다(resolve_opencode_model) — 따라서 fill
# 후보에서 분리됐다(중복·환각 제거). opencode 트리가 복사됐을 때만 해소 단계를 탄다.
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"


# 이 토큰을 해소할 때 가용 모델을 조회하는 원천의 하네스 키(`contract.MODEL_SOURCE`).
OPENCODE_MODEL_SOURCE_HARNESS = "opencode"


# fill 실 하니스 구동 게이트 환경변수(호출 횟수 축 — 무엇을 보여줄지가 아니라 몇 번 부를지다).
# PM_IMPORT_LIVE_HARNESS=1
# AND --fill auto 동시 충족 시만 실 runner 호출 — 둘 중 하나라도 없으면 stub/manual 강제.
LIVE_HARNESS_ENV = "PM_IMPORT_LIVE_HARNESS"


# 하니스 헤드리스 구동 명령 (fill auto). claude 는 stdout 캡처, opencode 는 --format json 파싱,
# codex 는 `exec --json` JSONL 파싱(최종 agent_message). 프롬프트는 cross 위임과 같은 방식으로 넘긴다 —
# claude·codex 는 표준 입력(전문을 쓰고 닫아 EOF), opencode 는 대상 폴더 안 프롬프트 파일(`--file`).
# 여러 줄 프롬프트를 실행 인자에 싣지 않는다: Windows 에서 실행 파일이 npm 셈(`.cmd`)이면 cmd.exe 가
# 인자를 해석해 첫 줄 뒤를 조용히 버린다(VM 실측). -s workspace-write·--skip-git-repo-check·
# -C <dest>·--dir <dest> 는 _build_runner_argv 가 붙인다. codex 는 `model` 생략=사용자 config 상속.
CLAUDE_FILL_CMD = ("claude", "-p")


OPENCODE_FILL_CMD = ("opencode", "run")


CODEX_FILL_CMD = ("codex", "exec")


# fill 실행도 위임·추가 리뷰와 같은 하네스 프로필을 쓴다. 과거 fill 전용 300초 wall 상한은
# cloud idle 임계보다 먼저 발화해 무진행 판정을 사실상 무력화했고, opencode 로컬 GPU 축의 실측
# 장시간 실행도 잘랐다. 채택 시점 1회성이라는 UX 차이는 별도 판정값의 근거가 아니므로 제거한다.
# 배포 환경 차이는 대상 repo local.conf 의 `harness.<name>.{idle,wall}_timeout` 으로 조정한다.
# 실제 fill argv 계약을 선언한다: (하네스, 증분 진행 신호 유무, 프롬프트 전달 방식).
# claude `-p`는 종료 시 평문 blob 하나를 내므로 profile의 일반 CLI event-stream 선언을 그대로
# 적용하면 정상 침묵도 idle kill한다. 반면 opencode `--format json`·codex `--json`은 실행 중
# 이벤트를 내므로 profile 신호를 소비한다. 표준 입력 전달은 전문을 쓴 뒤 닫아 EOF 를 준다(codex 는
# stdin 이 열려 있으면 추가 입력을 무기한 기다린다).
FILL_PROMPT_STDIN = "stdin"   # 표준 입력으로 전문을 쓴다


FILL_PROMPT_FILE = "file"     # argv 의 `--file` 프롬프트 파일(run_fill 이 대상 폴더 안에 쓴다)


FILL_DRIVER_BY_CMD = {
    CLAUDE_FILL_CMD: ("claude", False, FILL_PROMPT_STDIN),
    OPENCODE_FILL_CMD: ("opencode", True, FILL_PROMPT_FILE),
    CODEX_FILL_CMD: ("codex", True, FILL_PROMPT_STDIN),
}


# fill 가능 여부는 runner argv 계약 선언에서 파생한다. 등록 하네스 전체가 자동 fill을 할 수 있다는
# 가정은 두지 않는다; 새 등록분은 여기의 명시 runner 계약 없이는 --fill auto를 통과할 수 없다.
FILL_CAPABLE_HARNESSES = tuple(
    harness for harness in REGISTERED_HARNESSES
    if any(driver[0] == harness for driver in FILL_DRIVER_BY_CMD.values())
)


# ``--fill-harness`` argparse choices는 등록 adapter 전부다. auto 가능 여부는 설치 전
# ``FILL_CAPABLE_HARNESSES`` 게이트가 별도로 판정하므로 manual 경로를 여기서 막지 않는다.
FILL_HARNESS_CHOICES = REGISTERED_HARNESSES


# add_harness어댑터 네임스페이스 = {adapter dir(들), root doc}. 라이브 인스턴스에 두 번째
# harness 를 *비파괴로 추가*할 때 복사 스코프 = 이 네임스페이스 ∪ **guest flavor 가 `@render` 로 선언한
# 경로**(cross-ns 의존물 포함) − **host 실소유**. 그 밖(엔진·wiki dev-state·타 harness·
# 설정·파사드·flavor 미선언)은 plan 에 애초에 안 들어와 clobber 가 불가능(Decision 2·5). cross-ns 예:
# opencode 를 codex host 에 추가하면 opencode 가 네이티브 소비하는 `.claude/skills`(`.opencode` 밖·
# flavor `@render` 선언이라 복사·등재된다. host 가 이미 소유한 경로(dest engine.manifest
# core·`_dest_manifest_core_paths`)는 스코프 안이라도 제외한다(중복 레이다운 방지). 단일 harness
# (claude|opencode|codex) 하나만 추가한다(집합 선택은 최초 import 소관).
#
# 값 shape = **`(adapter_dirs: tuple, root_doc)`**. claude/opencode 는
# 어댑터 dir 가 하나라 단일-원소 튜플(`(".claude",)`)이고, codex 는 네임스페이스가 **둘**로 갈린다 —
# `.codex/`(agents·config·hooks·relay) + `.agents/`(skills·remap 강제) — 이 dual-namespace 가
# 값 shape 일반화를 기계적으로 강제했다(2-튜플 → dirs-튜플). 소비처(_in_adapter_namespace·add_harness
# unpack)는 dirs 를 iterate 한다([[cross-cutting-breaking-blast-radius]] — shape 변경 소비처 선-스코프).
ADD_HARNESS_ADAPTER = {
    "claude": ((".claude",), "CLAUDE.md"),
    "opencode": ((".opencode",), "AGENTS.md"),
    "codex": ((".codex", ".agents"), "AGENTS.md"),
}


# 둘 이상의 하네스가 같은 루트 진입 문서를 자동 로드할 때의 공존 정책.
# 값 = (문서를 공유하는 하네스 집합, harness-neutral 원본을 소유한 하네스). opencode 단독 lite는
# 자족형 AGENTS.lite.md를 계속 쓰지만, opencode+codex 공존에서는 그 opencode 실행 모델(task tool)이
# codex에도 자동 로드되지 않도록 두 트리의 AGENTS.md source를 공통 코어로 통일한다. full도 같은
# 선언을 타므로 두 무게축의 정책이 갈리지 않는다. 하네스별 실행/위임 지침은 각 namespace
# (.opencode/pm-instructions.md, .agents/skills·.codex/agents)에 그대로 분리되어 있다.
NEUTRAL_SHARED_ENTRY_DOCS = {
    Path("AGENTS.md"): (
        frozenset(
            harness
            for harness, (_dirs, root_doc) in ADD_HARNESS_ADAPTER.items()
            if root_doc == "AGENTS.md"
        ),
        "codex",
    ),
}


# 어댑터 네임스페이스에서 **채택자 소유**로 출하되는 파일의 단일 선언(harness → template-relative
# POSIX relpath 집합). 값의 뜻 = "flavor manifest 에 등재하지 않는다"(pm_update 가 안 덮는다) —
# 루트 진입 문서와 사용자 권한·trust·machine-local config 가 여기 든다. 이 선언이 없으면 그 파일은
# 산문 주석으로만 채택자 소유이고, 기계는 그것을 "출하되는데 어느 채널에도 없는 파일"(영구 동결)과
# 구분하지 못한다 — 출하-등재 역방향 가드가 이 선언을 소비해 둘을 가른다.
# 네 번째 하네스가 자기 config 를 들여오면 여기 명시해야 가드를 통과한다.
INSTANCE_OWNED_ADAPTER_FILES = {
    "claude": frozenset({"CLAUDE.md", ".claude/settings.json"}),
    "opencode": frozenset({"AGENTS.md", ".opencode/opencode.jsonc"}),
    "codex": frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
}


# add-harness가 절대 merge/clobber하지 않는 adopter-owned adapter 설정의 단일 정책 지점.
# 값은 template-relative POSIX relpath다. 위 소유 선언(INSTANCE_OWNED_ADAPTER_FILES)의 **부분집합**
# 이다 — 소유는 "누구 파일인가", 이 정책은 그 소유에 딸린 "add-harness 복사 때 어떻게 다루나"라
# 관심사가 다르고, 미선언 경로를 여기 넣으면 소유 진실이 둘로 갈린다(가드가 red). 엔진/어댑터
# 코드 전체를 보존하는 broad 예외가 아니라, 권한 경계인 개별 config 파일만 좁게 보호한다.
ADD_HARNESS_CREATE_IF_ABSENT = {
    "claude": frozenset(),
    "opencode": frozenset(),
    "codex": frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
}


# instance-owned config 의 **상류 fix 도달 채널** 분류. 키는 위 소유 선언에 있는 경로여야 하고,
# 선언된 경로는 전부 여기 분류가 있어야 한다(양방향을 가드가 강제) — 분류 누락을 허용하면 새 config
# 가 조용히 채널 0 으로 떨어져 지금 닫는 결함이 그대로 재발한다. 값의 뜻:
#   managed : 무편집(dest 해시 == 원장 해시)이면 백업 후 현행 template 으로 자동 갱신. 파일 전체가
#             엔진 동작이고 채택자 노브가 없는 것만 든다.
#   report  : 갱신하지 않고 template 대비 drift 만 표기. 채택자 노브(권한 allowlist·모델·threshold)가
#             실재해 자동 갱신이 그 값을 지울 수 있는 파일.
#   none    : 채널 없음 — 루트 진입 문서(본문 대부분이 채택자 산문이라 기계 대조가 무의미).
ADAPTER_CONFIG_MANAGED = "managed"


ADAPTER_CONFIG_REPORT = "report"


ADAPTER_CONFIG_NO_CHANNEL = "none"


ADAPTER_CONFIG_CHANNEL = {
    "claude": {
        "CLAUDE.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".claude/settings.json": ADAPTER_CONFIG_REPORT,
    },
    "opencode": {
        "AGENTS.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".opencode/opencode.jsonc": ADAPTER_CONFIG_REPORT,
    },
    "codex": {
        "AGENTS.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".codex/config.toml": ADAPTER_CONFIG_REPORT,
        # 초기 롤아웃의 유일한 managed 대상 — 상류 diff 가 100% 엔진 동작(훅 차단→비차단·문구 전면
        #   교체)이고 채택자 노브가 0 임이 실측됐다. 대상 확장은 원장이 누적된 뒤 별도 판정.
        ".codex/hooks.json": ADAPTER_CONFIG_MANAGED,
    },
}


# managed 갱신 뒤 채택자가 해야 하는 후속 행동 — 자동 갱신이 조용한 기능 비활성화가 되지 않게
# 갱신 시점에 함께 낸다(codex 는 훅 정의가 바뀌면 세션에서 다시 승인해야 발화한다).
ADAPTER_CONFIG_REAPPROVAL_NOTE = {
    ".codex/hooks.json": "codex 세션에서 `/hooks` 로 훅을 다시 승인해야 새 정의가 발화한다",
}


# instance-owned config 가 담는 **설정 키**의 선언 — 기능 단위 거리(`adapter_config_reports`)가
# "최신 템플릿과 무엇이 다른가" 를 줄 수가 아니라 항목으로 말하는 축이다. 키는 점 표기 경로이며
# 문서 안쪽으로 한 단계씩 내려간다(`permissions.allow` = `permissions` 객체의 `allow`).
#
# **키는 파일 단위로 선언한다.** 훅 세트(`ADAPTER_HOOK_SET`)에 얹지 않는 이유는 그 선언의 좌표가
# 훅을 등록하는 config 하나뿐이기 때문이다 — opencode 는 `config_relpath` 가 빈 문자열이고
# `.codex/config.toml` 은 훅 세트 밖이라, 하네스 축에 달면 이 두 파일의 설정 키가 선언될 자리가
# 없다. 파일 축은 그 자리를 그대로 준다(파일 = 판정 단위 = 표시 행).
#
# 훅 배선(`hooks.<이벤트>`)은 여기 없다 — 그 축은 문서의 `hooks` 매핑과
# `ADAPTER_HOOK_SET[...].entrypoints` 에서 파생한다(선언 중복 0).
#
# 하네스 사용 한도 반영(`apply_harness_ctx`)이 쓰는 관리 키는 이 표에 두지 않는다 — 값이 local.conf
# 에서 오므로 템플릿과의 차이는 채택자 편집이 아니다(claude `autoCompactWindow`·`idleCompaction`·codex
# `model_context_window`·`model_auto_compact_token_limit`·opencode `compaction`). opencode 는 모델
# 항목의 `limit` 이 `provider` 안에 있어 행을 지우지 않고 비교 전에 `limit` 만 뺀다
# (`_provider_without_model_limits`).
ADAPTER_SETTINGS_KEYS: dict[str, tuple[str, ...]] = {
    ".claude/settings.json": (
        "autoCompactEnabled", "env", "permissions.allow", "permissions.deny", "statusLine",
    ),
    ".opencode/opencode.jsonc": (
        "instructions", "permission.bash", "provider",
        "tool_output.max_lines", "tool_output.max_bytes",
    ),
    ".codex/config.toml": (
        "sandbox_mode", "approval_policy", "approvals_reviewer",
        "features.multi_agent", "features.hooks",
        "sandbox_workspace_write.network_access",
    ),
}


DEFAULT_TAGLINE = "한 줄 프로젝트 설명"


# lite 진입 파일 관례: `X.lite.md` 는 진입 `X.md` 의 lite 변종이다.
# (예: CLAUDE.lite.md → CLAUDE.md, AGENTS.lite.md → AGENTS.md.) 임의의 `*.lite.md` 에 일반화.
LITE_SUFFIX = ".lite.md"


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


# manifest 미소유 출하 텍스트 중 **설치 하네스 전체**가 함께 읽는 표면 = 인스턴스 wiki.
#   engine.manifest 는 *upstream 이 관리하는* 경로만 담는다 — status·log·architecture·raw/README
#   같은 wiki seed 는 인스턴스 상태라 등재하면 pm_update 가 채택자 상태를 덮는다(엔진/상태 분리).
#   그래서 이 부류는 "등재 강제"가 아니라 **설치 하네스 전체 집합 폴백**으로 표기를 해소한다.
#   루트 진입 문서(AGENTS.md)는 독자가 하네스 부분집합이라 이 접두사에 넣지 않는다 — 호출부가
#   NEUTRAL_SHARED_ENTRY_DOCS 멤버십으로 좁힌 context 를 명시 전달한다.
INSTANCE_SHARED_WIKI_PREFIX = ".project_manager/wiki/"


def _in_adapter_namespace(
    rel: Path, adapter_dirs: tuple, root_doc: str, owned_paths: set[str],
    guest_render_paths: set[str],
) -> bool:
    """rel(dst relpath)이 추가 harness 의 복사 스코프 안인가.

    스코프 = ({adapter dir(들) 하위, root doc} ∪ **flavor `@render` 선언**(`guest_render_paths`·cross-ns
    의존물 포함)) − **host 실소유 경로**(`owned_paths`). adapter_dirs 중 하나의 하위/root doc 정확일치/
    flavor `@render` 선언(그 자체·하위) 중 하나여야 하고, 그 중 host(dest)가 이미 소유(pm_update 관리)하는
    경로 하위는 제외한다(경로-포함·`_is_render_managed`). **cross-ns 확장**: opencode 의
    `.claude/skills @render`(네이티브 소비)는 `.opencode` namespace 밖이나 flavor 가 선언한
    의존물이라 codex host(미소유)엔 복사해야 한다 — 옛 namespace-only 스코프는 이를 놓쳐 PM 스킬이
    host 실소유 차감은 그 위에 얹혀 opencode host 의 `.claude/skills`
    (host 소유)처럼 **host 가 이미 가진 것만** 정확히 뺀다(claude host + opencode 는 여전히 미복사). 옛엔
    flavor-native `@render`(guest flavor 관점)로 판정해 claude-as-guest 의 `.claude/agents`·`.claude/skills`
    adapter_dirs 는 튜플 —
    claude/opencode 는 단일, codex 는 이중(`.codex`+`.agents`). 이 밖(엔진·wiki·타 harness·
    설정·파사드)은 전부 False → plan 에 애초에 안 들어온다(구조적 안전·flavor 미선언 경로 유입 0).
    """
    rel_posix = rel.as_posix()
    # `rel == d` 포함(등재 경계와 동일 판정): namespace 자체 relpath 도 안으로 본다(파일 복사는
    # 하위만 나오지만 두 경계를 문자적으로 일치시켜). flavor `@render` 선언(cross-ns 의존물)은
    # `_is_render_managed`(경로-포함)로 그 하위 파일까지 스코프에 넣는다.
    in_scope = (rel_posix == root_doc
                or any(rel_posix == d or rel_posix.startswith(d + "/") for d in adapter_dirs)
                or _is_render_managed(rel_posix, guest_render_paths))
    if not in_scope:
        return False
    # host(dest)가 이미 소유(pm_update 관리)하는 경로는 스코프 안이라도 제외(중복 레이다운 방지).
    if _is_render_managed(rel_posix, owned_paths):
        return False
    return True
