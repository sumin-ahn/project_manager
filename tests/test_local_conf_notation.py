"""local.conf 표기 통일 + 공용 로더 (T-0767).

`local.conf` 의 키 표기가 세 갈래(flat snake_case · dot notation · suffix per-harness)로 섞여 있었고
파싱이 9구현으로 갈려 같은 텍스트가 모듈마다 다른 dict 로 해소됐다(중복 키를 어떤 구현은 first-wins,
어떤 구현은 last-wins). 표기를 dot notation 하나로 통일하고 파싱을 `local_conf.py` 하나로 합친 절의
회귀다. 이 파일이 닫는 축:

1. 파싱 의미 — 한 표(conf 텍스트 → 기대 dict)로 계약을 고정하고, 두 정책(fail-soft 판독 / 판독 실패
   전파)이 호출부별로 갈리는 것까지 단언한다.
2. 구표기 fail-loud — 값을 소비하는 지점은 멈추고, `pm_update` 의 apply 는 통과해 **파일을 실제로
   배달**한다(그러지 않으면 채택자가 안내대로 고칠 수단 없이 갇힌다).
3. 전수 매핑표 ↔ 코드 양방향 — 표의 구키가 코드에 남아 있으면 red, 코드가 읽는 신키가 레지스트리에
   없으면 red.
4. 불변식 1·2·3 — flat 키 0 · per-harness 단일 문법 · 값 공급 경로 유일.
5. 교체 안내 — 키 단위 지목 + 모델 자동 이관 없음 + 기본값 변경 1줄(형상 무관) + 채택자 소유 파일 지목.
6. 위임 마스터 스위치 — off 의 차단 3층과 면제(harvest·copies·--dry-run).
7. conf 생성 문자열 — 주석이 설정값을 재진술하지 않는다(손으로 옮겨 적은 값은 반드시 drift 한다).

hermetic: 실 `local.conf` **파일**을 tmp 에 만들어 각 도구가 그 파일을 읽어 해소한 값을 단언한다
(조립한 dict·monkeypatch 상수를 단언하면 파일→값 경로가 통째로 빠진다). 외부 프로세스 스폰 0.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, alias: str | None = None):
    spec = importlib.util.spec_from_file_location(alias or name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def conf_module():
    return _load("local_conf")


def _write_conf(root: Path, text: str) -> Path:
    """tmp 트리에 실 `local.conf` 파일을 만든다 — 값 해소는 항상 이 파일에서 출발한다."""
    path = root / ".project_manager" / "local.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── ① 파싱 의미 (표 하나가 계약) ──────────────────────────────────────────

_PARSE_TABLE = (
    pytest.param("runtime.py=python3\n", {"runtime.py": "python3"}, id="plain"),
    pytest.param("  test.cmd = pytest -q  \n", {"test.cmd": "pytest -q"}, id="strip"),
    pytest.param("# 주석\n\ntest.cmd=pytest -q\n", {"test.cmd": "pytest -q"},
                 id="comment-and-blank-ignored"),
    pytest.param("그냥 문장\ntest.cmd=pytest -q\n", {"test.cmd": "pytest -q"},
                 id="line-without-equals-ignored"),
    pytest.param("test.cmd=pytest -q # 빠른\n", {"test.cmd": "pytest -q # 빠른"},
                 id="hash-inside-value-preserved"),
    pytest.param("identity.user=a\nidentity.user=b\n", {"identity.user": "b"},
                 id="duplicate-last-wins"),
    pytest.param("identity.user=a\nidentity.user=\n", {"identity.user": ""},
                 id="trailing-empty-unsets"),
    pytest.param("test.cmd=a=b\n", {"test.cmd": "a=b"}, id="value-may-contain-equals"),
    pytest.param("", {}, id="empty-file"),
)


@pytest.mark.parametrize("text,expected", _PARSE_TABLE)
def test_parsing_semantics_are_one_table(conf_module, tmp_path, text, expected):
    """파싱 의미는 이 표 하나다 — 소비 모듈이 늘어도 같은 텍스트는 같은 dict 로 해소된다."""
    path = _write_conf(tmp_path, text)
    assert conf_module.load(path).values == expected
    assert conf_module.parse(text) == expected


def test_missing_conf_is_an_empty_result_not_an_error(conf_module, tmp_path):
    """conf 부재는 정상 형상이다 — 새 clone 이 첫 명령에서 터지지 않는다."""
    assert conf_module.load(tmp_path / "없는" / "local.conf").values == {}


def test_unreadable_conf_splits_by_caller_policy(conf_module, tmp_path):
    """판독 실패의 처리는 **호출부 정책**이다 — 삼키는 진입과 올리는 진입이 따로 있다.

    한 함수로 합치면 "conf 를 못 읽었다" 가 "conf 가 비었다" 로 접히는데, 외부 송신 전 보호 선언을
    확인하는 자리에서 그 접힘은 미확인 송신이 된다.
    """
    path = _write_conf(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x00")            # UTF-8 로 못 읽는 바이트

    assert conf_module.load(path).values == {}            # fail-soft 진입
    assert conf_module.load_checked(path) == {}
    with pytest.raises(UnicodeError):                      # 전파 진입
        conf_module.load_strict(path)
    with pytest.raises(UnicodeError):
        conf_module.load_checked_readable(path)


def test_duplicate_key_resolves_the_same_in_every_consuming_module(tmp_path):
    """중복 키 형상에서 **모든 소비 모듈이 같은 값**을 본다(옛 first-wins 3구현 정정).

    같은 파일을 `pm-config`·board·리뷰 축이 다르게 읽으면 한 도구가 허용한 설정을 다른 도구가
    거부한다 — 그 비대칭이 표기 통일 전의 실제 결함이었다.
    """
    _write_conf(tmp_path, "identity.user=first@example.com\n"
                          "identity.user=last@example.com\n")
    board = _load("board", "notation_board")
    external = _load("external_review", "notation_external")
    config = _load("pm_config", "notation_config")
    board.LOCAL_CONF = tmp_path / ".project_manager" / "local.conf"
    board.REPO = tmp_path
    external.LOCAL_CONF = board.LOCAL_CONF
    external.REPO = tmp_path
    config.REPO = tmp_path

    assert board.local_config()["identity.user"] == "last@example.com"
    assert external.local_config()["identity.user"] == "last@example.com"
    assert config._local_conf_value("identity.user") == "last@example.com"


def test_module_level_local_config_seams_are_still_patchable(monkeypatch):
    """모듈 레벨 `local_config` 이름은 시그니처째 보존된다 — 63지점 테스트 seam."""
    for name in ("board", "external_review", "ticket_finish", "pm_delegate"):
        module = _load(name, f"seam_{name}")
        assert callable(module.local_config)
        monkeypatch.setattr(module, "local_config", lambda *a, **k: {"test.cmd": "x"})
        assert module.local_config()["test.cmd"] == "x"


# ── ② 구표기 fail-loud (소비는 멈추고 배달은 계속) ─────────────────────────

_LEGACY_CONF = (
    "additional_reviewer_enabled=true\n"
    "delegate_enabled=false\n"
    "regression_min_collected=40\n"
    "ctx_window_tokens_claude=200000\n"
)


@pytest.mark.parametrize("name", ("board", "external_review", "ticket_finish",
                                  "pm_delegate"))
def test_legacy_keys_stop_the_consumer_instead_of_silently_defaulting(tmp_path, name):
    """구표기 conf 를 **읽는 지점**이 멈춘다 — 조용히 엔진 기본값으로 떨어지지 않는다.

    `conf.get(key, default)` 로 읽던 값들이라 개칭 즉시 가드가 약해지는 방향으로 강등된다
    (`regression.min_collected` 는 0 수집 false-green 차단, `delegate.enabled` 는 기본이
    permissive 로 뒤집힌다).
    """
    _write_conf(tmp_path, _LEGACY_CONF)
    module = _load(name, f"legacy_{name}")
    module.REPO = tmp_path
    if hasattr(module, "LOCAL_CONF"):
        module.LOCAL_CONF = tmp_path / ".project_manager" / "local.conf"
    # 예외 **종류**는 이름으로 본다 — 도구마다 로더 사본이 따로 로드돼 클래스 identity 가 다르고,
    # 계약은 "구표기 잔존이 이 이름의 예외로 멈춘다" 이지 특정 객체가 아니다.
    with pytest.raises(RuntimeError) as caught:
        module.local_config()

    assert type(caught.value).__name__ == "LegacyConfKeyError"
    message = str(caught.value)
    for key in ("additional_reviewer_enabled", "delegate_enabled",
                "regression_min_collected", "ctx_window_tokens_claude"):
        assert key in message, key
    assert "additional_reviewer.enabled" in message
    assert "harness.claude.ctx_window_tokens" in message


def test_non_blocking_legacy_keys_are_named_but_do_not_block(conf_module, tmp_path):
    """읽는 코드가 이미 0인 구키는 안내만 받는다 — 무해한 한 줄이 전 명령을 세우지 않는다."""
    path = _write_conf(tmp_path, "session=pm\nprefix=T\ntest.cmd=pytest -q\n")
    result = conf_module.load(path)

    assert set(result.legacy) == {"session", "prefix"}
    assert conf_module.blocking_legacy(result.legacy) == {}
    conf_module.assert_no_legacy(result)                      # raise 하지 않는다
    assert conf_module.load_checked_readable(path)["test.cmd"] == "pytest -q"


def test_pm_update_apply_delivers_engine_files_on_a_legacy_conf(tmp_path, capsys):
    """`pm_update` apply 는 구표기 conf 에서도 통과하고 **파일을 실제로 배달**한다.

    막으면 채택자가 새 엔진 없이 안내대로 고칠 수단도 잃는다(§교체 절차 1). 그래서 배달은 계속하고
    값을 소비하는 지점에서만 멈춘다.
    """
    update = _load("pm_update", "apply_pm_update")
    dest = tmp_path / "adopter"
    (dest / ".project_manager" / "tools").mkdir(parents=True)
    _write_conf(dest, _LEGACY_CONF)
    stale = dest / ".project_manager" / "tools" / "local_conf.py"
    stale.write_text("# 구세대 사본\n", encoding="utf-8")

    # change tuple = (relpath, source, dest, kind) — planning 산출과 같은 모양이다.
    update.apply([(".project_manager/tools/local_conf.py",
                   TOOLS / "local_conf.py", stale, "update")])

    assert stale.read_bytes() == (TOOLS / "local_conf.py").read_bytes()
    update.print_conf_migration_notice(dest)
    out = capsys.readouterr().out
    assert "additional_reviewer_enabled" in out and "delegate_enabled" in out


@pytest.mark.parametrize("name", ("pm_handoff", "ticket_finish"))
def test_gate_resolution_stops_instead_of_falling_back_to_the_default_suite(tmp_path, name):
    """게이트 해소가 구표기 conf 를 만나면 **멈춘다** — 기본 pytest 게이트로 강등되지 않는다.

    해소 체인은 실패를 삼켜 솔로 폴백(None)으로 흘리도록 설계돼 있다. 그 삼킴이 구표기 판정까지
    먹으면 채택자가 conf 에 적어 둔 게이트가 사라진 채 `pytest tests/ -q` 가 대신 돈다 — 중앙
    로더를 fail-loud 로 만든 이유가 그 자리에서 무효가 된다.
    """
    _write_conf(tmp_path, _LEGACY_CONF + "test_cmd=false\n")
    board = _load("board", f"gate_board_{name}")
    board.REPO = tmp_path
    board.LOCAL_CONF = tmp_path / ".project_manager" / "local.conf"
    handoff = _load("pm_handoff", f"gate_handoff_{name}")

    # 실 파일을 읽는 board 모듈로 두 도구의 동형 경로를 돌린다(주입 dict 아님).
    with pytest.raises(RuntimeError) as caught:
        if name == "pm_handoff":
            handoff._resolve_gate_cmd(board)
        else:
            finish = _load("ticket_finish", "gate_ticket_finish")
            finish._load_board_module = lambda: board
            finish._load_pm_handoff = lambda: handoff
            finish._resolve_per_repo_test_cmd()

    assert type(caught.value).__name__ == "LegacyConfKeyError"
    assert "test_cmd" in str(caught.value)
    # 폴백 라벨이 대신 나가지 않았다 — 멈춘 자리에서 무엇을 고칠지 말한다.
    assert handoff._gate_label(None) not in str(caught.value)


def test_the_exception_text_points_at_adopter_owned_files(tmp_path):
    """소비자가 던지는 예외 **전문**이 엔진이 못 고치는 파일까지 지목한다.

    도구가 여기서 멈추면 이 예외가 채택자가 보는 유일한 안내다 — 교체 안내에만 붙어 있으면
    `pm-update` 를 다시 돌리기 전까지 그 파일이 stale 이라는 사실을 어디서도 듣지 못한다.
    """
    conf_module = _load("local_conf", "pointer_local_conf")
    _write_conf(tmp_path, _LEGACY_CONF)
    board = _load("board", "pointer_board")
    board.REPO = tmp_path
    board.LOCAL_CONF = tmp_path / ".project_manager" / "local.conf"

    with pytest.raises(RuntimeError) as caught:
        board.local_config()

    message = str(caught.value)
    for line in conf_module.adopter_pointer_lines():
        assert line in message, line
    assert ".codex/config.toml" in message


def test_the_round_limit_axis_is_named_as_removed_not_renamed(conf_module):
    """라운드 상한 구키 두 이름은 '대체 없이 제거'다 — 이 티켓이 신키를 만들지 않는다."""
    retired = ("additional_reviewer_round_limit", "external_review_round_limit")
    for key in retired:
        assert conf_module.LEGACY_KEY_MAP[key] is None
        assert f"`{key}` → 제거(대체 키 없음)" in "\n".join(
            conf_module.migration_lines({key: None}))
    assert [key for key in conf_module.KNOWN_KEYS if key.endswith(".round_limit")] == []
    external = _load("external_review", "round_limit_external")
    assert not hasattr(external, "_round_limit")
    assert ".round_limit" not in (REPO / "README.md").read_text(encoding="utf-8")


# ── ③ 전수 매핑표 ↔ 코드 (양방향) ─────────────────────────────────────────


def _engine_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8")
            for path in sorted(TOOLS.glob("*.py"))}


def test_no_legacy_key_literal_survives_in_the_engine(conf_module):
    """표에 있는 구키가 **conf 키 리터럴로** 코드에 남아 있으면 red (표 → 코드 방향).

    판정 대상은 **conf 조회 자리**(`conf.get("키")`·`conf["키"]`)다. 매핑표 자신과 산문(안내
    문구)은 그 키를 이름으로 말해야 하므로 제외한다.
    """
    replaced = [key for key, new in conf_module.LEGACY_KEY_MAP.items() if new is not None]
    # conf **조회**로 쓰인 자리만 본다 — 같은 낱말이 areas.md 칼럼·kwargs·git config 이름으로도
    # 쓰이므로 문자열 존재만 보면 그 자리들까지 red 가 된다(판정이 아니라 소음이 된다).
    lookups = re.compile(
        r"(?:conf|config|values|conf_values)\s*(?:\.get\(\s*|\[\s*)"
        r"[\"'](" + "|".join(re.escape(key) for key in replaced) + r")[\"']")
    offenders: list[str] = []
    for path, source in _engine_sources().items():
        if path.name == "local_conf.py":
            continue                                  # 매핑표 자신이 이 키들을 소유한다
        offenders.extend(f"{path.name}: {key}" for key in lookups.findall(source))
    assert offenders == [], offenders


def test_every_conf_key_the_engine_reads_is_registered(conf_module):
    """코드가 읽는 dot 표기 conf 키가 레지스트리/패턴 밖이면 red (코드 → 표 방향)."""
    known = set(conf_module.KNOWN_KEYS)
    pattern = re.compile(r'conf(?:ig)?\.get\(\s*["\']([a-z_]+\.[a-z_.]+)["\']')
    unknown: list[str] = []
    for path, source in _engine_sources().items():
        for key in pattern.findall(source):
            if key in known or conf_module.unknown_keys({key: ""}) == ():
                continue
            unknown.append(f"{path.name}: {key}")
    assert unknown == [], unknown


# ── ③-B 출하 표면 전수 (런타임 문자열·문서·facade·어댑터) ────────────────
#
# 조회 자리만 보는 가드는 **가르치는 문구**를 못 본다 — 오류·`--help`·출하 문서·facade 가 구표기를
# 처방하면 채택자는 그대로 적고, 그 conf 는 소비 지점에서 멈춘다(엔진은 맞는데 안내가 틀린 상태).
# 그래서 판정 표면을 Python 밖(shell·cmd·JS·문서·templates)까지 넓히고, **가드 시야가 그 표면과
# 같다는 것**을 독립 열거로 대조한다(존재가 아니라 파일 수·차집합 값).
#
# 문맥 규칙 — "표기" 로 세는 형태만 본다:
#   (a) `local.conf` 바로 뒤(조사·따옴표·백틱 허용)의 `키=` 대입 또는 백틱 키
#   (b) 대체 키가 있는 구키만 — 제거된 키(`session`·`prefix`·`reviewer_cmd`)는 산문이 이름으로
#       말해야 한다("이 키는 제거됐다"). 이름을 금지하면 제거 안내 자체를 못 쓴다.

_NOTATION_SURFACE_ROOTS = (".claude", "docs", "templates",
                           ".project_manager/tools", ".project_manager/wiki")
_NOTATION_SURFACE_EXTRA = ("README.md", "CHANGELOG.md")
_NOTATION_TEXT_SUFFIXES = frozenset({".py", ".md", ".sh", ".cmd", ".cjs", ".js",
                                     ".toml", ".jsonc"})
_NOTATION_SKIP_PARTS = frozenset({"__pycache__", ".local", ".git"})
# 구키를 **의도적으로 지목**하는 자리 — 그 밖은 신표기여야 한다.
_NOTATION_ALLOWLIST = (
    ".project_manager/tools/local_conf.py",   # 전수 매핑표·안내 문구의 소유자(사본 포함)
    "CHANGELOG.md",                           # 릴리즈 이력 — 그 시점의 이름을 보존한다
)
# 가드 시야 밖(대조에서 제외) — 엔진이 생성하는 wiki 산출물(추적 대상 아님).
_NOTATION_GENERATED = (".project_manager/wiki/board.md",
                       ".project_manager/wiki/log/dashboard.md")


def _notation_surface_files() -> list[str]:
    """가드가 여는 파일 — repo-owned 열거 seam 하나로 정의한다.

    직접 `rglob` 하면 이 저장소의 열거 규칙(추적/ignore 보장)을 우회하는 두 번째 정의가 생겨
    가드 시야와 실 출하 표면이 갈린다. 열거는 seam 이 소유하고 여기서는 확장자·경로만 거른다.
    """
    found: set[str] = set()
    for root in _NOTATION_SURFACE_ROOTS:
        for path in repo_owned_paths(REPO, root, mode=OWNED):
            if not path.is_file() or path.suffix not in _NOTATION_TEXT_SUFFIXES:
                continue
            if _NOTATION_SKIP_PARTS & set(path.parts):
                continue
            found.add(path.relative_to(REPO).as_posix())
    for extra in _NOTATION_SURFACE_EXTRA:
        if (REPO / extra).is_file():
            found.add(extra)
    return sorted(found - set(_NOTATION_GENERATED))


def _notation_pattern(conf_module) -> re.Pattern:
    """`local.conf` 문맥 안의 **구표기 키 표기**(대입·백틱)만 잡는 판정 정규식."""
    renamed = sorted((key for key, new in conf_module.LEGACY_KEY_MAP.items()
                      if new is not None), key=len, reverse=True)
    return re.compile(
        r"local[._]conf`?(?:\s*의)?[\s`\"'(]{1,4}("
        + "|".join(re.escape(key) for key in renamed) + r")(?![.\w])")


def test_no_shipped_surface_teaches_a_retired_key_notation(conf_module):
    """출하 표면 전수 — 런타임 문자열·문서·facade·어댑터가 구표기를 가르치지 않는다."""
    pattern = _notation_pattern(conf_module)
    offenders: list[str] = []
    for relpath in _notation_surface_files():
        if any(relpath.endswith(allowed) for allowed in _NOTATION_ALLOWLIST):
            continue
        text = (REPO / relpath).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            offenders.extend(f"{relpath}:{number} {key}" for key in pattern.findall(line))
    assert offenders == [], offenders


def test_the_notation_guard_field_of_view_matches_the_shipped_surface():
    """가드 시야 == 출하 표면 — **독립 열거**(git 색인)와 파일 단위로 대조한다.

    시야가 표면보다 좁으면 "구표기 0" 주장이 값으로 잠기지 않는다(가드가 안 보는 자리에 남는다).
    그래서 존재가 아니라 **차집합과 계층별 수**를 단언한다.
    """
    scanned = set(_notation_surface_files())
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", check=True).stdout.split())
    expected = {
        relpath for relpath in tracked
        if Path(relpath).suffix in _NOTATION_TEXT_SUFFIXES
        and (relpath in _NOTATION_SURFACE_EXTRA
             or relpath.startswith(tuple(f"{root}/" for root in _NOTATION_SURFACE_ROOTS)))
    }

    assert scanned == expected, {"미시야": expected - scanned, "색인밖": scanned - expected}
    # 계층별 수 — 어느 계층도 통째로 빠지지 않았다(0 이면 그 계층을 안 보는 것이다).
    layers = {
        "facade-shell": len([f for f in scanned if f.endswith((".sh", ".cmd"))]),
        "adapter-js": len([f for f in scanned if f.endswith(".cjs")]),
        "docs": len([f for f in scanned if f.startswith("docs/")]),
        "templates": len([f for f in scanned if f.startswith("templates/")]),
        "engine": len([f for f in scanned
                       if f.startswith(".project_manager/tools/")]),
    }
    assert all(count > 0 for count in layers.values()), layers
    # 표면이 실제로 담고 있는 파일들 — 이름으로 못박는다(리뷰 지적의 실제 자리).
    for relpath in ("templates/claude_code/pm-update.sh",
                    "templates/codex/pm-update.cmd",
                    "templates/opencode/.opencode/lib/ctx-guard-core.cjs",
                    "docs/portability.md", "docs/manual-import.md",
                    ".project_manager/tools/board.py",
                    ".project_manager/tools/pm_update.py"):
        assert relpath in scanned, relpath


# ── ④ 불변식 1·2·3 ────────────────────────────────────────────────────────


def test_invariant_one_notation_is_dot_only(conf_module):
    """불변식 1 — 신키 레지스트리에 flat snake_case 키가 0 이다(축 없는 값도 축을 만든다)."""
    flat = [key for key in conf_module.KNOWN_KEYS if "." not in key]
    assert flat == []


def test_invariant_two_per_harness_has_a_single_syntax(conf_module):
    """불변식 2 — per-harness 지정 문법은 `harness.<name>.<속성>` 하나다."""
    relay = _load("pm_relay", "notation_relay")
    for harness in relay.HARNESS_CHOICES:
        assert conf_module.unknown_keys(
            {f"harness.{harness}.ctx_window_tokens": "1"}) == ()
        # suffix 표기는 신키가 아니라 **구표기**로 판정된다 — 두 문법 공존이 없다.
        assert conf_module.is_legacy_key(f"ctx_window_tokens_{harness}") is True
        assert conf_module.legacy_replacement(f"ctx_window_tokens_{harness}") == \
            f"harness.{harness}.ctx_window_tokens"


def test_invariant_three_one_supply_path_per_value(conf_module):
    """불변식 3 — 신·구 두 이름이 같은 값을 공급하는 상태가 없다(구키는 레지스트리 밖)."""
    known = set(conf_module.KNOWN_KEYS)
    assert [key for key in conf_module.LEGACY_KEY_MAP if key in known] == []


def test_adapter_parsers_read_the_new_per_harness_key(conf_module):
    """엔진을 import 하지 않는 어댑터 4파서도 같은 신표기를 읽는다(문법이 갈리면 값이 갈린다)."""
    adapters = (
        REPO / "templates/claude_code/.claude/ctx_guard.py",
        REPO / "templates/codex/.codex/pm_orch_codex.py",
        REPO / "templates/opencode/.opencode/pm_orch_opencode.py",
        REPO / "templates/opencode/.opencode/lib/ctx-guard-core.cjs",
    )
    relay = _load("pm_relay", "adapter_notation_relay")
    for path in adapters:
        text = path.read_text(encoding="utf-8")
        # suffix 표기는 **차단 대상 이름**으로만 등장한다(조회 자리 0) — 그 선언은 엔진 생성분이다.
        for harness in relay.HARNESS_CHOICES:
            assert f"ctx_window_tokens_{harness}" not in text, path
        assert conf_module.LEGACY_SUFFIX_PREFIX in text, path      # 차단 접두는 있어야 한다
        assert "ctx_window_tokens" in text, path
        assert "harness." in text, path


# ── ⑤ 교체 안내 (키 단위 지목 · 자동 이관 없음) ────────────────────────────


def test_migration_notice_names_every_key_and_refuses_auto_migration(
        conf_module, tmp_path):
    """안내는 키 단위로 지목하고, 모델 값은 옮기지 않는다고 못 박는다(전수 매핑 기반)."""
    path = _write_conf(tmp_path, _LEGACY_CONF + "reviewer_cmd=codex exec\n")
    lines = conf_module.migration_notice(conf_module.load(path))
    text = "\n".join(lines)

    assert conf_module.DELEGATE_DEFAULT_CHANGE_NOTICE in lines[0]
    for key in ("additional_reviewer_enabled", "delegate_enabled",
                "regression_min_collected", "ctx_window_tokens_claude"):
        assert f"`{key}`" in text, key
    assert "`reviewer_cmd` → 제거(대체 키 없음)" in text
    assert "모델 값은 자동으로 옮기지 않습니다" in text
    assert str(path) in text                                   # 어느 파일인지
    assert ".codex/config.toml" in text                        # 채택자 소유 파일도 지목


def test_default_change_notice_is_unconditional(conf_module, tmp_path):
    """구키가 아예 없는 채택자에게도 기본값 변경 1줄은 나간다 — fail-loud 가 못 잡는 형상이다."""
    path = _write_conf(tmp_path, "test.cmd=pytest -q\n")
    lines = conf_module.migration_notice(conf_module.load(path))

    assert lines == [conf_module.DELEGATE_DEFAULT_CHANGE_NOTICE]
    assert "delegate.enabled=false" in lines[0]                # 끄는 법을 함께 말한다


def test_import_into_an_existing_tree_delivers_engine_then_refuses_init(
        tmp_path, capsys, monkeypatch):
    """`pm_import --into` 는 엔진을 배달한 뒤 board init 을 **거부**하고 안내한다.

    구표기 conf 로 init 을 돌리면 그 안에서 conf 를 소비해 traceback 이 나고, 채택자는 무엇을
    고쳐야 하는지 대신 스택을 본다. 엔진 파일은 이미 새것이므로 키만 바꾸고 재실행하면 된다.
    """
    imp = _load("pm_import", "notice_pm_import")
    dest = tmp_path / "adopter"
    (dest / ".project_manager").mkdir(parents=True)
    _write_conf(dest, _LEGACY_CONF)

    assert imp._local_conf_has_blocking_legacy(dest) is True
    imp.print_conf_migration_notice(dest)
    out = capsys.readouterr().out
    assert "additional_reviewer_enabled" in out
    assert "additional_reviewer.enabled" in out


# ── ⑥ 위임 마스터 스위치 (차단 3층 · 면제) ─────────────────────────────────


def _delegate_repo(tmp_path: Path, switch: str | None) -> Path:
    """위임 CLI 가 요구하는 git 작업공간 + 매핑이 갖춰진 conf (스위치만 형상별로 바뀐다)."""
    lines = ["delegate.developer.harness=codex\n", "delegate.developer.model=gpt-x\n"]
    if switch is not None:
        lines.insert(0, f"delegate.enabled={switch}\n")
    _write_conf(tmp_path, "".join(lines))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_ticket_prepare_is_refused_when_delegation_is_off(tmp_path, capsys):
    """스위치 off 는 `ticket prepare` 도 rc=3 — run-dir·라운드 순번을 만들지 않는다.

    native 위임도 이 CLI 로 라운드 파일을 준비하므로(스킬 규정) 엔진이 확실히 막을 수 있는 지점이
    여기다. 준비 부작용보다 앞에서 끊어야 off 형상에 고아 산출물이 남지 않는다.
    """
    delegate = _load("pm_delegate", "switch_pm_delegate")
    repo = _delegate_repo(tmp_path, "false")
    delegate.REPO = repo

    rc = delegate.main(["ticket", "prepare", "--ticket", "T-0001",
                        "--role", "developer", "--cwd", str(repo)])

    assert rc == 3
    err = capsys.readouterr().err
    assert "위임이 꺼져 있습니다" in err
    assert "delegate.enabled" in err
    assert not (repo / ".project_manager" / ".local").exists()   # 부작용 0


def test_delegation_switch_defaults_to_allow(tmp_path):
    """키가 없으면 허용이다 — 기본을 OFF 로 두면 기존 채택자의 native 위임이 새로 막힌다."""
    delegate = _load("pm_delegate", "default_pm_delegate")
    guard = _load("delegate_channel_guard", "default_guard")
    conf = {"delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x"}

    assert delegate._is_enabled(conf) is True
    assert delegate._is_enabled({**conf, "delegate.enabled": ""}) is True
    assert delegate._is_enabled({**conf, "delegate.enabled": "false"}) is False
    assert guard.decide("developer", "normal", {**conf, "delegate.enabled": "false"},
                        "claude")["verdict"] == "deny"


def test_harvest_and_copies_stay_outside_the_switch(tmp_path, capsys):
    """`harvest`·`copies` 는 게이트 밖이다 — 끄는 순간 진행 중 라운드가 고아가 되면 안 된다."""
    delegate = _load("pm_delegate", "harvest_pm_delegate")
    repo = _delegate_repo(tmp_path, "false")
    delegate.REPO = repo

    assert delegate.main(["ticket", "copies"]) == 0          # 조회면은 cwd 인자가 없다
    assert "위임이 꺼져 있습니다" not in capsys.readouterr().err


# ── ⑦ conf 생성 문자열 (주석이 값을 재진술하지 않는다) ──────────────────────


def test_generated_conf_comments_never_restate_a_configured_value():
    """불변식 4 — 엔진이 만드는 conf 주석에 설정값 재진술이 없다.

    실측 계기: `# 현행 매핑: dev…=codex` 주석 아래 실값이 전부 `claude` 였다. 손으로 옮겨 적은 값은
    반드시 drift 하므로, 주석은 "무엇을 하는 키인지" 만 말한다.
    """
    board = _load("board", "conf_writer_board")
    source = inspect.getsource(board._write_init_local_conf)
    generated = [line for line in source.splitlines() if '"' in line and "\\n" in line]
    for line in generated:
        stripped = line.strip()
        if not stripped.startswith('"#'):
            continue
        assert "=" not in stripped.split("#", 1)[1].split('\\n')[0], stripped


def test_init_conf_holds_only_real_values(tmp_path, monkeypatch):
    """생성된 conf 는 실값만 담는다 — 주석 처리된 예시 키가 0 이다(설명은 출하 문서 소유)."""
    board = _load("board", "init_conf_board")
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True)
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", pm / "local.conf")
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")

    board._write_init_local_conf()

    conf = (pm / "local.conf").read_text(encoding="utf-8")
    assert [line for line in conf.splitlines()
            if line.startswith("#") and "=" in line] == []
    assert board.local_config()["test.cmd"] == "pytest -q"


def test_shipped_catalog_documents_the_keys_the_conf_no_longer_explains(conf_module):
    """설명 블록의 이관처 — 출하 문서 카탈로그가 레지스트리 키를 실제로 담는다."""
    catalog = (REPO / "README.md").read_text(encoding="utf-8")
    for key in conf_module.KNOWN_KEYS:
        if key.startswith("additional_reviewer.") or key.startswith("upstream."):
            continue                       # 축 단위로 묶어 적는 행이 있다(아래에서 축을 본다)
        assert key in catalog, key
    for axis in ("additional_reviewer.", "upstream.", "harness.", "delegate."):
        assert axis in catalog, axis


# ── ⑧ 문구 규율 (동의 축 폐지 · 3타깃 파리티) ──────────────────────────────

_DELEGATE_SWITCH_SECTION = "### 위임 마스터 스위치"
_DELEGATE_CARDS = (
    REPO / ".claude/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/claude_code/.claude/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md",
)


def _switch_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(_DELEGATE_SWITCH_SECTION)
    end = text.index("\n## ", start)
    return text[start:end]


def test_delegate_switch_section_is_identical_across_targets():
    """세 출하 타깃의 스위치 절이 글자 단위로 같다 — 하네스별로 규칙이 갈리지 않는다."""
    sections = {path: _switch_section(path) for path in _DELEGATE_CARDS}
    assert len(set(sections.values())) == 1, [str(p) for p in sections]
    section = next(iter(sections.values()))
    for contract in ("기본은 허용", "rc=3", "ticket prepare", "harvest", "--dry-run",
                     "fail-open", "false-green"):
        assert contract in section, contract


def test_no_surface_calls_the_delegate_switch_a_consent_axis():
    """위임 축 문구에 "외부 송신·과금 동의" 가 0 이다 — 그 축은 폐지됐다.

    남은 동의 축은 추가 리뷰어(diff 통째 외부 전송)뿐이다. 위임 스위치를 동의로 다시 쓰면 native
    위임까지 "동의를 받아야 하는 행위" 로 되돌아가 스위치의 의미가 둘이 된다.
    """
    surfaces = [
        REPO / "README.md",
        REPO / ".project_manager/wiki/pm_role.md",
        REPO / ".project_manager/wiki/pm_playbook.md",
        REPO / "templates/codex/.codex/config.toml",
        *_DELEGATE_CARDS,
        *sorted(TOOLS.glob("*.py")),
    ]
    offenders: list[str] = []
    for path in surfaces:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "delegate" not in line and "위임" not in line:
                continue
            if "additional_reviewer" in line or "추가 리뷰어" in line:
                continue                       # 리뷰 축의 동의는 이번 릴리즈 존치다
            if "과금 동의" in line or "비용 동의" in line or "지속 동의" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], offenders


# ── ⑨ 실 conf 관측 (board lint advisory · never-block) ─────────────────────


def test_board_lint_surfaces_legacy_keys_and_restated_comments(tmp_path, monkeypatch):
    """채택자의 **실** conf 는 pytest 시야 밖이라 조회면이 본다 — 구표기와 값 재진술 주석 둘 다.

    엔진 생성 문자열은 정적 단언이 막지만, 이미 디스크에 있는 파일(git-ignored)은 그 단언이 닿지
    않는다. 관측은 advisory 다 — 값을 소비하지 않으므로 여기서 멈추면 안내 표면만 사라진다.
    """
    board = _load("board", "lint_conf_board")
    conf = _write_conf(tmp_path, _LEGACY_CONF + "# test.cmd=pytest -q\ntest.cmd=make test\n")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    findings = board.lint_local_conf()
    details = "\n".join(detail for _where, _kind, detail in findings)

    assert findings, "실 conf 의 구표기가 조회면에 뜨지 않는다"
    assert {kind for _where, kind, _detail in findings} == {"local-conf"}
    assert "local-conf" in board._ADVISORY_LINT_KINDS          # never-block
    assert "`delegate_enabled` → `delegate.enabled`" in details
    assert "주석이 실값과 다른 값을 재진술한다: `test.cmd=pytest" in details
    assert "실값 `make test`" in details


def test_board_lint_is_quiet_on_a_clean_conf(tmp_path, monkeypatch):
    """정상 conf 는 관측 0 이다 — 소음이 나면 조회면이 읽히지 않는다."""
    board = _load("board", "lint_clean_board")
    conf = _write_conf(tmp_path, "runtime.py=python3\ntest.cmd=pytest -q\n")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    assert board.lint_local_conf() == []


# ── ⑩ 어댑터 파서·셸 훅 (엔진을 import 하지 않는 독립 해소 경로) ───────────
#
# 이 넷은 엔진 로더를 쓰지 않는다(어댑터는 엔진 사본 경로에 묶이지 않는다는 계약). 그래서 엔진의
# fail-loud 가 여기까지 오지 않고, 구표기 conf 를 발견하고도 **조용히 엔진 기본값**(임계 30/20 ·
# 예산 200000)으로 강등할 수 있다 — 채택자 관점에서는 conf 를 고쳤는데 아무 일도 안 일어난다.
# 그래서 각 파서를 **프로세스로 띄우고 실 local.conf 를 넣어** 값으로 단언한다(문자열 존재 검사가
# 아니다). 차단 키 목록은 각 파서가 손으로 복제하지 않고 엔진 생성 블록을 그대로 품는다.

_ADAPTER_LEGACY_CONF = (
    "ctx_nudge_pct=91\n"
    "ctx_stop_pct=90\n"
    "ctx_window_tokens=12345\n"
    "ctx_window_tokens_claude=54321\n"
    "test_cmd=false\n"
)
_ADAPTER_NEW_CONF = (
    "ctx.nudge_pct=91\n"
    "ctx.stop_pct=90\n"
    "ctx.window_tokens=12345\n"
    "harness.claude.ctx_window_tokens=54321\n"
    "test.cmd=false\n"
)
_ADAPTER_LEGACY_KEYS = ("ctx_nudge_pct", "ctx_stop_pct", "ctx_window_tokens",
                        "ctx_window_tokens_claude", "test_cmd")

_PY_ADAPTER_DRIVER = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("adapter_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
conf = module.load_local_config(Path(sys.argv[2]))
print(sys.argv[3].join(str(part) for part in eval(sys.argv[4])))
"""

