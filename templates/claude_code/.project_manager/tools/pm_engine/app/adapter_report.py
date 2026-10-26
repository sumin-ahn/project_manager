"""어댑터 기능 차이·config 수렴·상류 template 차이 보고 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import adapter_templates
from pm_engine.domain import adapter_specs
from pm_engine.domain import install_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import adapter_documents
from pm_engine.infra import fs
from pm_engine.infra import install_git
from pm_engine.infra import install_receipt
from pm_engine.infra import upstream_git
from typing import NamedTuple
import difflib


def instance_owned_template_delta_lines(
        dest_root: Path, source_root: Path, *,
        git_runner: install_git.GitRunner | None = None) -> list[str]:
    """instance-owned 파일의 baseline template 세대 ↔ 현행 세대 델타 요약.

    파일별 ``adapter_baseline.json.template_rev``를 우선하고, 항목이 없는 진입문서는 직전
    ``local.conf upstream.rev``로 폴백한다. 판정은 source checkout의 로컬 object DB만 읽고
    파일을 쓰거나 자동 병합하지 않는다. 기준이 없거나 도달하지 않으면 그것을 변경 없음으로
    접지 않고 전량 확인 경고 한 줄로 축약한다.

    노출 수명은 경로별 기준 원장을 따른다. 진입 문서처럼 ``upstream.rev``를 폴백하는
    경로는 성공한 동기 후 기준이 전진해 이후 요약에서 사라질 수 있다. 반면 report-drift·edited
    managed 파일은 보존된 ``adapter_baseline.json.template_rev``가 기준이다. pm-update가
    무편집 managed 파일을 자동 갱신·원장화한 뒤 이 판정을 호출하면 그 항목은 사라지고, 원장이
    전진하지 않은 report-drift·edited managed만 명시적 ``--accept`` 전까지 반복된다. 어느 쪽도
    지난 세대를 복구하는 durable backstop은 아니며, 그 역할은 백업/git이 담당한다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    targets = adapter_templates.instance_owned_template_targets(dest_root, source_root)
    if not targets:
        return []
    recorded = adapter_baseline.read_adapter_baseline(dest_root).get("files", {})
    fallback_rev = adapter_templates._instance_owned_fallback_rev(dest_root)
    # pm-update는 이 함수를 adapter 채널의 쓰기 후 최종 판정에서 호출한다. dirty source처럼
    # template_rev만으로 현행 bytes를 정확히 이름 붙일 수 없는 경우에도, managed 파일과 원장이
    # 모두 현행 template에 수렴했다면 같은 파일에 --accept를 다시 처방하지 않는다. 동기 직전의
    # unedited/edited·원장 미기록은 converged가 아니므로 기존 처방을 그대로 유지한다.
    managed_converged: set[str] = set()
    try:
        managed_converged = {
            judgment.relpath
            for judgment in judge_adapter_configs(dest_root, source_root)
            if (judgment.mode == install_rules.ADAPTER_CONFIG_MANAGED
                and adapter_config_convergence_status(judgment) == "converged")
        }
    except (OSError, ValueError, adapter_baseline.AdapterConfigChannelUnavailable):
        # 세대 관측이 config 판정 실패를 숨기면 안 된다. 수렴을 증명하지 못한 경우는 아무 항목도
        # 자동 완료로 접지 않고 아래의 원장 기준 경고/처방을 보존한다.
        managed_converged = set()
    runner = git_runner if git_runner is not None else upstream_git._real_upstream_git_runner()
    prefix_status, prefix_path = adapter_templates._source_git_path_prefix(source_root, runner)
    if prefix_status == adapter_templates.SOURCE_PREFIX_NOT_GIT:
        return ["ℹ️  이 소스는 git 이 아니라 세대 판정 비대상."]
    if prefix_status != adapter_templates.SOURCE_PREFIX_OK:
        return [
            "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — "
            "판정 불가(변경 없음 아님)·전량 확인 권장."
        ]
    reachability: dict[str, bool] = {}
    findings: list[str] = []
    coordinate_unknown = False
    baseline_unknown = False
    baseline_unreachable = False

    for target in targets:
        if target.template is None or target.template_git_relpath is None:
            coordinate_unknown = True
            continue
        entry = recorded.get(target.relpath)
        entry_rev = entry.get("template_rev") if isinstance(entry, dict) else None
        baseline_rev = entry_rev.strip() if isinstance(entry_rev, str) else ""
        baseline_rev = baseline_rev or fallback_rev
        if not baseline_rev:
            baseline_unknown = True
            continue

        if baseline_rev not in reachability:
            reachability[baseline_rev] = adapter_templates._baseline_rev_reachable(
                source_root, baseline_rev, runner)
        if not reachability[baseline_rev]:
            baseline_unreachable = True
            continue

        try:
            # dirty source checkout도 현행 배달 후보인 on-disk bytes와 대조하는 것이 의도다.
            current_lines = fs.read_text_shared(target.template, encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            baseline_unreachable = True
            continue

        git_relpath = (prefix_path / target.template_git_relpath).as_posix()

        blob_status, old_text = adapter_templates._template_blob_at_rev(
            source_root, baseline_rev, git_relpath, runner)
        if blob_status == adapter_templates.TEMPLATE_BLOB_UNRESOLVED:
            baseline_unreachable = True
            continue
        old_lines = old_text.splitlines(keepends=True)
        if old_lines == current_lines:
            continue
        if target.mode == install_rules.ADAPTER_CONFIG_MANAGED and target.relpath in managed_converged:
            # pm-update의 config 채널이 이미 "이번 동기에서 갱신"을 별도 한 줄로 보고한다.
            # 여기서는 최종 상태와 모순되는 --accept 재처방을 만들지 않는다.
            continue
        added, removed, first_diff = adapter_templates._template_generation_size(
            old_lines, current_lines)
        if target.mode == install_rules.ADAPTER_CONFIG_NO_CHANNEL:
            remedy = (
                "현재 파일을 백업한 뒤 수동 병합 또는 ADOPT.md 절차로 재-import")
        else:
            remedy = f"pm-config sync-adapter-config --accept {target.relpath}"
        findings.append(
            f"⚠️  인스턴스 소유 템플릿 변경 — {target.relpath} · "
            f"+{added}/-{removed}줄 · 첫 차이 {first_diff}줄 · "
            f"처방(세대 변경; 기존 config 채널 보고는 내용 drift): {remedy}"
        )

    prefix: list[str] = []
    if coordinate_unknown:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 좌표 미기록·수동 확인 — "
            "자동 비교 생략(변경 없음 아님).")
    if baseline_unknown:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 기준 미기록 — 판정 불가(변경 없음 아님)·전량 확인 권장.")
    if baseline_unreachable:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — 판정 불가(변경 없음 아님)·전량 확인 권장.")
    return [*prefix, *findings]


