"""per-clone `local.conf` 의 정체성 키(`session=`/`prefix=`) 폐지 가드 (T-0779).

세션은 lease 장부, prefix 는 areas.md 칼럼이 단일 진실이다. 두 키는 slot·task 종속 값이라
프로젝트 공용 per-clone conf 의 범위가 아니었고, "등록 0 = solo 홈" 에서만 살아나는 그 폴백이
엔진의 유일한 solo 전용 경로였다. 이 파일은 그 경로가 **코드에서 사라졌다**는 사실을 세 축으로
못박는다:

  ① 정적 — 엔진 소스(canonical + 출하 템플릿 사본)에 그 키를 읽거나 쓰는 코드가 0.
  ② 동적 — 그 키만 있는 실 conf 픽스처에서 해소가 조용히 성공하지 않는다(조회 None·
     귀속 쓰기 fail-loud). 픽스처 문자열은 **구 엔진 init 이 실제로 쓰던 2줄**이다.
  ③ 형상 — `board init` 은 `--prefix` 유무와 무관하게 areas repo 행을 등록하므로 "등록 0"
     형상 자체가 새로 생기지 않는다. 남아 있는 등록 0 clone 에는 이관 안내가 붙는다.

②의 픽스처는 손으로 지어낸 conf 가 아니라 폐지 전 `board.py init` 이 만든 형상이다(주석 1줄 +
`session=pm`). 그래야 "구 채택자 clone 이 실제로 무엇을 만나는가" 를 값으로 단언한다.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATE_DIRS = ("claude_code", "codex", "opencode")

# 폐지 전 `board.py init` 이 fresh 홈에 쓰던 첫 두 줄 (구 채택자 clone 의 실 형상).
LEGACY_INIT_CONF_HEAD = (
    "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    "session=pm\n"
)

IDENTITY_KEYS = frozenset({"session", "prefix"})

# ① 줄-단위 패턴 — 폐지된 *이름* 과 conf 자체 파서 관용구. 값 조회는 아래 AST 스캐너가 본다
# (`conf = local_config()` 처럼 조회가 두 문장으로 갈라지면 한 줄 정규식이 못 본다).
_READ_NAME_PATTERNS = (
    # board 를 import 하지 않는 모듈이 같은 키를 stdlib 로 자체 파싱하던 관용구.
    re.compile(r'key\.strip\(\)\s*==\s*["\'](?:session|prefix)["\']'),
    re.compile(r'\b_local_conf_session\b'),
    re.compile(r'\bIDENTITY_SOURCE_SOLO_LOCAL_CONF\b'),
)
# 병합 경로 set-or-replace 의 dict 대입(`updates["session"] = …`) — 대상 이름은 conf/update 계열.
_WRITE_TARGET_NAME = re.compile(r'(?i)conf|updates?\b')
# conf 파일의 한 *줄* = 키로 시작해 개행 **또는 문자열 끝**에서 끝난다(마지막 줄에 개행이
# 없는 `write_text("prefix=pay")` 도 conf 줄이다 — F-007). 값 부분은 공백/탭을 포함하지
# 않는다 — 실 conf 값은 공백 없는 단일 토큰이고, 같은 이름을 쓰는 surface 출력
# (`prefix=… | …`)이나 dataclass __repr__(`session={self.session!r}, slot=…`)은 값 뒤에
# 공백이 와 갈린다(placeholder 만으로는 그 구분이 안 보이므로 문자 클래스로 막는다). 문자열
# 리터럴의 실제 값을 보므로 f-string 여부·따옴표 종류와 무관하다.
_CONF_LINE = re.compile(r'(?:\A|\n)(?:session|prefix)=[^\n \t]*(?:\n|\Z)')
# conf dict 를 돌려주거나 conf 키를 인자로 받는 함수의 이름 규칙(모듈 공용).
_CONF_ACCESSOR_NAME = re.compile(r'local_conf(?:ig)?')
_DICT_LOOKUP_METHODS = frozenset({"get", "pop", "setdefault"})
_FORMATTED_VALUE_PLACEHOLDER = "\x00"


def _callee_name(node: ast.AST) -> str:
    """호출 대상의 마지막 이름 조각 (`board.local_config` → `local_config`)."""
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_conf_accessor_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and bool(_CONF_ACCESSOR_NAME.search(_callee_name(node)))


def _identity_key_arg(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in IDENTITY_KEYS:
        return node.args[0].value
    return None


def _scopes(tree: ast.AST):
    """모듈 + 각 함수 본문 — taint 를 그 스코프 안으로 가둔다(다른 dict 의 같은 변수명 오검출 방지)."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def conf_identity_reads(source: str) -> list[tuple[int, str]]:
    """conf dict 에서 `session`/`prefix` 를 읽는 자리 전부 — (줄번호, 사유).

    한 줄 chain(`local_config().get("session")`)만이 아니라 **변수를 거친 두 문장**
    (`conf = local_config()` … `conf.get("session")`)·subscript·`in` 검사·키를 인자로 받는
    accessor(`_local_conf_value("session")`)까지 같은 클래스로 본다.
    """
    tree = ast.parse(source)
    hits: dict[int, str] = {}
    for scope in _scopes(tree):
        # taint 원천 = `Assign`(`conf = local_config()`) **과** `AnnAssign`
        # (`conf: dict = local_config()`) 양쪽 — 뒤엣것만 없으면 타입 주석이 붙은 변수를 거친
        # 두 문장 조회가 taint 밖으로 빠진다.
        assign_sources = (
            (node.targets, node.value) for node in ast.walk(scope)
            if isinstance(node, ast.Assign)
        )
        ann_sources = (
            ([node.target], node.value) for node in ast.walk(scope)
            if isinstance(node, ast.AnnAssign) and node.value is not None
        )
        tainted = {
            target.id
            for targets, value in (*assign_sources, *ann_sources)
            if _is_conf_accessor_call(value)
            for target in targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                key = _identity_key_arg(node)
                if key is None:
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _DICT_LOOKUP_METHODS:
                    receiver = func.value
                    if _is_conf_accessor_call(receiver) or (
                            isinstance(receiver, ast.Name) and receiver.id in tainted):
                        hits[node.lineno] = f'conf dict 에서 "{key}" 조회'
                elif _CONF_ACCESSOR_NAME.search(_callee_name(node)):
                    hits[node.lineno] = f'conf accessor 에 "{key}" 키 전달'
            elif (isinstance(node, ast.Subscript)
                    and (_is_conf_accessor_call(node.value)
                         or (isinstance(node.value, ast.Name) and node.value.id in tainted))
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value in IDENTITY_KEYS):
                hits[node.lineno] = f'conf dict["{node.slice.value}"] 인덱싱'
            elif isinstance(node, ast.Compare):
                for op, right in zip(node.ops, node.comparators):
                    if (isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Name)
                            and right.id in tainted and isinstance(node.left, ast.Constant)
                            and node.left.value in IDENTITY_KEYS):
                        hits[node.lineno] = f'conf dict 에 "{node.left.value}" 존재 검사'
    return sorted(hits.items())