_PY_ADAPTERS = (
    pytest.param(".claude/ctx_guard.py",
                 '(module.ctx_thresholds(conf)["stop_pct"], module.resolve_budget(conf, "claude"))',
                 "90 54321", id="claude-canonical"),
    pytest.param("templates/claude_code/.claude/ctx_guard.py",
                 '(module.ctx_thresholds(conf)["stop_pct"], module.resolve_budget(conf, "claude"))',
                 "90 54321", id="claude-shipped"),
    pytest.param("templates/codex/.codex/pm_orch_codex.py",
                 "(module.resolve_stop_pct(conf), module.resolve_ctx_budget(conf))",
                 "90 12345", id="codex-shipped"),
    pytest.param("templates/opencode/.opencode/pm_orch_opencode.py",
                 "(module.resolve_stop_pct(conf), module.resolve_ctx_budget(conf))",
                 "90 12345", id="opencode-shipped"),
)


def _run_py_adapter(adapter: Path, root: Path, expression: str):
    """어댑터 파서를 **별도 프로세스**로 띄워 실 conf 에서 값을 해소시킨다."""
    return subprocess.run(
        [sys.executable, "-c", _PY_ADAPTER_DRIVER, str(adapter), str(root), " ", expression],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


@pytest.mark.parametrize("relpath,expression,expected", _PY_ADAPTERS)
def test_adapter_parser_stops_on_legacy_conf_before_resolving_values(
        conf_module, tmp_path, relpath, expression, expected):
    """구표기 conf 에서는 멈추고(rc≠0·키 지목), 신표기 conf 에서는 **그 값**을 해소한다."""
    adapter = REPO / relpath
    _write_conf(tmp_path, _ADAPTER_LEGACY_CONF)
    blocked = _run_py_adapter(adapter, tmp_path, expression)

    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout.strip() == "", "값 해소 뒤에 멈췄다(기본값이 이미 쓰였다)"
    assert conf_module.adapter_stop_message(
        tmp_path / ".project_manager" / "local.conf",
        sorted(_ADAPTER_LEGACY_KEYS)) in blocked.stderr

    _write_conf(tmp_path, _ADAPTER_NEW_CONF)
    resolved = _run_py_adapter(adapter, tmp_path, expression)
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == expected


def test_opencode_js_core_stops_on_legacy_conf_before_resolving_values(
        conf_module, tmp_path):
    """JS 파서도 같은 계약이다 — 같은 conf 를 넣고 node 프로세스로 값을 단언한다."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    core = REPO / "templates/opencode/.opencode/lib/ctx-guard-core.cjs"
    script = (
        f'const m = require({str(core)!r});'
        'const c = m.loadLocalConf(process.argv[1]);'
        'console.log(m.resolveThresholds(c).stop_pct, m.resolveBudget(c, "opencode"));'
    )
    _write_conf(tmp_path, _ADAPTER_LEGACY_CONF)
    blocked = subprocess.run([node, "-e", script, str(tmp_path)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=120)

    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout.strip() == ""
    assert conf_module.adapter_stop_message(
        tmp_path / ".project_manager" / "local.conf",
        sorted(_ADAPTER_LEGACY_KEYS)) in blocked.stderr

    _write_conf(tmp_path, _ADAPTER_NEW_CONF)
    resolved = subprocess.run([node, "-e", script, str(tmp_path)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == "90 12345"


@pytest.mark.parametrize("relpath", [
    ".claude/ctx_guard.py",
    "templates/claude_code/.claude/ctx_guard.py",
    "templates/codex/.codex/pm_orch_codex.py",
    "templates/opencode/.opencode/pm_orch_opencode.py",
    "templates/opencode/.opencode/lib/ctx-guard-core.cjs",
])
def test_adapter_blocked_key_block_is_generated_not_hand_copied(conf_module, relpath):
    """각 파서가 품은 차단 키 선언이 엔진 생성 산출과 **글자 단위로** 같다(사본 drift 0)."""
    style = {".py": "python", ".cjs": "js"}[Path(relpath).suffix]
    assert conf_module.render_adapter_block(style) in (
        REPO / relpath).read_text(encoding="utf-8")
