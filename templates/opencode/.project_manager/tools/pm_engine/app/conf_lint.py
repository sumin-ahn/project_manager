"""클론별 설정·위임 설정 lint (층: app).

local.conf 판독 가능·미등록 키·필수 키·주석 재진술, 어댑터 계층 drift, 위임 모델 중복을 판정한다.
"""
from __future__ import annotations


from pm_engine.domain import conf_schema, model_lint
from pm_engine.infra import conf_store, fs, pm_paths


# adapter-drift baseline 의 두 local.conf 키.
# 한 키가 baseline 과 현재-관찰을 겸하면 race/자기비교라 *분리*한다:
#   - upstream.rev      : baseline — 마지막 성공 sync 의 upstream revision (pm_import·pm_update 가 기록).
#   - upstream.seen_rev : 현재 관찰값 — 형상 무관하게 app.upstream_revisions.record_upstream_revs 단일 writer 가
#                         sync 시 baseline 과 *함께* 기록(동기 시점 소스 checkout rev = 관찰값 · URL 은
#                         그 실행이 clone/fetch 한 캐시 HEAD·경로는 그 checkout HEAD).
_DRIFT_BASELINE_KEY = "upstream.rev"
_DRIFT_SEEN_KEY = "upstream.seen_rev"


def lint_adapter_drift(home) -> list[tuple[str, str, str]]:
    """adapter-layer drift advisory (never-block).

    채택자의 **adapter-layer manifest-제외 파일**(facade·진입문서·settings)이 baseline(마지막 동기)
    *이후* upstream 에서 변경됐는지 가시화한다. 이 잔여는 전파 채널이 없어(B 전파=채택자
    customization clobber·비파괴 위배) 소리없이 stale 되므로, *전파 대신* PM 에게 경고만 낸다
    (kind=`adapter-drift`·`_ADVISORY_LINT_KINDS` 등재로 `--gate` 종료코드 비기여·visibility>enforcement).

    **drift 판정 = baseline B**(codex MUST-FIX 2): "공식판과 다름"(채택자 customization 오탐)이 아니라
    "마지막 동기 이후 upstream 변경". **lint 는 git network 를 하지 않는다**(codex round-2·3): `local.conf`
    의 **2개 키**만 비교한다 —

      - `upstream.rev`      (baseline·마지막 성공 sync·pm_import/pm_update 가 기록)
      - `upstream.seen_rev` (현재 관찰값·형상 무관·pm_update 가 sync 시 baseline 과 함께 기록하는
        동기 시점 소스 checkout rev)

    둘 다 존재하고 **다르면** drift 1 finding(두 rev 불일치 = adapter-layer 가 낡았을 수 있음). 한 키 2역
    금지(race/자기비교 회피·codex round-3 NEW-2). **메시지는 방향-중립**— lint 는 git 을 호출하지
    않으므로(rev 문자열 비교뿐) 두 rev 의 선후를 알 수 없다. 관찰값이 baseline 의 *조상*(구 흡수 잔재)인
    경우까지 "upstream 이 baseline 이후 변경됨"이라 단정하면 거짓 경보다(정상 흡수 직후 상시
    발화). 선후를 알려고 런타임 git ancestor 판정을 넣지 않고, 기록 시점 정합
    (app.upstream_revisions.record_upstream_revs 가 형상 무관하게 두 키 동시 기록)으로 원천 해소한다.

    scope: 대상 = manifest-제외 adapter-layer(settings.json·루트 doc·
    facade·진입문서·local.conf — adopter config·전파 채널 없음) / 제외 = instance-state(status·
    architecture·tickets·log·decisions·README 스캐폴드·lite — 채택자 소유·diverge 정상). **hooks·driver 는
    engine-mirror 전파 대상**(manifest 등록) — pm-update 가 동기하므로
    silent-stale 클래스가 "대상 단정 안 함". lint 가 파일 단위 diff 를
    하지 않으므로(rev 비교만) scope 는 advisory 메시지로 안내한다.

    관측 불가의 세 갈래 (never-block advisory):
      - `upstream` 미설정(non-adopter·templates/upstream 부재 환경) → [].
      - baseline(`upstream.rev`) 미기록(아직 revision 추적 전·구 import) → [](관찰 기준점 자체 부재).
      - baseline 은 있으나 seen(`upstream.seen_rev`) 미기록(cache 부재 URL·pm-update 미실행) → **관찰불가
        advisory 1줄**(never-block). 과거엔 조용한 [](silent skip)였으나, hooks/driver 등 safety-critical
        잔여가 *관찰 없이* 낡으면 "green 인데 고장"(checkpoint 가드 미발화·회귀 게이트 무력)이라 관찰불가 자체를
        표면화한다. advisory 라 `--gate` 미차단·1줄이라 flood 아님.
    """
    findings: list[tuple[str, str, str]] = []
    # conf 판독은 lint 조회면의 seam 하나(`_lint_local_conf_read`)로 한다 — 값 소비 계약
    # (`local_config` → `load_checked_readable`)으로 읽으면 판독 실패가 여기서 traceback 이 되어
    # lint 전체가 죽는다(그 상태를 말해 줄 `lint_local_conf_readable` 까지 함께 사라진다).
    # 파일 부재·판독 실패는 빈 목록이고, 못 읽은 사실은 그 한 자리가 1건으로 말한다.
    try:
        read = _lint_local_conf_read(home)
    except LocalConfUnreadable:
        return findings
    if read is None:
        return findings
    conf = read.values

    # non-adopter — upstream 자체가 없으면 비교할 대상이 없다 (graceful).
    if not (conf.get("upstream.path") or "").strip():
        return findings

    baseline = (conf.get(_DRIFT_BASELINE_KEY) or "").strip()
    seen = (conf.get(_DRIFT_SEEN_KEY) or "").strip()

    # baseline 미기록(구 import·revision 추적 전) → graceful [] (관찰 기준점 자체가 아직 없음).
    if not baseline:
        return findings

    # baseline 은 있으나 seen(현재 관찰값) 미기록 — drift 를 판정할 수 없다. 과거엔 조용한 []
    # (silent skip)였으나, hooks/driver·adapter-layer 잔여가 *관찰 없이* 낡으면 "green 인데 고장"
    # 이라 관찰불가 자체를 advisory 로 표면화한다(never-block·1줄이라 flood 아님).
    if not seen:
        findings.append((
            "adapter-layer", "adapter-drift",
            f"upstream.seen_rev 미기록 — upstream 관찰값이 없어 baseline({baseline[:12]}) 이후 "
            f"adapter-layer(settings·루트 doc·facade) drift 를 판정할 수 없음(관찰불가). "
            f"`pm-update`(upstream fetch)로 관찰값을 기록하면 추적된다 (never-block·hooks/driver 는 전파됨)"))
        return findings

    # 두 rev 가 같으면 baseline 이후 upstream 변경 없음 → clean.
    if baseline == seen:
        return findings

    # 다름 = 두 rev 불일치. 어느 쪽이 앞선지는 알 수 없으므로(git 미호출) **방향-중립**으로
    # 알리고, adapter-layer(facade·진입문서·settings)가 낡았을 가능성만 PM 에게 안내한다
    # (전파 아님·never-block). 실제 선후·변경분은 `pm-update --changes` 가 판정한다.
    findings.append((
        "adapter-layer", "adapter-drift",
        f"upstream baseline({baseline[:12]}) 과 관찰({seen[:12]}) 불일치 — "
        f"adapter-layer(facade·진입문서·settings) 가 낡았을 수 있음. "
        f"`pm-update` 로 동기 (instance-state·README·lite 는 채택자 소유·제외)"))
    return findings


