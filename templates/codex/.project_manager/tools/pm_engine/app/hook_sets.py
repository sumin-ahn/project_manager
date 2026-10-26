"""훅 세트 세대 판정·수용 게이트·부분 전파 범위 판정 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import adapter_report
from pm_engine.domain import adapter_specs
from pm_engine.domain import install_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import adapter_documents
from pm_engine.infra import fs
from pm_engine.infra import install_receipt
from pm_engine.infra import upstream_declarations
from typing import NamedTuple
import json
import sys


# ── 훅 세트 세대 정합 (판정 단일 진실 — pm_update/pm_config 는 소비만) ────────────
# 판정 결과의 두 축: 무엇이 어긋났나(kind)와 어느 쪽이 뒤처졌나(remedy). 처방이 갈리는 건
# 후자 하나뿐이라 상수로 못박는다 — 문자열을 호출부가 지어내면 처방이 두 벌이 된다.
HOOK_SET_MISSING_SCRIPT = "missing-hook-script"


HOOK_SET_UNSUPPORTED_FLAG = "unsupported-hook-flag"


HOOK_SET_REMEDY_ENGINE_STALE = "engine-stale"     # dest 엔진 파일이 config 보다 구세대


HOOK_SET_REMEDY_CONFIG_AHEAD = "config-ahead"     # config 가 이 엔진 세대 자체보다 앞섬


HOOK_SET_REMEDY_UNKNOWN = "unknown"               # 비교 기준(상류 template) 미해소


class AdapterHookSetFinding(NamedTuple):
    """훅 세트 세대 불일치 1건. 빈 목록 = 정합(락아웃 위험 없음)."""
    harness: str
    config_relpath: str
    kind: str
    subject: str                  # 문제의 스크립트 relpath 또는 플래그
    unmet_paths: tuple[str, ...]  # 그 세대로 설치돼 있지 않은 dest 파일
    remedy: str
    detail: str


# ── 세대 선언 해소 — **모든 소비자의 단일 지점** ──────────────────────────────
# 훅 세트 선언(`ADAPTER_HOOK_SET`)은 엔진 package(`domain.adapter_specs`) 안에 산다. 그래서 *실행
# 중인* 엔진의 선언은 업그레이드에서 정의상 한 세대 뒤다 — 상류가 새 플래그·새 결합 묶음을 도입하는
# **첫 전파**에서 게이트가 그 세대를 모른 채 통과한다(같은 클래스가 3회 반복 발견됐다). 불변식:
#
#   훅 세트 세대 선언의 **게이트 소비자는 항상 상류 세대 선언으로 판정한다.**
#
# 소비자(원자 write 판정자·수용 게이트·부분 전파 가드·동기 채널 판정)는 전부 이 함수 하나로
# 선언을 받는다. 판정 코드는 실행 중 엔진 것을 그대로 쓰고 **선언 데이터만** 상류에서 온다 —
# 상류 판정 함수를 호출하면 그쪽 버그·시그니처 변화까지 실행 경로에 들어온다.
HOOK_SET_ORIGIN_UPSTREAM = "상류"


HOOK_SET_ORIGIN_LOCAL = "로컬"


HOOK_SET_ORIGIN_UNRESOLVED = "미해소"


def hook_set_declarations(home, source_root=None, *, required: bool = False) -> adapter_specs.HookSetGeneration:
    """훅 세트 세대 선언을 해소한다 — 상류 우선, 폴백은 호출부 성격이 정한다.

    `home` 은 실행 중인 엔진 clone(상류 선언 자식 프로세스의 임시 트리 소유자)이고 `source_root` 는
    읽기만 하는 상류 checkout 이다.

    `required=False`(조회·보고): 상류를 못 읽으면 로컬 선언으로 내려간다. 판정을 아예 잃는 것보다
    한 세대 뒤 선언으로라도 보는 편이 낫고, 사유는 `reasons` 로 올라가 호출부가 알린다.

    `required=True`(**mutation 게이트**): 상류를 못 읽으면 로컬로 내려가지 않는다(`declarations`
    None). 확인되지 않은 세대 위에서 파일을 바꾸는 것이 정확히 이 게이트가 막으려는 상태다 —
    모르면 멈춘다(fail-closed).
    """
    reasons: list[str] = []
    if source_root is not None:
        table, reason, digest = upstream_declarations._upstream_hook_set_declarations(home, source_root)
        if table is not None:
            return adapter_specs.HookSetGeneration(table, HOOK_SET_ORIGIN_UPSTREAM, (), digest)
        if reason:
            reasons.append(reason)
    else:
        reasons.append("상류 좌표 미지정")
    if required:
        return adapter_specs.HookSetGeneration(None, HOOK_SET_ORIGIN_UNRESOLVED, tuple(reasons))
    if adapter_specs.ADAPTER_HOOK_SET:
        return adapter_specs.HookSetGeneration(adapter_specs.ADAPTER_HOOK_SET, HOOK_SET_ORIGIN_LOCAL, tuple(reasons))
    return adapter_specs.HookSetGeneration(None, HOOK_SET_ORIGIN_UNRESOLVED, tuple(reasons))


def hook_set_query_fallback_lines(generation) -> list[str]:
    """조회 축 강등 사유 안내 줄 — 상류 선언을 못 읽고 내려간 판정이면 1줄, 아니면 빈 목록.

    문구는 처방(`hook_set_remedy_lines`)과 같은 규약으로 **여기 단일 진실**이다 — pm-update 와
    `pm-config --check` 가 같은 상태를 서로 다른 문장으로 말하면 채택자가 두 게이트를 다른 것으로
    읽는다. 조회 축은 관대 계약이라 이 줄은 차단이 아니라 침묵 제거다(mutation 축은 fail-closed)."""
    reasons = getattr(generation, "reasons", ())
    if not reasons:
        return []
    return ["[경고] 상류 훅 세트 세대 선언을 읽지 못해 설치본 선언으로 판정한다 — 이번 상류가 "
            "새로 들여오는 플래그·결합 묶음은 이 판정에 보이지 않는다(green 이어도 무판정 구간이 "
            f"있다). {' / '.join(reasons)}"]


def hook_set_namespaces(declarations=None) -> tuple[str, ...]:
    """훅 세트가 사는 **어댑터 네임스페이스 접두** — 선언 데이터에서 파생(하드코딩 0).

    상류 세대를 확인할 수 없을 때 fail-closed 대상을 정하는 축이다. 그 상태에서는 로컬 선언의
    결합 묶음 membership 으로 좁힐 수 없다 — 상류에만 있는 묶음은 정의상 로컬이 모르므로, 좁히면
    바로 그 케이스가 빠져나간다. 그래서 판정 단위를 "이 하네스의 훅이 사는 영역" 으로 올린다."""
    out: set[str] = set()
    for spec in _hook_set_table(declarations).values():
        if spec is None:
            continue
        for declared in spec.live_files:
            head = declared.strip("/").split("/", 1)[0]
            if head:
                out.add(head)
    return tuple(sorted(out))


def _hook_set_table(declarations) -> dict:
    """판정이 쓸 선언 테이블 — 미지정이면 이 엔진 선언(조회 기본값·게이트는 명시 전달)."""
    return adapter_specs.ADAPTER_HOOK_SET if declarations is None else declarations


def _is_hook_set_file(relpath: str, spec: adapter_specs.AdapterHookSetSpec) -> bool:
    """그 relpath 가 이 하네스의 훅 세트 파일인가 (파일 동일 또는 디렉토리 접두)."""
    for declared in spec.live_files:
        if declared.endswith("/"):
            if relpath.startswith(declared):
                return True
        elif relpath == declared:
            return True
    return False


def _hook_set_reference_prefix(relpath: str, spec: adapter_specs.AdapterHookSetSpec) -> str | None:
    """훅 커맨드가 가리킨 경로 → 그 훅 세트가 놓인 **디렉토리 접두**(선언 좌표면 빈 문자열).

    선언 좌표(`.claude/…`)로 부르는 게 채택자 형상이지만, 같은 엔진 파일을 **다른 좌표**로 부르는
    토폴로지가 실재한다 — 제품 루트의 settings.json 은 자기 `.claude/` 에 훅이 없어
    `templates/claude_code/.claude/ctx_stop_hook.sh` 를 부른다. 좌표가 선언과 다르다는 이유로 그
    커맨드를 검사에서 빼면 그 트리는 세대 검사가 통째로 꺼진다(stale 드라이버가 green). 접두를
    뽑아 두면 플래그 요구 파일까지 같은 접두로 판정할 수 있다(체인 전체가 같은 자리에 산다).

    훅 세트 밖(관련 없는 스크립트·트리 밖 좌표)이면 None — 채택자 자작 훅은 판정 대상이 아니다."""
    if _is_hook_set_file(relpath, spec):
        return ""
    for declared in spec.live_files:
        marker = "/" + declared.rstrip("/")
        if declared.endswith("/"):
            index = relpath.find(marker + "/")
            if index > 0:
                return relpath[:index]
        elif relpath.endswith(marker) and len(relpath) > len(marker):
            return relpath[:-len(marker)]
    return None


def _with_hook_set_prefix(prefix: str, relpath: str) -> str:
    """선언 relpath 를 그 훅 세트가 실제로 놓인 좌표로 옮긴다(접두 없으면 그대로)."""
    return f"{prefix}/{relpath}" if prefix else relpath


def _hook_file_declares(path: Path, literal: str, *, unknown_supported: bool) -> bool:
    """설치된 훅 파일이 그 리터럴(플래그)을 담고 있는가 — 실행 없이 하는 세대 판정.

    훅을 실제로 실행해 보는 판정은 그 자체가 부작용(락아웃 재현)이라 쓸 수 없다. 파일 부재는
    항상 미지원이고, **판정 불가**(권한·바이너리·해독 실패)는 호출부가 방향을 고른다:

      `unknown_supported=True`  조회(report) — 판정 불가를 red 로 올리면 거짓 처방이 나간다.
      `unknown_supported=False` 수용(mutation) — 확인되지 않은 파일을 근거로 config 를 새 세대로
                                앞세우면 그게 곧 락아웃이다. 모르면 진행하지 않는다(fail-closed).
    """
    try:
        return literal in fs.read_text_shared(path, encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError):
        return unknown_supported


def _hook_set_demands(document, spec: adapter_specs.AdapterHookSetSpec, root: Path, *,
                      unknown_supported: bool = True
                      ) -> dict[tuple[str, str], tuple[str, ...]]:
    """그 config 가 요구하는 것 중 `root` 트리가 충족하지 못한 항목 — {(kind, subject): 미충족}.

    `root` 는 판정 대상 트리다: 채택자 dest 를 주면 "지금 설치된 세대가 이 config 를 감당하나",
    상류 template 루트를 주면 "이 엔진 세대가 애초에 그걸 줄 수 있나" 가 된다. 처방 분기(어느
    쪽이 뒤처졌나)를 같은 코어 하나로 판정하려고 트리를 파라미터로 뺐다.

    `unknown_supported` 는 판정 불가 파일의 방향이다(`_hook_file_declares` 참조) — 기본은 조회용
    관대 판정이고, 수용 게이트만 fail-closed 로 뒤집는다."""
    out: dict[tuple[str, str], tuple[str, ...]] = {}

    def _record(kind: str, subject: str, unmet: tuple[str, ...]) -> None:
        # 같은 플래그를 여러 좌표에서 부르면(선언 좌표 + template 하위 참조) 미충족을 합친다 —
        #   뒤 커맨드가 앞 판정을 덮으면 한쪽 좌표의 구세대가 조용히 사라진다.
        merged = dict.fromkeys(out.get((kind, subject), ()) + unmet)
        out[(kind, subject)] = tuple(merged)

    for command in adapter_documents._hook_commands(document):
        tokens = adapter_documents._split_hook_command(command)
        if not tokens:
            continue
        script_rel, arguments = adapter_documents._hook_script_and_arguments(tokens)
        if script_rel is None:
            continue
        prefix = _hook_set_reference_prefix(script_rel, spec)
        if prefix is None:
            continue  # 엔진 소유 훅 세트 밖 — 채택자 자작 훅은 판정 대상이 아니다.
        if not (Path(root) / script_rel).is_file():
            _record(HOOK_SET_MISSING_SCRIPT, script_rel, (script_rel,))
            continue  # 스크립트가 없으면 플래그 지원 여부는 물을 것도 없다.
        for token in arguments:
            required = spec.flag_support.get(token)
            if not required:
                continue  # 선언 밖 플래그 — 지원 여부를 기계가 알 수 없다(판정 유보).
            unmet = tuple(
                actual for actual in
                (_with_hook_set_prefix(prefix, rel) for rel in required)
                if not _hook_file_declares(Path(root) / actual, token,
                                           unknown_supported=unknown_supported))
            if unmet:
                _record(HOOK_SET_UNSUPPORTED_FLAG, token, unmet)
    return out


def _hook_set_template_root(source_root: Path | None, harness: str) -> Path | None:
    """그 하네스의 상류 template 루트 — 처방 분기의 비교 기준(해소 불가면 None)."""
    if source_root is None:
        return None
    template_dir = install_rules.HARNESS_TEMPLATE_DIRS.get(harness, (None,))[0]
    if template_dir is None:
        return None
    root = Path(source_root) / "templates" / template_dir
    return root if root.is_dir() else None


def _read_hook_set_config(path: Path):
    """훅 세트 config 파싱 — 부재/파손이면 None(채택자 소유 파일이라 임의 상태가 정상 범위)."""
    try:
        return json.loads(fs.read_text_shared(path, encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _hook_set_finding(harness: str, spec: adapter_specs.AdapterHookSetSpec, kind: str, subject: str,
                      unmet: tuple[str, ...], remedy: str) -> AdapterHookSetFinding:
    joined = ", ".join(unmet)
    if kind == HOOK_SET_MISSING_SCRIPT:
        detail = (f"{spec.config_relpath} 이 실행하는 훅 스크립트가 설치돼 있지 않다 "
                  f"({subject}) — 이 훅은 매 발화마다 실패한다")
    else:
        detail = (f"{spec.config_relpath} 이 넘기는 `{subject}` 를 설치된 훅 세트가 "
                  f"지원하지 않는다 ({joined}) — 구 드라이버는 미지원 플래그를 rc2 로 거부하고 "
                  f"그 rc 는 도구 차단으로 번역된다(락아웃)")
    return AdapterHookSetFinding(
        harness, spec.config_relpath, kind, subject, unmet, remedy, detail)


def judge_adapter_hook_sets(dest_root: Path, source_root: Path | None = None,
                            harnesses=None, *,
                            declarations=None) -> list[AdapterHookSetFinding]:
    """설치된 훅 세트가 채택자 config 가 요구하는 세대인지 판정 (읽기 전용·부작용 0).

    pm_update 동기 경로와 `sync-adapter-config` 가 같은 판정을 본다(판정 사본 0). `source_root`
    를 주면 처방까지 분기한다 — 상류 template 이 그 요구를 충족하면 dest 엔진 파일이 뒤처진
    것이고(pm-update 로 자가 수리), 상류도 못 주면 config 가 이 엔진 세대보다 앞선 것이다
    (채택자 소유라 엔진이 못 덮으므로 `sync-adapter-config --accept` 가 유일 채널).

    `declarations` 는 세대 선언 테이블이다(`hook_set_declarations`) — 게이트 호출부는 상류
    세대를 명시로 넘긴다. 미지정이면 이 엔진 선언(설치본)으로 판정한다."""
    dest_root = Path(dest_root)
    table = _hook_set_table(declarations)
    names = (list(harnesses) if harnesses is not None
             else install_receipt.installed_harnesses(dest_root, source_root))
    out: list[AdapterHookSetFinding] = []
    for harness in names:
        spec = table.get(harness)
        if spec is None or not spec.config_relpath:
            continue
        document = _read_hook_set_config(dest_root / spec.config_relpath)
        if document is None:
            continue
        dest_unmet = _hook_set_demands(document, spec, dest_root)
        if not dest_unmet:
            continue
        template_root = _hook_set_template_root(source_root, harness)
        template_unmet = ({} if template_root is None
                          else _hook_set_demands(document, spec, template_root))
        for (kind, subject), unmet in sorted(dest_unmet.items()):
            if template_root is None:
                remedy = HOOK_SET_REMEDY_UNKNOWN
            elif (kind, subject) in template_unmet:
                remedy = HOOK_SET_REMEDY_CONFIG_AHEAD
            else:
                remedy = HOOK_SET_REMEDY_ENGINE_STALE
            out.append(_hook_set_finding(harness, spec, kind, subject, unmet, remedy))
    return out


# ── 역방향 축: 이 엔진 세대가 기대하는 진입점이 config 에 있나 ───────────────────
# 위 `judge_adapter_hook_sets` 는 **config → 엔진** 한 방향만 본다(config 가 요구하는 플래그를
# 설치본이 감당하나). 그 방향만으로는 "진입점이 아예 없어서 앞으로 등록될 가드가 발화하지 않는" 상태가
# 판정 밖이다 — config 가 아무것도 요구하지 않으면 미충족도 0 이라 green 이다. 이 절이 반대
# 방향을 채운다.
#
# 판정은 **loud advisory** 다(차단 아님). config 는 채택자 소유이고 그 파일엔 실 노브가 있어,
# 진입점 부재를 red 로 올리면 의도적으로 훅을 끈 채택자의 흡수가 영구히 막힌다. 소견은 별도
# 채널로 올라가고 완료 게이트(`_adapter_hook_set_gate_failed`)는 이 축을 소비하지 않는다.
HOOK_ENTRYPOINT_MISSING = "missing-hook-entrypoint"


HOOK_ENTRYPOINT_STALE_DISPATCHER = "stale-hook-dispatcher"


class AdapterHookEntrypointFinding(NamedTuple):
    """진입점 역방향 소견 하나 — 무엇이 어디서 빠졌는지와 사람이 읽을 사유."""
    harness: str
    config_relpath: str
    kind: str
    event: str
    matcher: str | None
    dispatcher: str
    detail: str


def entrypoint_invocation(entrypoint: adapter_specs.AdapterHookEntrypoint) -> str | None:
    """그 진입점이 커맨드에서 **호출해야 하는 값**(`--hook-dispatch PreToolUse`).

    플래그가 없는 진입점(claude 처럼 이벤트마다 커맨드 자체가 갈리는 형상)은 호출 값이 없다 —
    그 축의 세대 결합은 `flag_support` 가 따로 본다."""
    return None if not entrypoint.flag else f"{entrypoint.flag} {entrypoint.event}"


def _entrypoint_dispatcher_gap(dest_root: Path,
                               entrypoint: adapter_specs.AdapterHookEntrypoint) -> str | None:
    """설치된 디스패처가 진입점이 넘기는 **값**을 감당하는가 — 못 하면 빠진 리터럴을 돌려준다.

    기준을 플래그 하나가 아니라 `--hook-dispatch <이벤트>` 두 값으로 두는 이유는, 플래그는
    아는데 그 이벤트는 모르는 세대가 실재하기 때문이다(진입점 집합이 늘어난 릴리즈의 흡수
    창). 그 조합에서 훅은 매 발화마다 폴백으로 빠지므로 config 판정과 같은 값으로 본다."""
    if not entrypoint.flag:
        return None
    path = dest_root / entrypoint.dispatcher
    for literal in (entrypoint.flag, entrypoint.event):
        if not _hook_file_declares(path, literal, unknown_supported=True):
            return literal
    return None


def judge_adapter_hook_entrypoints(dest_root: Path, source_root: Path | None = None,
                                   harnesses=None, *,
                                   declarations=None) -> list[AdapterHookEntrypointFinding]:
    """설치된 채택자 config 가 이 엔진 세대의 진입점을 갖고 있는지 판정 (읽기 전용·advisory).

    `source_root` 는 형제 API 와 시그니처를 맞추기 위한 자리다 — 이 축의 기준은 상류 트리가 아니라
    **선언**(`declarations`)이라 판정에 쓰지 않는다. config 부재·파손은 소견을 내지 않는다: 그건
    어댑터 config 채널이 이미 자기 문구로 말하는 상태이고, 여기서 겹쳐 말하면 처방이 두 벌이 된다.
    """
    dest_root = Path(dest_root)
    table = _hook_set_table(declarations)
    names = (list(harnesses) if harnesses is not None
             else install_receipt.installed_harnesses(dest_root, source_root))
    out: list[AdapterHookEntrypointFinding] = []
    for harness in names:
        spec = table.get(harness)
        # 구세대 선언 사본에는 이 필드가 없다 — 그 세대엔 판정할 진입점 개념이 아예 없다.
        entrypoints = getattr(spec, "entrypoints", ()) if spec is not None else ()
        if spec is None or not spec.config_relpath or not entrypoints:
            continue
        document = _read_hook_set_config(dest_root / spec.config_relpath)
        if document is None:
            continue
        for entrypoint in entrypoints:
            if not adapter_documents._entrypoint_is_present(document, entrypoint):
                out.append(AdapterHookEntrypointFinding(
                    harness, spec.config_relpath, HOOK_ENTRYPOINT_MISSING,
                    entrypoint.event, entrypoint.matcher, entrypoint.dispatcher,
                    f"{spec.config_relpath} 에 {entrypoint.event} 범용 진입점"
                    f"(matcher {entrypoint.matcher!r} → {entrypoint.dispatcher})이 없다 — "
                    f"이 이벤트에 앞으로 등록될 가드는 발화하지 않는다"
                    f"(옛 직결 배선이 남아 있으면 그 기능 자체는 계속 돈다)"))
                continue
            gap = _entrypoint_dispatcher_gap(dest_root, entrypoint)
            if gap is not None:
                out.append(AdapterHookEntrypointFinding(
                    harness, spec.config_relpath, HOOK_ENTRYPOINT_STALE_DISPATCHER,
                    entrypoint.event, entrypoint.matcher, entrypoint.dispatcher,
                    f"설치된 {entrypoint.dispatcher} 가 {entrypoint.event} 진입점이 넘기는 "
                    f"`{entrypoint_invocation(entrypoint)}` 를 감당하지 않는다"
                    f"(`{gap}` 미보유) — 훅이 매 발화마다 폴백으로 빠져 가드가 무음 통과한다"))
    return out


def hook_entrypoint_advisory_lines(
        finding: AdapterHookEntrypointFinding) -> list[str]:
    """진입점 소견의 처방 줄 — pm-update 와 형제 소비자가 같은 문구를 낸다(처방 사본 0)."""
    if finding.kind == HOOK_ENTRYPOINT_STALE_DISPATCHER:
        return [f"pm-update 로 {finding.dispatcher} 를 먼저 받아라"]
    return [f"pm-config sync-adapter-config --accept {finding.config_relpath} "
            f"(이 엔진 세대의 진입점을 담은 config 로 되돌린다)",
            "채택자가 그 이벤트의 훅을 의도적으로 끈 상태면 이 줄은 무시해도 된다(차단 아님)"]


def hook_set_remedy_lines(finding: AdapterHookSetFinding) -> list[str]:
    """처방 줄 — pm_update 와 pm_config 가 같은 문구를 낸다(처방 사본 0).

    manifest 소유 파일(래퍼·드라이버)은 엔진이 직접 고칠 수 있고, 채택자 소유 config 는 수용
    커맨드가 유일 채널이다. 순서는 항상 **엔진 파일 먼저** — 반대 순서가 지금 닫는 락아웃이다."""
    accept = (f"pm-config sync-adapter-config --accept {finding.config_relpath} "
              f"(백업 후 이 엔진 세대의 config 로 되돌린다)")
    # 지목할 파일이 없는 소견도 있다 — 상류 세대 자체를 못 읽어 **무엇이 미충족인지 열거할 수
    #   없는** blocker(`unmet_paths=()`)가 그렇다. 빈 괄호 `()` 를 붙이면 처방이 깨진 문장으로
    #   보이므로 괄호째 생략한다(사유는 `detail` 이 이미 말한다).
    engine_first = "pm-update 로 훅 세트 엔진 파일을 먼저 받아라"
    if finding.unmet_paths:
        engine_first += f" ({', '.join(finding.unmet_paths)})"
    if finding.remedy == HOOK_SET_REMEDY_ENGINE_STALE:
        return [engine_first]
    if finding.remedy == HOOK_SET_REMEDY_CONFIG_AHEAD:
        return [f"{finding.config_relpath} 가 이 엔진 세대보다 앞선 요구를 담고 있다 — {accept}"]
    return [engine_first, f"그래도 남으면 {accept}"]


class HookSetAcceptDecision(NamedTuple):
    """수용 전 세대 판정 결과 — 막는 사유와 **판정에 쓴 template bytes 의 해시**를 함께 낸다.

    blockers          비어 있지 않으면 호출부는 파일을 건드리지 않고 거부한다.
    template_sha256   판정 시점 template 내용의 해시(없으면 None).
    generation_sha256 판정에 쓴 **선언 소스**(상류 엔진 package) 그 시점 snapshot digest.
    generation        판정에 쓴 선언의 출처(`HOOK_SET_ORIGIN_*`).
    reasons           상류 선언을 못 읽었을 때의 사유(호출부가 loud 로 낸다).

    두 해시는 **하나의 스냅샷**이다 — 호출부가 그대로 `accept_adapter_config` 에 넘겨 쓰기 직전에
    함께 재확인한다. template 만 묶으면 "선언 해소 뒤 상류가 통째로 갱신됐는데 그 config 파일만
    우연히 같은" 조합이 구 선언 판정으로 통과한다(어느 쪽이 변했든 중단해야 한다).
    """
    blockers: list
    template_sha256: str | None
    generation: str
    reasons: tuple[str, ...] = ()
    generation_sha256: str | None = None


def hook_set_accept_decision(dest_root: Path, source_root: Path, relpath: str, *,
                             declarations=None) -> HookSetAcceptDecision:
    """그 config 를 상류 값으로 수용해도 되는지 — 순서 게이트 판정 + 판정한 bytes 결속.

    판정 대상은 **들어올 config**(상류 template)의 요구다 — 수용은 dest config 를 그 세대로
    앞세우는 행위이므로, 그 세대의 래퍼·드라이버가 dest 에 이미 있어야 한다. 이 검사가 순서
    (엔진 파일 선행 · config 후행)를 기계가 강제하는 지점이다.

    두 축이 모두 fail-closed 다:
      - 읽을 수 없는 훅 파일은 "지원함" 으로 넘기지 않는다(`unknown_supported=False`).
      - 세대 선언은 **상류**여야 한다(`declarations` 필수) — 설치본 선언은 상류가 이번에 들여오는
        새 플래그를 모르므로, 그 위에서 config 를 앞세우면 게이트가 있으나 마나다. 상류를 못 읽어
        `declarations` 가 None 이면 판정 불가를 blocker 로 낸다(모르면 멈춘다).
    """
    dest_root = Path(dest_root)
    out: list[AdapterHookSetFinding] = []
    if not (dest_root / relpath).is_file():
        # 그 config 가 dest 에 없으면 수용 자체가 성립하지 않는다 — 순서 게이트가 아니라
        #   채널 판정이 낼 오류다(여기서 가로채면 엉뚱한 처방이 나간다).
        return HookSetAcceptDecision([], None, HOOK_SET_ORIGIN_UNRESOLVED, ())
    generation = (declarations if isinstance(declarations, adapter_specs.HookSetGeneration)
                  else adapter_specs.HookSetGeneration(declarations, HOOK_SET_ORIGIN_LOCAL, ()))
    template_sha256: str | None = None
    targets = {rel: template for rel, _h, _m, template
               in adapter_report.adapter_config_targets(dest_root, source_root)}
    template_path = targets.get(relpath)
    if template_path is not None:
        # 여기 `file_sha256` 은 내용 판정이 아니라 **설치할 상류 bytes 의 스냅샷 결속**이다 —
        #   수용 직전 재대조(`expected_template_sha256`)와 같은 raw 축이어야 결속이 성립한다.
        template_sha256 = adapter_baseline.file_sha256(template_path)
    if generation.declarations is None:
        blocker = AdapterHookSetFinding(
            "-", relpath, HOOK_SET_MISSING_SCRIPT, relpath, (), HOOK_SET_REMEDY_UNKNOWN,
            f"상류 세대 선언을 읽지 못해 {relpath} 가 요구하는 훅 세대를 검증할 수 없다 "
            f"({'; '.join(generation.reasons) or '사유 미상'})")
        return HookSetAcceptDecision(
            [blocker], template_sha256, generation.origin, generation.reasons,
            generation.source_sha256)
    # 스냅샷 해시를 못 만들면 결속이 조용히 생략된다(호출부의 `is not None` 가드) — mutation 축은
    #   모르면 멈춘다. **판정한 세대를 재확인할 수 없는 상태**를 blocker 로 올린다.
    #   상류 선언으로 판정한 경로에서만 본다 — 결속을 약속하는 게 그 경로이고, 설치본 선언으로
    #   판정한 결과의 None 은 계산 실패가 아니라 애초에 결속이 없는 계약이다.
    unreadable = [] if generation.origin != HOOK_SET_ORIGIN_UPSTREAM else [
        name for name, value in
        (("상류 config template", template_sha256),
         ("상류 세대 선언 package", generation.source_sha256))
        if value is None]
    if unreadable:
        blocker = AdapterHookSetFinding(
            "-", relpath, HOOK_SET_MISSING_SCRIPT, relpath, (), HOOK_SET_REMEDY_UNKNOWN,
            f"{', '.join(unreadable)} 의 해시를 만들지 못해 판정한 스냅샷을 쓰기 직전에 재확인할 "
            f"수 없다 — 검증 없이 {relpath} 를 교체하지 않는다")
        return HookSetAcceptDecision(
            [blocker], template_sha256, generation.origin, generation.reasons,
            generation.source_sha256)
    for harness, spec in generation.declarations.items():
        if spec is None or spec.config_relpath != relpath:
            continue
        template_root = _hook_set_template_root(source_root, harness)
        if template_root is None:
            continue
        document = _read_hook_set_config(template_root / spec.config_relpath)
        if document is None:
            continue
        for (kind, subject), unmet in sorted(
                _hook_set_demands(document, spec, dest_root,
                                  unknown_supported=False).items()):
            out.append(_hook_set_finding(
                harness, spec, kind, subject, unmet, HOOK_SET_REMEDY_ENGINE_STALE))
    return HookSetAcceptDecision(
        out, template_sha256, generation.origin, generation.reasons,
        generation.source_sha256)


def _entry_holds_path(declared: str, path: str) -> bool:
    """그 dest 경로가 선언 항목에 속하는가 (파일 항목은 동일·디렉토리 항목은 접두)."""
    norm = declared.rstrip("/")
    return path == norm or path.startswith(norm + "/")


def _entries_hit_by(group: tuple[str, ...], paths: set[str]) -> set[str]:
    """그 경로 집합이 건드리는 묶음 항목."""
    return {declared for declared in group
            if any(_entry_holds_path(declared, path) for path in paths)}


def hook_set_partial_update(updated_paths, pending_paths, *,
                            declarations=None) -> list[tuple[str, tuple, tuple]]:
    """이번 전파가 결합 묶음을 **반쪽만** 갱신하는가 — (harness, 남겨진 항목, 함께 옮길 항목).

    경로 스코프 전파(`--paths`)는 어댑터 채널을 끄므로 세대 검사가 전무하다. 그 상태에서 래퍼만
    옮기면 "신 래퍼 + 구 드라이버" 를 손수 만들어 놓고 rc0 로 끝난다(락아웃 생성). 판정 단위는
    **결합 묶음**(`coupled_groups`)이다 — 결합이 없는 파일(독립 relay 드라이버 등)까지 묶으면
    정당한 단건 전파를 막는다.

    입력은 **해소된 계획의 dest 좌표**다:
      `updated_paths` 이번 실행이 갱신할 경로 · `pending_paths` 스코프가 없었다면 갱신됐을 경로.
    원문 스코프 표기를 보면 `@source` 상류 좌표 요청이 선언과 교집합 0 으로 빠져 검사가 무발화하고,
    "묶음 전량이 스코프에 있나" 만 보면 **이미 최신인 형제**까지 요구해 거짓 거부가 된다. 그래서
    조건은 하나다: 이 묶음에서 뭔가 옮기는데(`updated`) **옮겨야 할 것이 남으면**(`pending`) 거부.

    `declarations` 는 세대 선언 테이블이다(`hook_set_declarations`) — 상류가 이번에 들여오는 새
    결합 묶음은 설치본 선언에 없으므로, 게이트 호출부는 상류 세대를 명시로 넘긴다."""
    table = _hook_set_table(declarations)
    updated = {str(path).replace("\\", "/").strip("/")
               for path in (updated_paths or []) if str(path).strip()}
    pending = {str(path).replace("\\", "/").strip("/")
               for path in (pending_paths or []) if str(path).strip()}
    if not updated:
        return []
    out: list[tuple[str, tuple, tuple]] = []
    for harness, spec in table.items():
        if spec is None:
            continue
        for group in spec.coupled_groups:
            moving = _entries_hit_by(group, updated)
            if not moving:
                continue
            left_behind = _entries_hit_by(group, pending) - moving
            if left_behind:
                order = {declared: index for index, declared in enumerate(group)}
                out.append((
                    harness,
                    tuple(sorted(left_behind, key=order.get)),
                    tuple(sorted(moving | left_behind, key=order.get)),
                ))
    return out


def coupled_hook_set_paths(paths, *, declarations=None) -> tuple[str, ...]:
    """그 경로들 중 **결합 묶음에 속한** 것 — 상류 세대를 못 읽었을 때 fail-closed 대상 판별.

    결합이 없는 훅 파일(독립 relay 드라이버 등)은 반쪽 갱신이라는 개념 자체가 없으므로, 세대를
    검증하지 못해도 단건 전파를 막지 않는다(가드 과잉 금지).

    `declarations` 는 형제 API 전부와 같은 **kw-only** 다(`judge_adapter_hook_sets`·
    `hook_set_partial_update`·`is_live_hook_set_path` 계열) — 세대 선언을 위치인자로 받는 지점이
    하나라도 있으면 호출부가 표기를 헷갈리고, 인자 하나가 밀려도 조용히 통과한다."""
    table = _hook_set_table(declarations)
    out: list[str] = []
    for path in paths:
        norm = str(path).replace("\\", "/").strip("/")
        for spec in table.values():
            if spec is None:
                continue
            if any(_entries_hit_by(group, {norm}) for group in spec.coupled_groups):
                out.append(norm)
                break
    return tuple(out)


def live_hook_set_paths(declarations=None) -> tuple[str, ...]:
    """실행 중 하네스가 읽는 훅 세트 파일 전수(하네스 합집합·`/` 끝 = 디렉토리 접두)."""
    out: set[str] = set()
    for spec in _hook_set_table(declarations).values():
        if spec is not None:
            out.update(spec.live_files)
    return tuple(sorted(out))


def is_live_hook_set_path(relpath: str, declarations=None) -> bool:
    """동기가 그 dest 경로를 **원자 교체**해야 하는가 (하네스가 실행 중 읽는 파일인가).

    copy2 는 dest 를 truncate 한 뒤 채우므로 그 창에 하네스가 읽으면 부분 파일을 실행한다.
    훅 세트만 원자 write 로 올린다 — 전 파일 확대는 이 클래스가 요구하지 않는다. `declarations`
    미지정이면 이 엔진 선언(설치본)으로 판정한다(호출부가 상류 세대를 명시로 넘긴다)."""
    text = str(relpath).replace("\\", "/")
    for declared in live_hook_set_paths(declarations):
        if declared.endswith("/"):
            if text.startswith(declared):
                return True
        elif text == declared:
            return True
    return False


def resolve_hook_set_predicate(home, source_root=None):
    """훅 세트 원자-write 판정자 — 상류 세대 선언(못 읽으면 `home` 설치본 선언)으로 만든다.

    선언 해소·판정은 이 모듈의 세대 값(`hook_set_declarations`)이 단일 진실이다(사본을 두면 "검사는
    하는데 원자 write 는 안 하는" 파일이 조용히 생긴다). 해소는 실행당 1회이고 결과를 `apply` 에
    넘긴다 — 상류 선언 조회는 매번 별도 프로세스를 띄우므로 파일마다 부르지 않는다.

    상류를 주고도 그 선언을 못 읽으면 설치본 선언으로 판정하고 그 사실을 stderr 한 줄로 남긴다 —
    이번 상류가 새로 등재하는 훅 파일은 그 판정에서 빠져 일반 복사로 떨어진다."""
    generation = hook_set_declarations(home, source_root)
    if source_root is not None and generation.reasons:
        print(
            "[경고] 상류 훅 세트 선언을 읽지 못해 설치본 세대로 판정한다 — 이번 상류가 새로 "
            f"등재하는 훅 파일은 원자 교체 대상에서 빠진다. {' / '.join(generation.reasons)}",
            file=sys.stderr,
        )
    return lambda rel, _t=generation.declarations: bool(
        is_live_hook_set_path(str(rel).replace("\\", "/"), _t))