# ── 어댑터 기능 단위 거리 (읽기 계약 · `pm-config status --json --with-adapters`) ──────
# 줄 수 차이는 "얼마나" 만 말하고 "무엇이" 를 말하지 않는다. 기능 표는 선언
# (`ADAPTER_SETTINGS_KEYS` + `ADAPTER_HOOK_SET[...].entrypoints`)과 최신 template 을 축으로
# 항목별 판정을 낸다. 판정 어휘는 넷뿐이고 그 밖의 상태를 만들지 않는다.
ADAPTER_FEATURE_SAME = "same"          # 프로젝트 값 == 최신 값.


ADAPTER_FEATURE_MISSING = "missing"    # 최신에는 있고 프로젝트에는 없다.


ADAPTER_FEATURE_DIFFERS = "differs"    # 둘 다 있고 값이 다르다.


ADAPTER_FEATURE_RETIRED = "retired"    # 프로젝트에는 있고 최신에는 없다(폐지).


class AdapterFeature(NamedTuple):
    """기능 표 한 행 — 항목 이름과 양쪽 값 요약, 그리고 판정.

    `project`·`latest` 는 **표시용 요약**이다(모음은 원소 수·그 밖은 값 그대로). 판정은 요약이
    아니라 **원값 동일성**으로 낸다 — 길이만 같고 내용이 다른 allowlist 를 `same` 으로 접지 않는다.
    """
    name: str
    project: object
    latest: object
    verdict: str