# 실 `local.conf` 관측 kind — 채택자 conf 는 git-ignored 라 pytest 시야 밖이다. 엔진이 만드는
# 생성 문자열은 테스트가 정적으로 단언하고, 이미 디스크에 있는 파일은 이 조회면이 본다.
_LOCAL_CONF_LINT_KIND = "local-conf"


def lint_local_conf(home) -> list[tuple[str, str, str]]:
    """실 local.conf 의 구표기 잔존·값 재진술 주석을 advisory 로 표면화(never-block).

    두 관측 모두 **값을 소비하지 않는다** — 그래서 여기서는 fail-loud 하지 않는다. lint 가 멈추면
    무엇을 고쳐야 하는지 보여 줄 표면 자체가 사라지고, 채택자는 다른 명령의 traceback 만 보게 된다
    (값 소비 지점의 차단은 그 지점이 이미 한다).

    · 구표기 키: 공용 로더의 전수 매핑표가 판정과 문구를 소유한다(사본 금지).
    · 값 재진술 주석: 주석이 적어 둔 `키=값` 이 같은 파일의 실값과 다르면 그 줄은 이미 drift 했다.
      손으로 옮겨 적은 값은 반드시 이렇게 되므로 발견 즉시 지우는 것이 처방이다.
    """
    if not home.local_conf.is_file():
        return []
    try:
        result = conf_store.load(home.local_conf)
        text = fs.read_text_shared(home.local_conf, encoding="utf-8")
    except Exception:  # noqa: BLE001 — 조회면은 판독 실패로 멈추지 않는다.
        return []
    where = pm_paths._rel_to_repo(home, home.local_conf)
    findings = [
        (where, _LOCAL_CONF_LINT_KIND, line.strip())
        for line in conf_schema.migration_lines(result.legacy)
    ]
    findings.extend(
        (where, _LOCAL_CONF_LINT_KIND,
         f"주석이 실값과 다른 값을 재진술한다: `{key}={stated}` (실값 `{result.values[key]}`)")
        for key, stated in _restated_conf_comments(text)
        if key in result.values and result.values[key] != stated
    )
    return findings


