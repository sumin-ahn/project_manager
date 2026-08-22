"""bare 등재 × 본문 운영 토큰 × 토큰 소유 선언 매트릭스 가드 (T-0578).

manifest 에 **bare**(무 `@render`) 로 등재된 경로는 `pm_update` 가 byte-copy 로 덮는다. 그런
파일의 본문에 운영 토큰(`{{PROJECT_NAME}}`·`{{DATE}}` …)이 있는데 설치(`pm_import`)가 그 토큰을
값으로 치환하면, 채택자 디스크는 값-form / upstream 은 토큰-form 이 되어 **매 sync 마다
진동한다** — 채택자 실측(v1.6.0 제보): `pm_update` 후 `wiki/README.md` 가 `{{PROJECT_NAME}}` 로,
`pm_state.template.md`·`domain/_template.md` 가 `{{DATE}}` 로 회귀.

근본은 "같은 파일을 두 주체가 반대 방향으로 소유" 다. 그래서 토큰의 소유자를 **파일 × 토큰**
단위로 하나만 둔다:

  ① 설치 시 치환 — `pm_import`(기본값)
  ② 소비 시 치환 — 그 템플릿이 산출물을 만드는 시점(`worktree_pool.ensure_task_pm_state`·
     `board.cmd_init`·사람이 스캐폴드를 복사하는 시점)
  ③ 상시 토큰   — 엔진 소유 문서가 토큰을 *설명*으로 담아 아무도 치환하지 않는다
     (엔진 소스 `tools/**`·`engine.manifest`·방법론 문서 pm_role/pm_playbook)

이 파일이 닫는 것:

  (A) 전 flavor manifest(루트 + `templates/*`) 의 bare 등재 파일 **전수 스캔** — 본문 운영 토큰이
      ②·③ 어느 소유 선언에도 안 걸리면 fail(진동 예약).
  (B) ② 선언 원장(`pm_import.CONSUMPTION_TIME_TOKENS`)과 이 파일의 allow 목록이 정확히 일치하고,
      각 소비처가 **실재**한다(소비처가 사라지면 선언이 dangling → red).
  (C) 신규 설치 직후 템플릿 2종의 `{{DATE}}` 가 토큰으로 남는다(같은 파일의 다른 토큰은 무영향).
  (D) ② 소비처가 실제로 돈다 — `ensure_task_pm_state`·`board.cmd_init` 산출물의 날짜 = 생성 시각.
  (E) e2e — import → `pm_update` 왕복 후 대상 문서 byte 불변(진동 0·샌드박스 재현 시나리오 박제).

기계 테스트 — 라이브 하니스·네트워크 0(`opencode models` 조회 stub·framework 사본은 비-git).
"""
from __future__ import annotations

import argparse
import ast
import datetime
import importlib.util
import io
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

PROJECT_NAME = "AcmeProj"

# ② 소비 시점 소유 원장 — 엔진 선언(`pm_import.CONSUMPTION_TIME_TOKENS`)과 **정확히 일치**해야
#   한다(선언이 조용히 늘 수 없다). 값 = 그 토큰을 실제로 채우는 소비처:
#     "<module>.<symbol>" — 엔진 코드가 산출물 생성 시점에 치환. 가드가 그 모듈에 심볼이 실재하고
#                           **그 함수 본문이 토큰 리터럴을 다루는지** AST 로 확인한다(no-op 선언 금지).
#     SCAFFOLD_COPY       — 사람이 스캐폴드를 복사해 산출물을 만들 때 채운다. 가드가 그 파일이 실제
#                           템플릿 스캐폴드 관례(`pm_import._is_template_scaffold`)인지 확인한다.
SCAFFOLD_COPY = "<scaffold-copy>"