def _literal_text(node: ast.AST) -> str | None:
    """문자열 리터럴의 실제 값 — f-string 은 치환부를 placeholder 로 둔 근사 텍스트.

    `FormattedValue.value` 자체가 정적 문자열(`f"{'prefix='}{prefix}"` 처럼 중첩 formatted
    expression 으로 조립한 conf write)이면 그 조각도 재귀적으로 이 함수로 평탄화해 결합
    텍스트에 반영한다(F-007) — placeholder 는 정말 동적인 리프(`Name`·`Call`·`IfExp` 등)에만
    쓴다. 그래야 `f"{'prefix='}{prefix}"` 가 placeholder 로 뭉개져 사라지지 않는다. 반대로
    `templates/*/board.py:11206` 같은 `('prefix=' + prefix + ' · ') if namespaced else ''`
    (동적 조건의 삼항)은 이 함수가 풀어내지 못해 그대로 placeholder 가 되고, 그 앞의 리터럴
    `"✓ local.conf: "` 가 여전히 결합 텍스트에 남아 `session=`/`prefix=` 가 줄 시작(`\\A`/`\\n`)에
    오지 않게 막는다 — 오탐은 그대로 해소된 채 유지된다.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.FormattedValue):
                nested = _literal_text(piece.value)
            else:
                nested = _literal_text(piece)
            parts.append(nested if nested is not None else _FORMATTED_VALUE_PLACEHOLDER)
        return "".join(parts)
    return None


def conf_identity_writes(source: str) -> list[tuple[int, str]]:
    """conf 텍스트에 그 키 *줄* 을 심거나 conf dict 에 그 키를 대입하는 자리 전부."""
    tree = ast.parse(source)
    # f-string 안의 정적 조각(`ast.JoinedStr.values` 의 `Constant`)과 `{…}` 치환식 내부의 리터럴
    # (예 `f"...{('prefix=' + x) if c else ''}"` 의 `'prefix='`)은 `ast.walk` 이 부모 `JoinedStr`
    # 와 별개 노드로도 순회한다 — 조각만 떼어보면 "session="/"prefix=" 로 끝나는 반쪽 문자열이
    # `\Z`(문자열 끝) 종결 조건에 우연히 걸린다(엔클로징 f-string 전체·치환식 문맥에서는 뒤에
    # " | " 등 non-conf 내용이 이어져 걸리지 않는데도). `JoinedStr` 서브트리 전체를 건너뛰고
    # **그 노드 자신의 결합 텍스트**로만 본다 — `_literal_text` 가 그 결합 텍스트를 문맥 보존
    # 방식으로 재구성한다(치환식 안의 정적 문자열 조각은 반영·정말 동적인 리프만 placeholder,
    # F-007). 그래서 `f"{'prefix='}{prefix}"` 같은 중첩 formatted expression 조립도 실제 conf
    # 줄로 잡히면서, 앞의 문맥이 살아있는 `board.py:11206` 류 삼항 surface 문구는 여전히 안 잡힌다.
    joined_descendant_ids = {
        id(descendant)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for descendant in ast.walk(node)
        if descendant is not node
    }
    hits: dict[int, str] = {}
    for node in ast.walk(tree):
        if id(node) in joined_descendant_ids:
            continue
        text = _literal_text(node)
        if text is not None:
            match = _CONF_LINE.search(text)
            if match:
                hits[node.lineno] = f"conf 줄 리터럴 {match.group().strip()!r}"
            continue
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and _WRITE_TARGET_NAME.search(target.value.id)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in IDENTITY_KEYS):
                hits[node.lineno] = f'conf dict 에 "{target.slice.value}" 대입'
    return sorted(hits.items())


def _engine_sources() -> list[Path]:
    """canonical 엔진 + 출하 템플릿 사본의 모든 `.py` (스캔 대상 전수).

    템플릿 사본까지 보는 이유: 채택자가 실제로 실행하는 것은 그 사본이다. canonical 만 고치고
    전파를 잊으면 채택자 트리에서는 폐지된 폴백이 계속 산다.
    """
    sources = sorted(TOOLS.glob("*.py"))
    for flavor in TEMPLATE_DIRS:
        tools = REPO / "templates" / flavor / ".project_manager" / "tools"
        if tools.is_dir():
            sources.extend(sorted(tools.glob("*.py")))
    return sources


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── ① 정적: 읽는 코드 0 · 쓰는 코드 0 ─────────────────────────────────────────

def _scan_engine(scanner) -> list[str]:
    """엔진 소스 전수를 스캐너에 태우고 위반을 `경로:줄: 사유` 로 평탄화한다."""
    offenders = []
    for path in _engine_sources():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for lineno, why in scanner(source):
            offenders.append(f"{path.relative_to(REPO)}:{lineno}: {why} — {lines[lineno - 1].strip()}")
    return offenders


def test_no_engine_source_reads_the_local_conf_identity_keys():
    """엔진 소스 어디에도 conf `session=`/`prefix=` 를 읽는 코드가 없다 (템플릿 사본 포함)."""
    offenders = _scan_engine(conf_identity_reads)
    offenders += [
        f"{path.relative_to(REPO)}:{index}: 폐지된 이름 — {line.strip()}"
        for path in _engine_sources()
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for pattern in _READ_NAME_PATTERNS
        if pattern.search(line)
    ]
    assert not offenders, (
        "per-clone local.conf 의 정체성 키를 읽는 코드가 남아 있다 — 세션은 lease 장부,\n"
        "prefix 는 areas.md 가 단일 진실이다:\n  " + "\n  ".join(sorted(offenders))
    )


def test_no_engine_source_writes_the_local_conf_identity_keys():
    """엔진 소스 어디에도 conf 에 그 키를 쓰는 코드가 없다 (템플릿 사본 포함).

    읽기만 지우고 쓰기를 남기면 채택자 conf 에 읽히지 않는 키가 계속 쌓인다.
    """
    offenders = _scan_engine(conf_identity_writes)
    assert not offenders, (
        "local.conf 에 정체성 키를 쓰는 코드가 남아 있다:\n  " + "\n  ".join(offenders)
    )


# 가드 자기검증 표본 — 폐지된 관용구를 *형태별*로 나열한다. 한 줄 chain 만 잡던 옛 가드가
# 놓치던 형태(두 문장 조회·subscript·존재검사·비 f-string conf 줄)를 각각 포함한다.
_RETIRED_READ_SAMPLES = (
    ('sess = local_config().get("session")', "한 줄 chain"),
    ('return local_config().get("prefix") or None', "한 줄 chain(prefix)"),
    ('conf = local_config()\nsess = conf.get("session")', "두 문장 조회"),
    ('def f():\n    conf = board.local_config()\n    return conf["prefix"]', "subscript"),
    ('def f():\n    conf = local_config()\n    return "session" in conf', "존재 검사"),
    ('value = _local_conf_value("session")', "키를 인자로 받는 accessor"),
    # F-007 — 확인 라운드가 잡은 추가 false-negative 2건(accessor 직접 receiver·AnnAssign taint).
    ('value = local_config()["session"]', "accessor call 직접 subscript"),
    ('conf: dict = local_config()\nvalue = conf.get("session")', "AnnAssign 을 거친 두 문장 조회"),
)
_RETIRED_READ_NAME_SAMPLES = (
    '        if key.strip() == "session":',
    'IDENTITY_SOURCE_SOLO_LOCAL_CONF = "solo local.conf"',
)
_RETIRED_WRITE_SAMPLES = (
    ('conf += f"session={sess}\\n"', "f-string conf 줄"),
    ('conf += "session=pm\\n"', "비 f-string conf 줄"),
    ('HEAD = "# per-clone 설정\\nprefix=ACC\\n"', "여러 줄 리터럴 안의 conf 줄"),
    ('updates["prefix"] = prefix', "병합 경로 dict 대입"),
    # F-007 — 종결 개행 없는 conf 줄(마지막 줄에 `\n` 을 안 붙인 `write_text` 호출).
    ('LOCAL_CONF.write_text("prefix=pay")', "종결 개행 없는 conf 줄"),
    # F-007 확인 라운드 퇴행 — `JoinedStr` descendant 전체 제외가 놓치던 중첩 formatted
    # expression 조립(정적 문자열 조각이 `{…}` 안에 있는 실제 conf write).
    (
        '''LOCAL_CONF.write_text(f"{'prefix='}{prefix}")''',
        "중첩 f-string(정적 조각으로 조립한 실제 conf 줄·prefix)",
    ),
    (
        '''LOCAL_CONF.write_text(f"{'session='}{session}\\n")''',
        "중첩 f-string(정적 조각으로 조립한 실제 conf 줄·session+개행)",
    ),
)
# 반대 방향 — 같은 이름을 쓰지만 conf 와 무관한 코드는 잡히면 안 된다(가드가 정상 코드를 막지 않음).
_INNOCENT_SAMPLES = (
    'row = {}\nvalue = row.get("prefix")',                    # areas 행 dict 조회
    'print(f"prefix={prefix or \'none\'} | {repo}")',           # surface 출력(개행 없음)
    'text = f"session={self.session!r}, slot={self.slot!r}"',  # dataclass __repr__
    # F-007 — 실 코드 `templates/*/board.py:11206` 형태(동적 조건 삼항 안의 정적 조각). 앞의
    # 리터럴 문맥("✓ local.conf: ")이 살아 있어 session=/prefix= 가 줄 시작에 오지 않는다.
    '''print(f"✓ local.conf: {('prefix=' + prefix + ' · ') if namespaced else ''}session={surface_sess}")''',
)


def test_static_scan_actually_catches_the_retired_idioms():
    """가드 자기검증 — 폐지된 관용구를 형태별로 넣으면 전부 잡히고, 무관한 코드는 안 잡힌다."""
    for source, label in _RETIRED_READ_SAMPLES:
        assert conf_identity_reads(source), f"read 가드가 놓침({label}): {source}"
    for line in _RETIRED_READ_NAME_SAMPLES:
        assert any(p.search(line) for p in _READ_NAME_PATTERNS), f"read 이름 가드가 놓침: {line}"
    for source, label in _RETIRED_WRITE_SAMPLES:
        assert conf_identity_writes(source), f"write 가드가 놓침({label}): {source}"
    for source in _INNOCENT_SAMPLES:
        assert not conf_identity_reads(source) and not conf_identity_writes(source), (
            f"가드가 무관한 코드를 오검출: {source}")


def test_the_three_self_parsing_modules_dropped_their_copies():
    """board 를 import 하지 않는 3모듈의 conf `session=` 자체 파서가 전부 사라졌다.

    한 모듈만 남으면 저장측/매칭측 세션 해소가 조용히 갈린다(per-slot test_cmd·claim 소유권 미스).
    """
    for name, filename in (("pm_config", "pm_config.py"),
                           ("worktree_pool", "worktree_pool.py"),
                           ("pm_log", "pm_log.py")):
        module = _load(f"t0779_{name}", filename)
        assert not hasattr(module, "_local_conf_session"), name
    pm_log = _load("t0779_pm_log_sources", "pm_log.py")
    assert not hasattr(pm_log, "IDENTITY_SOURCE_SOLO_LOCAL_CONF")
    assert "solo local.conf" not in pm_log.SNAPSHOT_IDENTITY_SOURCES


# ── ② 동적: 구 clone 형상에서 조용한 폴백이 없다 ────────────────────────────

@pytest.fixture
def board(tmp_path, monkeypatch):
    """구 채택자 clone 형상의 hermetic board — 실 init 이 만든 conf 2줄만 있고 장부는 없다."""
    project = tmp_path / "proj"
    pm_dir = project / ".project_manager"
    tickets = pm_dir / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (pm_dir / ".local").mkdir(parents=True, exist_ok=True)
    module = _load(f"board_t0779_{id(tmp_path)}", "board.py")
    for name, value in {
        "REPO": project,
        "TICKETS_DIR": tickets,
        "BOARD_FILE": pm_dir / "wiki" / "board.md",
        "LOG_FILE": pm_dir / "wiki" / "log" / "current.md",
        "STATUS_FILE": pm_dir / "wiki" / "status.md",
        "LOCAL_CONF": pm_dir / "local.conf",
        "AREAS_FILE": pm_dir / "areas.md",
        "LOCAL_DIR": pm_dir / ".local",
        "BOARD_LOCK": pm_dir / ".local" / "board.lock",
        "LEASES_FILE": pm_dir / ".local" / "worktree-leases.json",
        "PM_STATE_FILE": pm_dir / "wiki" / "pm_state.md",
        "PM_STATE_TEMPLATE": pm_dir / "wiki" / "pm_state.template.md",
    }.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(module, "prompt_external_review_optin", lambda: None)
    monkeypatch.setattr(module, "prompt_delegate_optin", lambda: None)
    monkeypatch.setattr(module, "_configure_board_submodule", lambda: False)
    monkeypatch.setattr(module, "_git_config_email", lambda: None)
    monkeypatch.setattr(module, "_detect_py", lambda: "python3")
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return module


def test_legacy_conf_session_does_not_resolve_a_session(board):
    """`session=pm` 만 있는 구 clone 조회 → None (조용한 폴백 0)."""
    board.LOCAL_CONF.write_text(LEGACY_INIT_CONF_HEAD, encoding="utf-8")
    assert board.session_name() is None


def test_legacy_conf_session_fails_loud_on_attributed_write(board):
    """같은 형상의 귀속 쓰기 → fail-loud 문구를 **실값**으로 단언한다."""
    board.LOCAL_CONF.write_text(LEGACY_INIT_CONF_HEAD, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        board.session_name(required=True)
    assert str(exc.value) == board.UNREGISTERED_SESSION_ABORT
    assert "`--repo <repo> --slot <N>`" in str(exc.value)


def test_legacy_conf_prefix_does_not_namespace_new_tickets(board):
    """`prefix=ACC` 만 있는 구 clone → 해소 None·발행은 무prefix `T-NNNN`(none 카테고리)."""
    board.LOCAL_CONF.write_text(LEGACY_INIT_CONF_HEAD + "prefix=ACC\n", encoding="utf-8")
    assert board.id_prefix(None) is None
    assert board.known_prefixes() == frozenset()   # 승인 게이트 3소스에도 없다


def test_lease_ledger_shape_resolves_the_session(board):
    """실 lease 장부(단일 leased 행) 형상이면 같은 홈에서 세션이 해소된다 — 역방향 확인.

    폴백 제거가 *정상* 사용을 막지 않는다: 슬롯을 대여한 홈은 장부에서 세션이 나오고,
    귀속 쓰기도 fail-loud 하지 않는다.
    """
    import json
    board.LOCAL_CONF.write_text(LEGACY_INIT_CONF_HEAD, encoding="utf-8")
    board.LEASES_FILE.write_text(json.dumps({"leases": [{
        "slot": "work/proj_1", "repo": "proj", "session": "proj_1", "state": "leased",
    }]}), encoding="utf-8")
    assert board.session_name() == "proj_1"
    assert board.session_name(required=True) == "proj_1"


def test_env_binding_resolves_the_session(board):
    """env 명시(`PM_SESSION_NAME`)도 그대로 해소된다 — 명시 층은 그대로다."""
    board.LOCAL_CONF.write_text(LEGACY_INIT_CONF_HEAD, encoding="utf-8")
    import os
    os.environ["PM_SESSION_NAME"] = "proj_2"
    try:
        assert board.session_name(required=True) == "proj_2"
    finally:
        os.environ.pop("PM_SESSION_NAME", None)


# ── ③ 형상: init 이 등록 0 을 만들지 않는다 + 등록 두 축(repo·카테고리)의 실제 값 ──
# 폐지된 conf 폴백을 대체하는 것이 areas repo 행이므로, "등록이 실제로 남는가" 를 값으로 친다.
# 특히 무prefix init 뒤의 카테고리 지정(재실행)이 **셀 갱신**인지 — 여기서 2행이 되면 한 물리
# repo 가 두 repo 로 등록돼 세션→repo→prefix 해소가 깨진다.

def _init_args(**kv):
    base = dict(prefix=None, area=None, owner=None, repo=None, slot=None,
                user=None, user_ack=None)
    base.update(kv)
    return argparse.Namespace(**base)


def _new_args(**kv):
    base = dict(title="t", prefix=None, touches=None, depends=None, tag=None,
                estimate="small", user_ack=None)
    base.update(kv)
    return argparse.Namespace(**base)


def test_init_without_prefix_leaves_no_unregistered_shape(board):
    """무prefix init 1회 → repo 행 등록 + conf 에 정체성 키 0 + 이관 안내 소멸."""
    assert board.lint_areas_repo_unregistered()          # 등록 전엔 안내가 붙는다
    assert board.cmd_init(_init_args()) == 0
    assert board.registered_repos() == {board.REPO.name}
    conf = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "session=" not in conf and "prefix=" not in conf
    assert board.lint_areas_repo_unregistered() == []    # 등록 후엔 사라진다


def test_unregistered_shape_advisory_names_the_init_rerun(board):
    """등록 0 형상의 이관 안내는 처방(`board.py init` 재실행)을 지목하고 never-block 이다."""
    findings = board.lint_areas_repo_unregistered()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert (label, kind) == ("areas.md", "areas-repo-unregistered")
    assert "board.py init" in detail
    assert "session=" in detail and "prefix=" in detail   # 더 이상 읽지 않음을 명시
    assert kind in board._ADVISORY_LINT_KINDS            # push 미차단(advisory)
    assert findings[0] in board.lint_tickets()           # 집계에 배선됨


def test_both_id_formats_keep_resolving_on_a_mixed_board(board):
    """무prefix·prefixed 티켓이 섞인 보드에서 두 형식이 계속 해소·조회·완료된다.

    실 보드 건수는 테스트가 셀 수 없으므로(엔진 저장소엔 보드가 없다) 두 형식을 섞은 픽스처로
    같은 성질을 친다 — 폴백 제거가 기존 ID 를 건드리지 않는다.
    """
    board.cmd_init(_init_args())
    for ticket_id in ("T-0778", "T-0779", "T-project_manager-001", "T-project_manager-002"):
        path = board.TICKETS_DIR / "open" / f"{ticket_id}-seed.md"
        board.dump_ticket(path, {"id": ticket_id, "title": "seed", "status": "open",
                                 "created": "2026-08-21"}, f"# {ticket_id} — seed\n")
    board.invalidate_known_prefixes_cache()
    assert board._ticket_prefix("T-0779") is None
    assert board._ticket_prefix("T-project_manager-002") == "project_manager"
    assert board._next_id(None) == "T-0780"
    assert board._next_id("project_manager") == "T-project_manager-003"
    assert board.known_prefixes() == frozenset({"project_manager"})


def _areas_data_rows(board) -> list[str]:
    """areas.md 의 데이터 행(헤더 제외) 원문 — 행이 몇 개 생겼는지 값으로 센다."""
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    return [line for line in text.splitlines()
            if line.startswith("| ") and not line.startswith("| repo |")]


def test_category_init_after_bare_init_updates_the_cell_not_a_second_row(board, capsys):
    """무prefix init → 카테고리 init 재실행: 행은 1개 그대로, prefix 칼럼만 채워진다.

    두 축(repo 칼럼 = 이 clone · prefix 칼럼 = 작업 카테고리)이 분리돼 있어야 재실행이 멱등하다.
    """
    assert board.cmd_init(_init_args()) == 0
    assert board.registered_repos() == {"proj"} and board.registered_prefixes() == set()
    assert board.id_prefix(None) is None
    assert board._next_id(None) == "T-0001"

    assert board.cmd_init(_init_args(prefix="acct", area="회계", user_ack="acct")) == 0
    board.invalidate_known_prefixes_cache()
    assert len(_areas_data_rows(board)) == 1                  # 행 추가 없음(셀 갱신)
    assert board.registered_repos() == {"proj"}
    assert board.registered_prefixes() == {"acct"}
    assert board.id_prefix(None) == "acct"
    assert board._next_id("acct") == "T-acct-001"
    assert "기존 행의 prefix 칼럼 갱신" in capsys.readouterr().out


def test_category_named_after_the_clone_folder_stays_one_row(board):
    """카테고리 이름이 clone 폴더명과 같아도 행은 1개다 (prefix 를 repo 이름으로 쓰지 않는다)."""
    assert board.cmd_init(_init_args()) == 0
    assert board.cmd_init(_init_args(prefix="proj", area="본체", user_ack="proj")) == 0
    board.invalidate_known_prefixes_cache()
    assert len(_areas_data_rows(board)) == 1
    assert board.registered_repos() == {"proj"} and board.registered_prefixes() == {"proj"}
    assert board.id_prefix(None) == "proj"


def test_explicit_repo_flag_names_the_areas_row_not_the_prefix(board):
    """`--repo svc --prefix acct` → repo 칼럼은 `svc`, prefix 칼럼은 `acct`.

    repo 칼럼에 카테고리가 들어가면 세션 `svc_1` → repo `svc` 행 조회가 빗나가 prefix 해소가 깨진다.
    """
    assert board.cmd_init(_init_args(repo="svc", prefix="acct", area="회계",
                                     user_ack="acct")) == 0
    assert board.registered_repos() == {"svc"}
    board.invalidate_known_prefixes_cache()
    assert board.registered_prefixes() == {"acct"}
    assert board._prefix_from_session("svc_1") == "acct"      # 세션 유도 해소가 산다


def test_repo_only_init_registers_with_zero_active_leases(board):
    """활성 lease 0(=fresh clone)에서도 `--repo <이름>` 만으로 등록된다.

    등록명 지정은 actor 정체성 해소와 다른 축이다 — 여기서 슬롯 미해소로 중단하면 repo 이름
    유도 실패 안내(`--repo <이름>`)가 그 자리에서 죽는다.
    """
    assert not board.LEASES_FILE.exists()                    # 장부 자체가 없다
    assert board.cmd_init(_init_args(repo="billing")) == 0
    assert board.registered_repos() == {"billing"}
    assert _areas_data_rows(board)[0].split("|")[1].strip() == "billing"


def test_changing_the_category_by_init_is_refused_with_zero_side_effects(board, capsys):
    """이미 카테고리가 있는 행에 다른 카테고리로 init → 부작용 0 fail-loud + 처방 지목."""
    board.cmd_init(_init_args(prefix="acct", area="회계", user_ack="acct"))
    capsys.readouterr()
    assert board.cmd_init(_init_args(prefix="ops", area="운영", user_ack="ops")) == 1
    err = capsys.readouterr().err
    assert "board.py prefix rename acct ops" in err
    board.invalidate_known_prefixes_cache()
    assert len(_areas_data_rows(board)) == 1
    assert board.registered_prefixes() == {"acct"}           # 갈아끼우지 않았다


def test_a_category_taken_by_another_repo_is_refused(board, capsys):
    """다른 repo 행이 쓰는 카테고리는 거부 — 두 repo 가 같은 ID 순번을 나눠 쓰지 못한다."""
    board.areas_append("acct", "회계", "other_1", repo="other")
    capsys.readouterr()
    assert board.cmd_init(_init_args(prefix="acct", area="회계", user_ack="acct")) == 1
    assert "이미 repo 'other' 행에" in capsys.readouterr().err
    assert board.registered_repos() == {"other"}             # 이 clone 행은 생기지 않았다


def test_completion_surface_prints_only_id_formats_the_board_will_issue(board, capsys):
    """완료 안내의 ID 포맷 = 이 clone 에서 `board.py new` 가 실제로 발행할 값."""
    board.cmd_init(_init_args())
    bare = capsys.readouterr().out
    assert "T-NNNN" in bare and "T-proj-NNN" not in bare
    assert board._next_id(board.id_prefix(None)) == "T-0001"

    board.cmd_init(_init_args(prefix="acct", area="회계", user_ack="acct"))
    board.invalidate_known_prefixes_cache()
    named = capsys.readouterr().out
    assert "T-acct-NNN" in named
    assert board._next_id(board.id_prefix(None)) == "T-acct-001"


def test_completion_surface_flags_ambiguity_instead_of_a_bare_id_it_cannot_issue(board, capsys):
    """다중-카테고리·세션 미바인딩 형상 — 완료 안내가 `cmd_new` 의 ≥2 모호 가드와 같은 판정을 쓴다.

    시퀀스(확인 라운드 재현): 이 clone 이 무prefix init → 다른 repo 행(`ops`)이 이미 카테고리를
    쓰는 형상에서 이 clone 이 `acct` 카테고리를 셀 갱신으로 얻는다. `id_prefix(None)` 은 등록
    카테고리가 ≥2 이고 세션이 안 묶여 None 이지만, 그건 "무prefix 발행 가능" 이 아니라 `cmd_new`
    가 자신의 ≥2 가드로 거부하는 모호 상태다 — 완료 안내가 `T-NNNN` 을 성공 메시지로 내면 보드가
    발행하지 않을 포맷을 광고하는 것이다.
    """
    assert board.cmd_init(_init_args()) == 0
    board.areas_append("ops", "운영", "other_1", repo="other")   # 다른 repo 가 이미 한 카테고리 사용
    board.invalidate_known_prefixes_cache()
    capsys.readouterr()
    assert board.cmd_init(_init_args(prefix="acct", area="회계", user_ack="acct")) == 0
    out = capsys.readouterr().out
    board.invalidate_known_prefixes_cache()
    assert board.registered_prefixes() == {"ops", "acct"}
    assert board.id_prefix(None) is None                        # 미해소(모호) — count-based 아님
    # 완료 안내는 발행 불가 포맷을 성공 메시지로 내지 않고, 명시 --prefix 커맨드를 지목한다.
    assert "T-NNNN (none 카테고리)" not in out
    assert "--prefix <PFX>" in out
    # 직후 무명시 cmd_new 는 안내가 예고한 대로 실제로 거부된다 — 안내와 실 거동의 정합.
    assert board.cmd_new(_new_args()) == 1


def test_completion_surface_still_issues_the_bare_format_when_unambiguous(board, capsys):
    """역방향 확인 — 등록이 단일-repo·무prefix 뿐이면 완료 안내는 여전히 `T-NNNN` 을 낸다.

    F-001 모호-형상 가드를 조인해도 정상(비모호) 형상의 완료 안내가 사라지면 안 된다.
    """
    # cmd_new 발행에 필요한 본문 템플릿 — 이 fixture 는 완료 안내(cmd_init)만 겨냥해 만들지
    # 않으므로, 이 테스트에서만 최소 템플릿을 심는다(다른 테스트는 무영향).
    board.template_file().write_text(
        "---\nid: T-NNNN\ntitle: <제목>\nstatus: open\ncreated: YYYY-MM-DD\n"
        "claimed_by:\nclaimed_at:\ncompleted_at:\ndepends_on: []\nblocks: []\n"
        "touches: []\nestimate: small\ntags: []\n---\n\n# T-NNNN — <제목>\n\n## 목표\n채워라.\n",
        encoding="utf-8",
    )
    assert board.cmd_init(_init_args()) == 0
    out = capsys.readouterr().out
    assert "T-NNNN (none 카테고리)" in out
    assert "--prefix <PFX>" not in out
    assert board.cmd_new(_new_args()) == 0                       # 안내대로 무명시 발행이 실제로 된다