def _restated_conf_comments(text: str) -> list[tuple[str, str]]:
    """conf 주석 줄이 적어 둔 `키=값` 쌍 — 실값 대조는 호출부가 한다."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        if "=" not in body:
            continue
        key, _, value = body.partition("=")
        stated = value.strip().split()[0] if value.strip() else ""
        pairs.append((key.strip(), stated))
    return pairs


class LocalConfUnreadable(RuntimeError):
    """실 local.conf 가 **있는데 읽지 못했다**(인코딩·권한) — "값이 없다" 와 다른 사실이다."""


def _lint_local_conf_read(home):
    """lint 조회면이 쓰는 실 local.conf 판독 — 세 상태를 구분한다.

    · 파일 없음 → `None`(관측 없음 · conf 없는 트리는 정상 형상).
    · 판독 실패(`OSError`·`UnicodeError`) → `LocalConfUnreadable` 로 **올린다**. 판독은
      `conf_store.load_strict` 로 한다 — 삼키는 `load` 를 쓰면 잘못된 UTF-8 이 "키 0개" 가 돼
      관측 kind 전부가 조용히 0건이 된다(못 읽은 것이 값이 없는 것으로 위장).
    · 성공 → 판독 결과(`ConfResult`).

    아래 관측 kind 들이 이 **한 경계**를 공유한다 — 같은 규칙을 함수마다 복제하면 규칙이 갈린다.
    발견을 내는 자리는 `lint_local_conf_readable()` 하나이고 나머지 소비자는 빈 목록이다(같은
    사실을 여러 번 말하지 않는다).
    """
    if not home.local_conf.is_file():
        return None
    try:
        return conf_store.load_strict(home.local_conf)
    except (OSError, UnicodeError) as exc:
        raise LocalConfUnreadable(f"{home.local_conf}: {exc}") from exc


# 실 local.conf 를 **읽지 못한** 관측 kind — 위 판독 seam 하나가 낸다(값 축과 배타적).
_LOCAL_CONF_UNREADABLE_LINT_KIND = "local-conf-unreadable"


def lint_local_conf_readable(home) -> list[tuple[str, str, str]]:
    """실 local.conf 를 읽지 못한 사실을 advisory 로 표면화(never-block · 발견 1건).

    파일이 없으면 발견 0건이다(관측 대상 자체가 없다 — 채택자 대부분의 정상 형상). 있는데 못
    읽었을 때만 뜬다: 그 상태에서 다른 conf 관측 kind 는 전부 0건이라, 이 줄이 없으면 화면은
    "설정에 문제 없음" 과 구별되지 않는다.
    """
    try:
        _lint_local_conf_read(home)
    except LocalConfUnreadable as exc:
        return [(pm_paths._rel_to_repo(home, home.local_conf), _LOCAL_CONF_UNREADABLE_LINT_KIND,
                 f"local.conf 를 읽지 못했다: {exc} — 값이 없는 것이 아니라 읽지 못한 것이다"
                 "(파일 인코딩/권한 확인)")]
    return []


# 실 local.conf 의 **레지스트리 밖 키** 관측 kind — `local-conf`(구표기 잔존)와 배타적 축이다.
_LOCAL_CONF_UNKNOWN_LINT_KIND = "local-conf-unknown-key"

# 키 목록·설명·기본값의 정본은 레지스트리(`conf_schema.KEY_REGISTRY`)이고, 사람은 이 명령으로
# 본다 — 안내 두 줄이 같은 문자열을 쓴다(문서 절 이름을 가리키던 옛 안내는 사라졌다).
_LOCAL_CONF_KEYS_CMD = "python3 .project_manager/tools/local_conf.py --keys"


def lint_local_conf_keys(home) -> list[tuple[str, str, str]]:
    """실 local.conf 의 **레지스트리 밖 키**(오타·폐기·손열거 밖 패턴)를 advisory 로 표면화(never-block).

    known-key 판정은 공용 로더(`local_conf.py`)가 소유한다(사본 금지·`ConfResult.unknown`) —
    이 함수는 그 결과를 lint finding 으로 감쌀 뿐이다. 구표기 잔존은 `lint_local_conf()` 가 이미
    별도 kind(`local-conf`)로 낸다 — `unknown` 은 구표기 키를 걸러내므로(`is_legacy_key`) 두
    kind 는 겹치지 않는다.

    키별 특례는 두지 않는다 — 폐기 키(`review_ticket_body_max_bytes` 등)도 오타 키도 이 규칙
    하나로 걸린다. 여러 키가 한꺼번에 있어도 finding 은 1개(1줄) — `cmd_init` 병합 경로가 같은
    목록을 같은 형태로 재사용한다(사본 없이 같은 함수 재호출).
    """
    try:
        read = _lint_local_conf_read(home)
    except LocalConfUnreadable:
        return []          # 못 읽은 사실은 `lint_local_conf_readable` 이 한 번 말한다.
    if read is None:
        return []
    unknown = read.unknown
    if not unknown:
        return []
    where = pm_paths._rel_to_repo(home, home.local_conf)
    keys = ", ".join(f"`{key}`" for key in unknown)
    return [(where, _LOCAL_CONF_UNKNOWN_LINT_KIND,
             f"엔진이 모르는 local.conf 키: {keys} (오타 또는 폐기 키 — "
             f"`{_LOCAL_CONF_KEYS_CMD}` 로 이름을 대조하라)")]


# 실 local.conf 의 **필수 키 누락** 관측 kind — 위 두 kind(구표기·모르는 키)와 배타적 축이다.
_LOCAL_CONF_MISSING_LINT_KIND = "local-conf-missing-required"


def lint_local_conf_required(home) -> list[tuple[str, str, str]]:
    """실 local.conf 에 **필수 키가 선언되지 않은** 것을 advisory 로 표면화(never-block).

    필수 판정은 공용 로더가 소유한다(`local_conf.missing_required` · 사본 금지). 필수는 핵심
    명령(회귀·init)이 못 도는 두 키뿐이고 `board.py init` 이 둘 다 심으므로, 이 줄이 뜨는 형상은
    init 을 거치지 않고 손으로 만든 conf 다. 값이 빈 문자열이면 "있음" 으로 본다 — 선언 유무만
    보는 축이고 값의 내용 판정은 그 값을 소비하는 지점이 한다.
    """
    try:
        read = _lint_local_conf_read(home)
    except LocalConfUnreadable:
        return []          # 못 읽은 사실은 `lint_local_conf_readable` 이 한 번 말한다.
    if read is None:
        return []
    missing = conf_schema.missing_required(read.values)
    if not missing:
        return []
    keys = ", ".join(f"`{key}`" for key in missing)
    return [(pm_paths._rel_to_repo(home, home.local_conf), _LOCAL_CONF_MISSING_LINT_KIND,
             f"local.conf 필수 키 누락: {keys} (`board.py init` 이 채운다 — 설명·기본값은 "
             f"`{_LOCAL_CONF_KEYS_CMD}`)")]


def lint_delegate(home) -> list[tuple[str, str, str]]:
    """delegate 동일-모델 dev/reviewer 경고를 board lint finding 으로 표면화 (advisory·never-block).

    model_lint.lint_same_model(conf) 의 `(label, detail)` 를 board 관례 `(label, kind, detail)` 로
    감싼다. kind=`delegate-same-model`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드에 *절대*
    기여하지 않는다·visibility>enforcement — 사용자가 동일-모델 조합을 선택할 자유 유지). 설정
    읽기 실패·설정 미매핑 → [] (delegate 미사용 프로젝트 무영향). `home` 의
    local.conf(local_config)로 판정한다."""
    try:
        findings = model_lint.lint_same_model(conf_store.local_config(home))
    except Exception:  # noqa: BLE001 — 설정 읽기 실패는 빈 결과(advisory).
        return []
    return [(label, "delegate-same-model", detail) for label, detail in findings]