class AdapterConfigReport(NamedTuple):
    """instance-owned config 한 개의 읽기 보고 — 판정 + 기준/최신 diff + 기능 표.

    `judge_adapter_configs` 의 판정(`mode`·`status`)을 그대로 싣고 표시 어휘를 새로 만들지 않는다.
    `diff_vs_baseline` 은 설치 기준(`template_rev` 시점 template) → 현재 파일이라 **채택자 편집**의
    크기이고(`added`/`removed` 도 그 축), `diff_vs_latest` 는 현재 파일 → 최신 template 이라
    **상류 변경**이다. 기준을 해소하지 못하면 `None` 이다(빈 문자열 = 차이 없음과 구분한다).

    `status is None` = **선언된 대상인데 판정이 없다**(대개 dest 파일 부재 · template 를 읽지 못한
    report 대상 포함). 부재를 행이 사라지는 것으로 말하지 않는 축이라(관측 인벤토리는 전수) 같은
    사실을 재진술하는 `present` 필드는 두지 않는다 — dest 존재 확인도 여기서 하지 않는다.
    그 행은 `added`/`removed` 도 널이다(잴 기준이 없다 — 0 은 "차이 없음" 이라 다른 사실이다).
    """
    relpath: str
    harness: str
    mode: str
    status: str | None
    template_rev: str | None
    added: int | None
    removed: int | None
    diff_vs_baseline: str | None
    diff_vs_latest: str
    features: tuple[AdapterFeature, ...]
    distance: int


def _document_key_value(document: dict, dotted_key: str) -> tuple[bool, object]:
    """점 표기 경로로 문서 안쪽 값을 찾는다 — (있음, 값). 중간이 dict 가 아니면 없음."""
    node: object = document
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _feature_value_summary(value: object) -> object:
    """표에 싣는 값 요약 — 모음은 원소 수, 그 밖은 값 그대로(JSON 스칼라)."""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _feature_verdict(project_present: bool, project_value: object,
                     latest_present: bool, latest_value: object) -> str | None:
    """네 어휘 중 하나 — 양쪽 다 없으면 행 자체가 없다(None)."""
    if not project_present and not latest_present:
        return None
    if not project_present:
        return ADAPTER_FEATURE_MISSING
    if not latest_present:
        return ADAPTER_FEATURE_RETIRED
    if project_value == latest_value:
        return ADAPTER_FEATURE_SAME
    return ADAPTER_FEATURE_DIFFERS


def _hook_event_units(document, entrypoints: tuple = ()) -> dict[str, tuple[str, ...]]:
    """문서의 `hooks.<이벤트>` → 그 이벤트가 실행하는 **단위** (정렬·중복 제거).

    단위는 `"<matcher>:<스크립트 relpath>[ 인자…]"` 다 — 한 이벤트 안에서도 matcher 나 인자가
    다르면 **다른 것이 실행된다**(출하 claude `PreToolUse` 는 `*` 그룹과 `Bash` 그룹이 같은 래퍼를
    다른 인자로 부른다). 스크립트 집합만 보면 그 그룹 하나를 통째로 지워도 집합이 안 변해 표가
    `same` 으로 접힌다. matcher 없는 그룹의 자리는 빈 문자열이다.

    기능 축은 "이 이벤트에서 무엇이 실행되나" 라 커맨드 문자열이 아니라 **스크립트+인자**가 단위다
    (인터프리터 표기가 달라도 같은 훅이다). 추출 규칙 **둘 다 엔진 것**이다:
      · argv 분해(`_hook_script_and_arguments`) — 직접 실행·인터프리터 선행 커맨드.
      · 진입점 실재 판정(`_entrypoint_is_present`) — 선언된 진입점 한정.
    후자가 없으면 안 된다: 출하 codex 커맨드는 인터프리터 해소를 담은 셸 한 줄(`if command -v …`)
    이라 argv 분해가 스크립트를 못 뽑고, 그러면 **template 과 바이트가 같은 config 도** 이벤트마다
    "다름" 으로 실린다(판정이 표기에 걸린 거짓 신호).
    """
    out: dict[str, tuple[str, ...]] = {}
    for event, commands in adapter_documents._hook_event_commands(document).items():
        units = set()
        for matcher, command in commands:
            script, arguments = adapter_documents._hook_script_and_arguments(adapter_documents._split_hook_command(command))
            if not script:
                continue
            unit = f"{matcher or ''}:{script}"
            units.add(f"{unit} {' '.join(arguments)}" if arguments else unit)
        out[event] = tuple(sorted(units))
    for entrypoint in entrypoints:
        if not adapter_documents._entrypoint_is_present(document, entrypoint):
            continue
        merged = set(out.get(entrypoint.event, ()))
        merged.add(f"{entrypoint.matcher or ''}:{entrypoint.dispatcher}")
        out[entrypoint.event] = tuple(sorted(merged))
    return out