CONSUMPTION_TIME_OWNERS: dict[str, dict[str, tuple[str, ...]]] = {
    ".project_manager/wiki/pm_state.template.md": {
        # task pm_state 렌더 + per-clone `wiki/pm_state.md` seed — 둘 다 생성 시각으로 채운다.
        "{{DATE}}": ("worktree_pool.ensure_task_pm_state", "board.cmd_init"),
    },
    ".project_manager/wiki/domain/_template.md": {
        # 엔진 생성 경로(`domain.write_draft_page`)는 스캐폴드 frontmatter 를 복사하지 않고 자체
        # today 로 쓴다 — 이 토큰의 소비처는 스캐폴드를 손으로 복사해 페이지를 만드는 시점뿐이다.
        "{{DATE}}": (SCAFFOLD_COPY,),
    },
}


def _load(name: str, alias: str | None = None):
    spec = importlib.util.spec_from_file_location(alias or name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    mod = _load("pm_import")
    # hermetic — 라이브 `opencode models` CLI 미호출(미설치 동치).
    mod._real_models_runner = lambda: (False, [])
    return mod


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


@pytest.fixture(scope="module")
def repo_files():
    return _load("repo_owned_files")


@pytest.fixture(scope="module")
def board():
    return _load("board", alias="board_for_token_ownership_test")


# ── manifest 인벤토리 (bare 등재가 나르는 파일 전수) ─────────────────────────


def _flavor_manifests(pm_import) -> dict[str, Path]:
    """flavor 이름 → engine.manifest 경로. 루트 + 등록 하네스 템플릿 전수(하드코딩 0).

    축은 엔진 권위 목록(`HARNESS_TEMPLATE_DIRS`)에서 파생한다 — 네 번째 하네스가 추가되면 이
    매트릭스에 자동 편입된다(손으로 세 번째를 적어 넣던 클래스 회피).
    """
    manifests = {"root": REPO / ".project_manager" / "engine.manifest"}
    for template_dirs in pm_import.HARNESS_TEMPLATE_DIRS.values():
        for dirname in template_dirs:
            manifests[dirname] = (
                REPO / "templates" / dirname / ".project_manager" / "engine.manifest")
    return manifests


def _bare_entry_files(pm_update, repo_files, manifest_path: Path) -> list[tuple[str, Path]]:
    """(dest relpath, source 절대경로) — bare 등재(=`@render` 아님) 항목이 나르는 파일 전수.

    디렉토리 항목은 canonical seam(`repo_owned_files`)으로 펼친다 — 이 판정의 대상은 "채택자가
    실제로 받는 repo 추적 파일" 이라 출하 인벤토리와 같은 채널이어야 한다(자체 tree-walk 신설 0).
    source 부재(`@target-owned` 등)는 빈 목록이라 자연 제외된다.

    ⚠ **flavor 축이 가르는 것은 경로 멤버십뿐이다** — 어느 flavor manifest 를 읽느냐가 "그 flavor 가
    무엇을 bare 로 등재하는가"를 정할 뿐, 내용은 항상 **REPO(canonical worktree) 기준 bytes** 를 읽는다
    (bare 항목의 source 는 루트 파일이거나 `@source=templates/<harness>/…` 의 그 템플릿 파일이다).
    각 `templates/<flavor>/` **사본**의 bytes 를 flavor 별로 대조하는 게 아니다 — 그 축(사본 ↔ canonical
    byte-parity)은 `tests/test_manifest_template_parity.py` 소관이다.
    """
    out: list[tuple[str, Path]] = []
    for entry in pm_update.read_manifest(manifest_path):
        if entry.render:
            continue  # @render = 매 sync 재렌더 — 토큰-form 소스가 정상(진동 축 아님)
        dest_rel = str(entry).replace("\\", "/")
        source_rel = (entry.source_rel or str(entry)).replace("\\", "/")
        for shipped in repo_files.list_repo_owned_files(
                REPO, source_rel, mode=repo_files.TRACKED_ONLY):
            shipped_posix = shipped.as_posix()
            if shipped_posix == source_rel:
                rel = dest_rel
            else:  # 디렉토리 항목 — source 아래 상대경로를 dest 에 그대로 잇는다.
                rel = f"{dest_rel}/{shipped_posix[len(source_rel) + 1:]}"
            out.append((rel, REPO / shipped))
    return out


def _operational_tokens(pm_import) -> tuple[str, ...]:
    """치환 지도(`_substitution_map`)의 키 전수 — 스캔 대상 토큰의 단일 진실."""
    return tuple(_substitution_map(pm_import))


def _substitution_map(pm_import) -> dict[str, str]:
    return pm_import._substitution_map(PROJECT_NAME, REPO, "2026-01-01")


def _ownership_violation(pm_import, dest_rel: str, token: str, sed_exclude) -> str | None:
    """bare 등재 파일의 이 토큰이 소유자 없이 떠 있으면 사유, 정상이면 None.

    소유자가 있으면(③ 설치가 안 건드림 / ② 소비 시점 선언) 채택자 디스크와 upstream 이 같은
    bytes 라 byte-copy 가 no-op 이다. 둘 다 아니면 설치가 값으로 굳히고 다음 sync 가 되돌린다.
    """
    rel = Path(dest_rel)
    if not pm_import._should_substitute(rel, sed_exclude):
        return None  # ③ 엔진 소유 상시 토큰(엔진 소스·manifest·방법론 문서) — 설치가 안 건드림
    if token in pm_import._consumption_time_tokens(rel):
        return None  # ② 소비 시점 소유 — 설치가 그 토큰만 비켜간다
    return (
        f"{dest_rel} 의 {token} 이 소유자 없이 떠 있다 — 설치(pm_import)가 값으로 굳히는데 "
        f"manifest bare 등재라 pm_update byte-copy 가 토큰-form 으로 되돌린다(매 sync 진동). "
        f"토큰을 제거(중립 문구)하거나 pm_import.CONSUMPTION_TIME_TOKENS 에 소비 시점 소유를 "
        f"선언하라."
    )


# ── (A) 전 flavor 매트릭스 ───────────────────────────────────────────────────


def test_bare_registered_tokens_have_a_single_owner(pm_import, pm_update, repo_files):
    """전 flavor manifest 의 bare 등재 파일 전수 × 운영 토큰 전수 — 소유자 없는 토큰 0.

    ticket T-0578 이 닫는 진동 클래스의 본체 가드다. 새 파일이 bare 로 등재되며 토큰을 담으면
    (또는 기존 파일에 토큰이 새로 들어오면) 여기서 red 가 난다.
    """
    tokens = _operational_tokens(pm_import)
    violations: list[str] = []
    scanned = 0
    for flavor, manifest_path in _flavor_manifests(pm_import).items():
        assert manifest_path.is_file(), f"{flavor} manifest 부재: {manifest_path}"
        sed_exclude = pm_import._derive_sed_exclude_relpaths(manifest_path)
        for dest_rel, source_path in _bare_entry_files(pm_update, repo_files, manifest_path):
            try:
                text = source_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 바이너리·읽기불가 = 치환 대상 아님(진동 축 밖)
            scanned += 1
            for token in tokens:
                if token not in text:
                    continue
                why = _ownership_violation(pm_import, dest_rel, token, sed_exclude)
                if why:
                    violations.append(f"[{flavor}] {why}")
    assert scanned > 0, "bare 등재 파일을 하나도 못 읽었다 — 인벤토리 배선이 깨졌다(공허 가드)."
    assert violations == [], "\n".join(violations)


@pytest.mark.parametrize("dest_rel,token,owned", [
    # ③ 엔진 소유 상시 토큰 — 설치가 안 건드리므로 byte-copy 와 진동하지 않는다.
    (".project_manager/tools/board.py", "{{PROJECT_NAME}}", True),
    (".project_manager/engine.manifest", "{{PROJECT_NAME}}", True),
    # ② 소비 시점 소유 — **그 토큰만** 비켜간다(파일 통째 면제가 아니다).
    (".project_manager/wiki/pm_state.template.md", "{{DATE}}", True),
    (".project_manager/wiki/pm_state.template.md", "{{PROJECT_NAME}}", False),
    (".project_manager/wiki/domain/_template.md", "{{DATE}}", True),
    # 소유자 없음 — 설치가 굳히고 sync 가 되돌리는 진동 형상(이 가드가 잡아야 하는 것).
    (".claude/some_adapter_hook.sh", "{{PY}}", False),
    (".project_manager/wiki/README.md", "{{PROJECT_NAME}}", False),
])
def test_ownership_verdict_is_per_file_and_per_token(pm_import, dest_rel, token, owned):
    """소유 판정 자체의 sensitivity — 파일 단위가 아니라 (파일 × 토큰) 단위로 갈린다.

    (A) 가 green 인 이유가 "판정이 아무것도 안 잡아서" 가 아님을 합성 입력으로 못박는다.
    """
    verdict = _ownership_violation(pm_import, dest_rel, token, frozenset())
    if owned:
        assert verdict is None, f"{dest_rel} 의 {token} 이 소유자 없음으로 오판됐다: {verdict}"
    else:
        assert verdict is not None, (
            f"{dest_rel} 의 {token} 이 소유자 없이 떠 있는데 판정이 통과시켰다 — 가드 무력화.")


# ── (B) 선언 원장 ↔ 엔진 선언 일치 · 소비처 실재 ────────────────────────────


def test_engine_declaration_matches_ledger(pm_import):
    """엔진 선언과 이 파일의 allow 원장이 정확히 일치한다 — 선언이 조용히 늘 수 없다."""
    ledger = {rel: frozenset(tokens) for rel, tokens in CONSUMPTION_TIME_OWNERS.items()}
    engine = {rel: frozenset(tokens)
              for rel, tokens in pm_import.CONSUMPTION_TIME_TOKENS.items()}
    assert engine == ledger, (
        "pm_import.CONSUMPTION_TIME_TOKENS 와 이 파일의 CONSUMPTION_TIME_OWNERS 가 어긋난다 — "
        "소비 시점 소유를 새로 선언했다면 소비처와 함께 원장에도 적어라(선언만 늘면 진동이 "
        "다시 조용해진다).")


def _module_functions(module: str) -> dict[str, str] | None:
    """`tools/<module>.py` 의 함수명 → 소스 조각 (모듈 부재면 None)."""
    path = TOOLS / f"{module}.py"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.get_source_segment(text, node) or ""
    return out


def _consumer_source(module: str, symbol: str) -> str | None:
    """선언된 소비처의 소스 + **같은 모듈 직접 호출 1홉** 소스 (심볼 부재면 None).

    치환 한 줄이 진입 함수에 있는 경우(`board.cmd_init`)와 진입 함수가 부르는 렌더 헬퍼에 있는
    경우(`worktree_pool.ensure_task_pm_state` → `_render_initial_task_pm_state`)를 같은 규칙으로
    본다. 1홉으로 좁혀 "모듈 어딘가에 토큰이 있으면 통과" 하는 무른 판정이 되지 않게 한다.
    """
    functions = _module_functions(module)
    if functions is None or symbol not in functions:
        return None
    source = functions[symbol]
    callees = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return "\n".join([source, *(functions[name] for name in sorted(callees)
                                if name in functions)])


@pytest.mark.parametrize("rel,token,consumer", [
    (rel, token, consumer)
    for rel, tokens in CONSUMPTION_TIME_OWNERS.items()
    for token, consumers in tokens.items()
    for consumer in consumers
])
def test_declared_consumer_exists_and_handles_the_token(pm_import, rel, token, consumer):
    """선언된 소비처가 실재하고 그 토큰을 실제로 다룬다 — dangling·no-op 선언 금지.

    소비처가 사라지거나 치환 줄이 지워지면 그 토큰은 아무도 안 채우는데 설치도 비켜간 채로
    채택자 산출물에 리터럴로 남는다(조용한 degrade). 그 자리에서 red 를 낸다.
    """
    if consumer == SCAFFOLD_COPY:
        assert pm_import._is_template_scaffold(Path(rel).name), (
            f"{rel} 은 템플릿 스캐폴드 관례가 아닌데 '사람이 복사할 때 채운다'로 선언됐다 — "
            "실 소비처를 적어라.")
        return
    module, _, symbol = consumer.rpartition(".")
    source = _consumer_source(module, symbol)
    assert source is not None, (
        f"선언된 소비처 {consumer} 가 실재하지 않는다 — dangling 선언(소비처가 사라졌으면 "
        f"{rel} 의 {token} 소유를 다시 정하라).")
    assert token in source, (
        f"{consumer} 가 {token} 을 다루지 않는다 — 선언만 남고 치환은 사라졌다(no-op 소유).")


# ── (C) 신규 설치 직후 토큰 잔존 · 같은 파일의 다른 토큰 무영향 ─────────────


@pytest.fixture(scope="module")
def imported(pm_import, tmp_path_factory):
    """fresh `--new` claude 채택자 (모듈 1회 — import 는 비싸다)."""
    dest = tmp_path_factory.mktemp("adopter") / "instance"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pm_import.main(["--new", str(dest), "--harness", "claude",
                             "--name", PROJECT_NAME, "--fill", "manual"])
    assert rc == 0, f"import 실패(rc={rc}):\n{buf.getvalue()[-2000:]}"
    return dest


@pytest.mark.parametrize("rel", sorted(CONSUMPTION_TIME_OWNERS))
def test_fresh_install_keeps_consumption_time_token(imported, rel):
    """설치 직후 소비 시점 토큰이 **토큰-form 으로 남는다**(설치일로 굳지 않는다).

    굳으면 다음 `pm_update` byte-copy 가 토큰-form 을 되돌려 매 sync 진동한다(제보 실측).
    """
    path = imported / rel
    assert path.is_file(), f"채택자 트리에 {rel} 부재"
    text = path.read_text(encoding="utf-8")
    for token in CONSUMPTION_TIME_OWNERS[rel]:
        assert token in text, (
            f"{rel} 의 {token} 이 설치 시점에 굳었다 — 소비 시점 소유가 배선되지 않았다.")


def test_readme_has_no_operational_token(imported, pm_import):
    """`wiki/README.md` 는 토큰 자체를 안 담는다 — 치환 주체를 없앤 근본 해소(중립 문구).

    이 파일은 `pm_update` 가 byte-copy 로 덮는 엔진 소유 문서라, `@render` 로 바꾸면 동기마다
    채택자 local.conf 로 재치환하는 새 채널이 필요하다. 토큰 제거가 그 채널 자체를 없앤다.
    """
    for path in (REPO / ".project_manager" / "wiki" / "README.md", imported / ".project_manager" / "wiki" / "README.md"):
        text = path.read_text(encoding="utf-8")
        leaked = [t for t in _operational_tokens(pm_import) if t in text]
        assert leaked == [], f"{path} 에 운영 토큰 잔존: {leaked}"


def test_other_tokens_in_same_file_still_substituted(pm_import, tmp_path):
    """소유 선언은 **그 토큰만** 끈다 — 같은 파일의 다른 운영 토큰은 종전대로 설치 시 치환."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_state.template.md")
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("created: {{DATE}}\nowner: {{PROJECT_NAME}}\n", encoding="utf-8")

    changed = pm_import.substitute_placeholders(
        dest, {"{{DATE}}": "2026-01-01", "{{PROJECT_NAME}}": PROJECT_NAME}, {rel})

    assert changed == 1
    assert target.read_text(encoding="utf-8") == (
        f"created: {{{{DATE}}}}\nowner: {PROJECT_NAME}\n"), (
        "소유 선언이 파일 통째 치환-제외로 번졌다(다른 토큰까지 미치환) — 선언은 토큰 단위다.")


# ── (D) 소비 시점 치환이 실제로 돈다 ────────────────────────────────────────


def test_ensure_task_pm_state_renders_creation_date(tmp_path):
    """`worktree_pool.ensure_task_pm_state` 가 만드는 task pm_state 의 날짜 = 생성 시각."""
    proj = tmp_path / "proj"
    wiki = proj / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / ".project_manager" / "wiki" / "pm_state.template.md",
                    wiki / "pm_state.template.md")
    local = proj / ".project_manager" / ".local"
    local.mkdir(parents=True, exist_ok=True)

    wp = _load("worktree_pool", alias="wp_for_token_ownership_test")
    wp.REPO = proj
    wp.LOCAL_DIR = local
    wp.TASKS_DIR = local / "tasks"

    state = wp.ensure_task_pm_state("tokenjob")
    text = state.read_text(encoding="utf-8")

    assert "{{DATE}}" not in text, (
        "task pm_state 에 미해소 토큰 잔존 — 소비 시점 치환이 안 돌았다.")
    assert f"created: {datetime.date.today().isoformat()}" in text, (
        "task pm_state 의 created 가 생성 시각이 아니다(설치일 박제 재발?).")


def test_board_init_seeds_pm_state_with_creation_date(board, monkeypatch, tmp_path):
    """`board.cmd_init` 이 만드는 per-clone `pm_state.md` 의 날짜 = 생성 시각.

    템플릿은 채택자 디스크에 토큰-form 으로 남으므로(위 C), 그 템플릿으로 산출물을 만드는 이
    지점이 날짜를 채워야 한다 — 안 채우면 채택자 pm_state.md 에 `created: {{DATE}}` 가 박힌다.
    """
    state_file = tmp_path / "pm_state.md"
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / "local.conf")
    monkeypatch.setattr(board, "PM_STATE_FILE", state_file)
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE",
                        REPO / ".project_manager" / "wiki" / "pm_state.template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)
    # init 은 areas repo 행을 **항상** 등록하므로(T-0779) REPO 도 tmp 로 묶어야 hermetic 하다 —
    # 안 묶으면 `areas_file()`·`board_lock()` 이 실 저장소 루트를 잡는다.
    _pm = tmp_path / "proj" / ".project_manager"
    (_pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "REPO", tmp_path / "proj")
    monkeypatch.setattr(board, "AREAS_FILE", _pm / "areas.md")
    monkeypatch.setattr(board, "LOCAL_DIR", _pm / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK", _pm / ".local" / "board.lock")
    monkeypatch.setattr(board, "LEASES_FILE", _pm / ".local" / "worktree-leases.json")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board.cmd_init(args)

    assert rc == 0, buf.getvalue()[-2000:]
    text = state_file.read_text(encoding="utf-8")
    assert "{{DATE}}" not in text, (
        "pm_state.md 에 미해소 토큰 잔존 — 생성 시점 치환이 안 돌았다(채택자 가시 결함).")
    assert f"created: {datetime.date.today().isoformat()}" in text


# ── (E) e2e — import → pm_update 왕복 진동 0 ────────────────────────────────


_ROUNDTRIP_WATCHED = (
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/pm_state.template.md",
    ".project_manager/wiki/domain/_template.md",
    ".claude/ctx_stop_hook.sh",
    ".claude/ctx_statusline.sh",
)


def _build_claude_framework(tmp_path: Path) -> Path:
    """REPO 로부터 claude 프레임워크 사본을 만든다 (import 소스 + self-update 소스 겸용).

    claude flavor manifest 가 참조하는 root-상대 경로 전부를 담는다: 엔진(`.project_manager/`)·
    claude 템플릿(`templates/claude_code/` — `@source` 어댑터/훅 canonical)·root `.claude/`
    (agents·skills)·`.gitattributes`·`.github/workflows`. REPO 를 손대지 않게
    사본을 쓴다.
    """
    framework = tmp_path / "framework"
    ignore = shutil.ignore_patterns("__pycache__", ".git", "node_modules")
    shutil.copytree(REPO / ".project_manager", framework / ".project_manager", ignore=ignore)
    shutil.copytree(REPO / "templates" / "claude_code",
                    framework / "templates" / "claude_code", ignore=ignore)
    shutil.copytree(REPO / ".claude", framework / ".claude", ignore=ignore)
    shutil.copytree(REPO / ".github", framework / ".github", ignore=ignore)
    shutil.copy2(REPO / ".gitattributes", framework / ".gitattributes")
    return framework


def test_import_then_update_roundtrip_does_not_oscillate(
        pm_import, pm_update, tmp_path, monkeypatch):
    """샌드박스 재현 시나리오 박제 — import 직후 bytes 가 `pm_update` 왕복 후에도 그대로다.

    제보 실측(v1.6.0)의 재현 절차 그대로다: fresh import → self-update → 대상 문서 diff.
    옛 형상에서는 `wiki/README.md`(`{{PROJECT_NAME}}`)·템플릿 2종(`{{DATE}}`)·claude ctx 래퍼
    (`{{PY}}` 주석)가 전부 토큰-form 으로 회귀했다.

    비공허 증명은 아래 (2) — 같은 경로를 일부러 값-form 으로 되돌려 두면 `pm_update` 가 upstream
    bytes 로 덮는다(=byte-copy 채널이 이 경로에 살아 있다). 그래서 (1) 의 불변은 "채널이 죽어서"
    가 아니다.
    """
    framework = _build_claude_framework(tmp_path)
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    dest = tmp_path / "adopter"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", PROJECT_NAME,
                             "--fill", "manual", "--from", str(framework)])
    assert rc == 0, f"import 실패(rc={rc}):\n{buf.getvalue()[-2000:]}"

    monkeypatch.setattr(pm_update, "REPO", dest)
    before = {rel: (dest / rel).read_bytes() for rel in _ROUNDTRIP_WATCHED}
    for rel, payload in before.items():
        assert payload, f"{rel} 이 비었다(공허 스냅샷)"

    # (1) 왕복 불변 — 진동 0.
    with redirect_stdout(io.StringIO()):
        rc = pm_update.main(["--from", str(framework)])
    assert rc == 0, "self-update 가 rc0 아님"
    oscillated = sorted(rel for rel in _ROUNDTRIP_WATCHED
                        if (dest / rel).read_bytes() != before[rel])
    assert oscillated == [], (
        f"import → pm_update 왕복에서 내용이 바뀐 경로: {oscillated} — 설치 시 치환과 manifest "
        f"byte-copy 가 같은 파일을 반대 방향으로 소유한다(진동 재발).")

    # (2) 비공허 — 값-form 으로 되돌려 두면 다음 sync 가 upstream bytes 로 덮는다(채널 생존).
    #     **두 소유 종류를 각각** 확인한다: 토큰 제거 문서(README·설치 치환 대상)와 소비-시점 소유
    #     템플릿(pm_state.template.md·설치가 비켜가는 파일). 후자를 빼면 "선언 덕에 안 바뀐 것"과
    #     "애초에 byte-copy 대상이 아니라 안 바뀐 것"을 구분할 수 없다.
    canaries = (".project_manager/wiki/README.md",
                ".project_manager/wiki/pm_state.template.md")
    for rel in canaries:
        (dest / rel).write_text("OSCILLATION-CANARY\n", encoding="utf-8")
    with redirect_stdout(io.StringIO()):
        rc = pm_update.main(["--from", str(framework)])
    assert rc == 0
    for rel in canaries:
        assert (dest / rel).read_bytes() == before[rel], (
            f"pm_update 가 {rel} 을 덮지 않았다 — (1) 의 불변이 '채널이 죽어서' 인지 "
            f"구분할 수 없다(공허 가드).")