def _provider_without_model_limits(present: bool, value: object) -> tuple[bool, object]:
    """opencode `provider` 값에서 반영이 쓰는 모델 `limit` 을 뺀 비교용 사본 — (있음, 값).

    `limit` 을 지운 뒤 빈 객체가 된 모델 항목·`models`·provider 항목을 차례로 지우고, `provider`
    자체가 비면 없는 것으로 본다. 반영이 전역 설정의 모델로 만든 항목은 `limit` 만 가지므로
    비교에서 사라진다. 객체가 아닌 자리는 손대지 않는다(그 차이는 그대로 보인다)."""
    if not present or not isinstance(value, dict):
        return present, value
    providers: dict = {}
    for provider_name, provider in value.items():
        if isinstance(provider, dict) and isinstance(provider.get("models"), dict):
            models = {}
            for model_name, model in provider["models"].items():
                if isinstance(model, dict):
                    model = {key: item for key, item in model.items() if key != "limit"}
                    if not model:
                        continue
                models[model_name] = model
            provider = {key: item for key, item in provider.items() if key != "models"}
            if models:
                provider["models"] = models
            if not provider:
                continue
        providers[provider_name] = provider
    return bool(providers), (providers or None)


# 기능 표에서 값을 비교하기 전에 관리 키를 빼는 자리 — (파일, 설정 키) → 변환. 반영이 쓰는 값은
#   local.conf 에서 오므로 템플릿과의 차이로 보고하지 않는다(`ADAPTER_SETTINGS_KEYS` 주석).
_ADAPTER_FEATURE_MANAGED_FILTERS = {
    (".opencode/opencode.jsonc", "provider"): _provider_without_model_limits,
}


def _adapter_feature_rows(relpath: str, project_document: dict,
                          latest_document: dict) -> tuple[AdapterFeature, ...]:
    """한 config 파일의 기능 표 — 선언 설정 키 축 + 훅 이벤트 축."""
    rows: list[AdapterFeature] = []
    for key in install_rules.ADAPTER_SETTINGS_KEYS.get(relpath, ()):
        project_present, project_value = _document_key_value(project_document, key)
        latest_present, latest_value = _document_key_value(latest_document, key)
        managed_filter = _ADAPTER_FEATURE_MANAGED_FILTERS.get((relpath, key))
        if managed_filter is not None:
            project_present, project_value = managed_filter(project_present, project_value)
            latest_present, latest_value = managed_filter(latest_present, latest_value)
        verdict = _feature_verdict(project_present, project_value,
                                   latest_present, latest_value)
        if verdict is None:
            continue
        rows.append(AdapterFeature(
            key, _feature_value_summary(project_value) if project_present else None,
            _feature_value_summary(latest_value) if latest_present else None, verdict))

    entrypoints = tuple(
        entrypoint
        for spec in adapter_specs.ADAPTER_HOOK_SET.values() if spec.config_relpath == relpath
        for entrypoint in spec.entrypoints
    )
    project_events = _hook_event_units(project_document, entrypoints)
    latest_events = dict(_hook_event_units(latest_document, entrypoints))
    # 이 엔진 세대가 config 에 기대하는 진입점은 template 에 없더라도 최신 축에 든다 — 선언이
    # 곧 "있어야 하는 것" 이고, 그것이 빠진 채택자는 `missing` 으로 보여야 한다.
    for entrypoint in entrypoints:
        merged = set(latest_events.get(entrypoint.event, ()))
        merged.add(f"{entrypoint.matcher or ''}:{entrypoint.dispatcher}")
        latest_events[entrypoint.event] = tuple(sorted(merged))
    for event in sorted(set(project_events) | set(latest_events)):
        project_units = project_events.get(event)
        latest_units = latest_events.get(event)
        verdict = _feature_verdict(
            project_units is not None, project_units,
            latest_units is not None, latest_units)
        if verdict is None:
            continue
        rows.append(AdapterFeature(
            f"hooks.{event}",
            ", ".join(project_units) if project_units is not None else None,
            ", ".join(latest_units) if latest_units is not None else None,
            verdict))
    return tuple(rows)


def adapter_config_reports(dest_root: Path, source_root: Path) -> list[AdapterConfigReport]:
    """instance-owned config 전수의 읽기 보고 (부작용 0 · 판정은 `judge_adapter_configs` 그대로).

    관리자(`pm-config status --json --with-adapters`)가 파일별 상태·차이·기능 거리를 한 번에 읽는
    자리다. 기준(`template_rev` 시점) 내용은 원장에 없으므로 소스 checkout 의 object DB 에서
    읽는다 — 세대 요약과 **같은 읽기 관용구**(`_source_git_path_prefix`·`_template_blob_at_rev`)를
    쓰고 새 git 경로를 만들지 않는다. 기준을 해소하지 못한 파일은 `diff_vs_baseline=None` 이며
    변경 없음으로 접지 않는다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    judgments = judge_adapter_configs(dest_root, source_root)
    targets = {target.relpath: target
               for target in adapter_templates.instance_owned_template_targets(dest_root, source_root)}
    recorded = adapter_baseline.read_adapter_baseline(dest_root).get("files", {})
    fallback_rev = adapter_templates._instance_owned_fallback_rev(dest_root)
    runner = upstream_git._real_upstream_git_runner()
    prefix_status, prefix_path = adapter_templates._source_git_path_prefix(source_root, runner)
    reachability: dict[str, bool] = {}

    out: list[AdapterConfigReport] = []
    for judgment in judgments:
        relpath = judgment.relpath
        dest_path = dest_root / relpath
        current_lines = fs.read_text_shared(
            dest_path, encoding="utf-8", errors="replace").splitlines(keepends=True)
        latest_lines = fs.read_text_shared(
            judgment.template, encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff_vs_latest = "".join(difflib.unified_diff(
            current_lines, latest_lines,
            fromfile=relpath, tofile=f"{relpath}@latest"))

        entry = recorded.get(relpath)
        entry_rev = entry.get("template_rev") if isinstance(entry, dict) else None
        baseline_rev = entry_rev.strip() if isinstance(entry_rev, str) else ""
        baseline_rev = baseline_rev or fallback_rev or None

        target = targets.get(relpath)
        baseline_lines: list[str] | None = None
        if (baseline_rev and prefix_status == adapter_templates.SOURCE_PREFIX_OK
                and target is not None and target.template_git_relpath):
            if baseline_rev not in reachability:
                reachability[baseline_rev] = adapter_templates._baseline_rev_reachable(
                    source_root, baseline_rev, runner)
            if reachability[baseline_rev]:
                git_relpath = (prefix_path / target.template_git_relpath).as_posix()
                blob_status, baseline_text = adapter_templates._template_blob_at_rev(
                    source_root, baseline_rev, git_relpath, runner)
                if blob_status != adapter_templates.TEMPLATE_BLOB_UNRESOLVED:
                    baseline_lines = baseline_text.splitlines(keepends=True)

        if baseline_lines is None:
            added = removed = None
            diff_vs_baseline = None
            template_rev = baseline_rev
        else:
            added, removed, _first = adapter_templates._template_generation_size(
                baseline_lines, current_lines)
            diff_vs_baseline = "".join(difflib.unified_diff(
                baseline_lines, current_lines,
                fromfile=f"{relpath}@{baseline_rev}", tofile=relpath))
            template_rev = baseline_rev

        features = _adapter_feature_rows(
            relpath,
            adapter_documents._load_adapter_config_document(dest_path),
            adapter_documents._load_adapter_config_document(judgment.template))
        distance = sum(1 for row in features if row.verdict != ADAPTER_FEATURE_SAME)
        out.append(AdapterConfigReport(
            relpath=relpath,
            harness=judgment.harness,
            mode=judgment.mode,
            status=judgment.status,
            template_rev=template_rev,
            added=added,
            removed=removed,
            diff_vs_baseline=diff_vs_baseline,
            diff_vs_latest=diff_vs_latest,
            features=features,
            distance=distance,
        ))
    judged = {judgment.relpath for judgment in judgments}
    # 채널이 있는데 판정이 없는 대상 = dest 에 파일이 없다. 관측 인벤토리는 선언 전수라 그
    # 사실을 행이 사라지는 것으로 말하지 않는다 — 상태만 `None`(파일 없음)이다.
    out.extend(
        AdapterConfigReport(
            relpath=target.relpath,
            harness=target.harness,
            mode=target.mode,
            status=None,
            template_rev=None,
            added=None,
            removed=None,
            diff_vs_baseline=None,
            diff_vs_latest="",
            features=(),
            distance=0,
        )
        for target in targets.values()
        if target.mode != install_rules.ADAPTER_CONFIG_NO_CHANNEL and target.relpath not in judged
    )
    return out


def adapter_config_targets(dest_root: Path, source_root: Path,
                           harnesses=None) -> list[tuple[str, str, str, Path]]:
    """(relpath, harness, mode, template 경로) — 채널을 가진 instance-owned config 전수.

    대상은 **설치 하네스**(`installed_harnesses`)와 **기존 managed 경로가 지목한 하네스**의
    합집합에서 파생하고 분류(`ADAPTER_CONFIG_CHANNEL`)가 mode 를 준다. 후자는 설치 영수증과 완전
    adapter shape가 없는 partial recovery에서도 managed 파일 하나를 빈 판정으로 접지 않는
    보수 경계다. report-only 경로만으로는 하네스를 추론하지 않는다.

    `none` 분류와 **report-only** template 부재는 대상에서 빠진다. 반면 existing managed dest의
    template 부재/읽기 실패는 판정 불능이므로 ``AdapterConfigChannelUnavailable``로 fail-loud한다
    (빈 judgments false-green 차단)."""
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    if harnesses is not None:
        names = list(harnesses)
    else:
        installed, authority = install_receipt._installed_harnesses_with_authority(dest_root, source_root)
        inferred = set(installed)
        # 유효 receipt는 설치 사실의 명시 단일 진실이다. 그 목록이 부정한 하네스를 우연히 함께
        # 놓인 foreign managed 파일 하나로 되살리면 `--accept`가 외래 파일 overwrite를 권한다.
        # 경로 보강은 receipt 부재/손상으로 증거 추론에 내려간 partial recovery에서만 허용한다.
        if authority == "inferred":
            inferred.update(adapter_baseline._existing_managed_adapter_harnesses(dest_root))
        names = [name for name in install_rules.REGISTERED_HARNESSES if name in inferred]
    out: list[tuple[str, str, str, Path]] = []
    for harness in names:
        channel = install_rules.ADAPTER_CONFIG_CHANNEL.get(harness, {})
        template_dir = install_rules.HARNESS_TEMPLATE_DIRS.get(harness, (None,))[0]
        if template_dir is None:
            continue
        template_root = source_root / "templates" / template_dir
        for relpath in sorted(channel):
            mode = channel[relpath]
            if mode == install_rules.ADAPTER_CONFIG_NO_CHANNEL:
                continue
            template = template_root / relpath
            dest_path = dest_root / relpath
            # 여기 `file_sha256` 은 내용 판정이 아니라 **가독성 probe** 다 — 값을 비교하지 않고
            #   None(읽기 실패)만 본다. 정규화 여부와 무관하므로 raw 스트리밍 해시를 그대로 쓴다.
            if not template.is_file() or adapter_baseline.file_sha256(template) is None:
                if (mode == install_rules.ADAPTER_CONFIG_MANAGED
                        and adapter_baseline._adapter_config_path_exists(dest_path)):
                    raise adapter_baseline.AdapterConfigChannelUnavailable(
                        f"managed 어댑터 config 비교 기준 unavailable: {relpath}의 source template을 "
                        f"읽을 수 없거나 찾을 수 없다 ({template})")
                continue  # report-only 또는 dest 후보 자체 없음 — 완료 게이트 비차단.
            out.append((relpath, harness, mode, template))
    return out


def judge_adapter_configs(dest_root: Path, source_root: Path,
                          harnesses=None) -> list[adapter_baseline.AdapterConfigJudgment]:
    """instance-owned config 전수의 현재 판정 (읽기 전용·부작용 0).

    pm_update 의 동기 채널과 `sync-adapter-config --list` 가 같은 판정을 본다(판정 사본 0).

    세 축(dest·template·원장) 모두 `content_sha256` 으로 본다 — 개행 표기만 다른 파일은 같은
    내용이다(체크아웃 변환을 채택자 편집으로 오판하지 않는다). 원장 대조는 구 축(raw bytes)으로
    기록된 항목까지 `_baseline_attests_dest` 로 함께 인정한다(마이그레이션은 기록 쪽 담당)."""
    dest_root = Path(dest_root)
    recorded = adapter_baseline.read_adapter_baseline(dest_root).get("files", {})
    out: list[adapter_baseline.AdapterConfigJudgment] = []
    for relpath, harness, mode, template in adapter_config_targets(
            dest_root, source_root, harnesses):
        dest_path = dest_root / relpath
        dest_hash = adapter_baseline.content_sha256(dest_path)
        if dest_hash is None:
            if (mode == install_rules.ADAPTER_CONFIG_MANAGED
                    and adapter_baseline._adapter_config_path_exists(dest_path)):
                raise adapter_baseline.AdapterConfigChannelUnavailable(
                    f"managed 어댑터 config 비교 기준 unavailable: dest를 읽을 수 없다 "
                    f"({dest_path})")
            continue  # dest 에 없거나 못 읽음 — 이 채널의 관심사가 아니다.
        entry = recorded.get(relpath)
        baseline = entry.get("sha256") if isinstance(entry, dict) else None
        if adapter_baseline.content_sha256(template) == dest_hash:
            status = "in-sync"
        elif baseline is None:
            status = "unrecorded"
        elif adapter_baseline._baseline_attests_dest(baseline, dest_path, dest_hash):
            status = "unedited"
        else:
            status = "edited"
        out.append(adapter_baseline.AdapterConfigJudgment(
            relpath, harness, mode, status, template, dest_hash, baseline))
    return out


def adapter_config_convergence_status(judgment: adapter_baseline.AdapterConfigJudgment) -> str:
    """managed config 한 건의 완료 판정 — ``converged`` 또는 실행 가능한 red 상태.

    내용이 template 과 같다는 사실만으로는 미래 자동 갱신 궤도에 들어간 게 아니다. 원장에도
    **그 dest 해시**가 있어야 완료다. ``judge_adapter_configs`` 의 원시 판정은 표시/3-way 갱신에
    그대로 쓰고, 완료 게이트만 이 helper가 정규화한다(판정 사본 0).

    report-only 대상은 채택자 노브라 완료 게이트를 막지 않는다. 호출부는 필요하면 원시
    ``judgment.status`` 를 계속 출력한다.
    """
    if judgment.mode != install_rules.ADAPTER_CONFIG_MANAGED:
        return "report-only"
    if judgment.status != "in-sync":
        return judgment.status
    if (not judgment.dest_sha256
            or judgment.baseline_sha256 != judgment.dest_sha256):
        # 수동 byte 교체 또는 원장 기록 실패. 내용은 같아도 durable 증거가 없으므로 red다.
        return "unrecorded"
    return "converged"


def unconverged_managed_adapter_configs(
        judgments: list[adapter_baseline.AdapterConfigJudgment]) -> list[tuple[adapter_baseline.AdapterConfigJudgment, str]]:
    """완료 게이트를 막는 managed 판정만 ``(judgment, normalized_status)`` 로 반환."""
    out = []
    for judgment in judgments:
        status = adapter_config_convergence_status(judgment)
        if status not in ("converged", "report-only"):
            out.append((judgment, status))
    return out
