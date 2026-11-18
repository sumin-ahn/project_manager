"""T-0674 — opencode 두 진입 채널과 pm_update 생성 계약.

**판정 층(T-0708)**: 생성 산출물과 기대 렌더를 모두 LF 정규화 bytes로 만들어 비교하는 **내용
동일성**이다. 여기에 더해 pm_update 생성 산출물 자체가 LF 표기임을 별도로 단언한다(생성 층은
source 표기와 무관하게 LF bytes를 기록한다).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths
from _skill_command import expected_command_bytes, normalized_bytes
from _textio import dominant_newline_bytes, renotated, write_crlf, write_lf
from pm_engine.infra import manifest_store
from pm_engine.app import update_plan
from pm_engine.app import update_render
from pm_engine.infra import install_conf
from pm_engine.infra import pm_paths
from pm_engine.cli import update_cmd

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "templates" / "opencode" / ".project_manager" / "engine.manifest"
# AGENTS.md 는 harness-neutral 공통 코어(ADR-0069·codex 와 byte-parity)라 하네스 고유 경로를
# 담지 않는다 — 두 쪽 서술은 opencode 전용 채널인 pm-instructions.md 와 진입 문서가 진다.
DOCS = (
    REPO / "templates" / "opencode" / ".opencode" / "pm-instructions.md",
    REPO / "templates" / "opencode" / "AGENTS.lite.md",
    REPO / "templates" / "opencode" / "README.md",
)
# 훅 core 가 사는 자리 — 출하 템플릿.
HOOK_CORE_DIRS = (
    REPO / "templates" / "opencode" / ".opencode" / "lib",
)
# 엔진 루트를 자기 위치에서 내야 하는 core (판정·주입·감시 훅). 나머지 core(warning-channel)는 엔진
# 루트를 쓰지 않는다.
SELF_LOCATED_CORES = (
    "templates/opencode/.opencode/lib/git-check-core.cjs",
    "templates/opencode/.opencode/lib/principle-recall-core.cjs",
    "templates/opencode/.opencode/lib/delegate-channel-core.cjs",
    "templates/opencode/.opencode/lib/ctx-guard-core.cjs",
)
ENGINE_ROOT_DECLARATION = 'const ENGINE_ROOT = path.resolve(__dirname, "..", "..");'
# 엔진 경로를 조립하는 base 식별자 — 두 번째 인자가 ".project_manager" 리터럴이거나 모듈 상수
# (이름이 `_REL` 로 끝나는 것)인 `path.join`/`path.resolve` 호출의 첫 인자를 뽑는다. 반복문 모양이
# 아니라 "경로의 뿌리가 무엇인가"를 보므로, 조상 탐색을 다른 문법으로 다시 써도 base 가 늘어나 걸린다.
# 엔진 경로를 조립하는 호출의 **뿌리 식별자**를 뽑는다. 뿌리는 `path.resolve(...)` 로 한 번
# 감싸여 있을 수 있다(`path.join(path.resolve(root), MARKER_DIR_REL, ...)`) — 그 껍질을 벗기지
# 않으면 그 파일이 검사에서 조용히 빠진다.
ENGINE_PATH_BASE_RE = re.compile(
    r"path\.(?:join|resolve)\(\s*"
    r"(?:path\.resolve\(\s*)?"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\)?\s*,\s*"
    r"(?:\"\.project_manager\"|[A-Za-z_$][A-Za-z0-9_$]*_REL\b)"
)
# 허용되는 뿌리는 둘뿐이다 — 파일 자기 위치 상수 ENGINE_ROOT 와 순수함수가 인자로 받은 root.
ALLOWED_ENGINE_PATH_BASES = frozenset(("ENGINE_ROOT", "root"))

def _local_conf_of(pm_update, root):
    """dest 의 local.conf 값 — 계획·렌더가 받는 conf (디스크 읽기는 호출 경계 한 번)."""
    return install_conf._read_local_conf(Path(root) / ".project_manager" / "local.conf")


def _load_pm_update():
    path = REPO / ".project_manager" / "tools" / "pm_update.py"
    spec = importlib.util.spec_from_file_location("t0674_pm_update", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _command_entries(pm_update):
    return [
        entry for entry in manifest_store.read_manifest(MANIFEST)
        if str(entry).startswith(".opencode/command/")
    ]


def _expected_command(canonical: Path, name: str) -> bytes:
    """기대 command 내용 (LF 정규화 bytes) — 실측값과 같은 층에서 만든다."""
    return expected_command_bytes(canonical, name)


def _expected_command_bytes_on_disk(canonical: Path, name: str) -> bytes:
    """디스크에 있어야 할 기대 bytes — 내용은 렌더, 표기는 **source 표기**(신규 사본 규칙)."""
    return renotated(
        _expected_command(canonical, name),
        dominant_newline_bytes(canonical.read_bytes()),
    )


def test_manifest_maps_every_command_to_root_canonical_skill():
    pm_update = _load_pm_update()
    entries = _command_entries(pm_update)
    skills = sorted(p.parent.name for p in (REPO / ".claude/skills").glob("*/SKILL.md"))
    assert len(entries) == len(skills) == 15
    actual = {str(entry): (entry.render, entry.source_rel) for entry in entries}
    expected = {
        f".opencode/command/{name}.md": (True, f".claude/skills/{name}/SKILL.md")
        for name in skills
    }
    assert actual == expected
    assert sum(source.startswith(".claude/skills/") for _render, source in actual.values()) == 15


def test_pm_update_plan_generates_and_updates_flat_command_copies(tmp_path):
    pm_update = _load_pm_update()
    entries = _command_entries(pm_update)
    changes, missing = update_plan.plan(pm_paths.home_at(pm_update.REPO), REPO, entries, dest_root=tmp_path, render_enabled=False, conf=_local_conf_of(pm_update, tmp_path))
    assert missing == []
    assert len(changes) == 15 and {kind for _rel, _src, _dst, kind in changes} == {"new"}
    update_render.apply(pm_paths.home_at(pm_update.REPO), changes)
    generated = tmp_path / ".opencode/command/pm-bootstrap.md"
    canonical = REPO / ".claude/skills/pm-bootstrap/SKILL.md"
    assert normalized_bytes(generated) == _expected_command(canonical, "pm-bootstrap")
    delegate = REPO / ".claude/skills/pm-dev-delegate/SKILL.md"
    delegated = tmp_path / ".opencode/command/pm-dev-delegate.md"
    assert normalized_bytes(delegated) == _expected_command(delegate, "pm-dev-delegate")
    # 생성 층 자체의 표기 단언 — pm_update는 **신규 사본을 source 표기로** 쓴다(T-0709
    # `_write_rendered_text`: dest 표기 보존·신규는 source 표기 = byte-copy 와 같음). "항상 LF"로
    # 손으로 적은 기대는 CRLF 체크아웃(`core.autocrlf=true`)에서만 red였다(T-0724 실측). 기대
    # bytes를 source 표기로 되돌려 byte-exact로 본다 — 내용이든 표기든 한 글자만 달라도 red다.
    assert generated.read_bytes() == _expected_command_bytes_on_disk(
        canonical, "pm-bootstrap")
    assert delegated.read_bytes() == _expected_command_bytes_on_disk(
        delegate, "pm-dev-delegate")

    generated.write_text("stale\n", encoding="utf-8")
    changes, missing = update_plan.plan(pm_paths.home_at(pm_update.REPO), REPO, entries, dest_root=tmp_path, render_enabled=False, conf=_local_conf_of(pm_update, tmp_path))
    assert missing == []
    assert [(rel, kind) for rel, _src, _dst, kind in changes] == [
        (".opencode/command/pm-bootstrap.md", "update")
    ]


def _plan_one_command(pm_update, monkeypatch, source_root: Path, manifest: Path,
                      dest_root: Path):
    """tmp source(비-git)로 command 사본 1건을 계획한다 — 수준 낮춤 warning 은 여기 관심사가 아니다.

    "source 가 git checkout 이 아니다"는 이 헬퍼의 명시 전제다 — 픽스처 위치가 그 답을 정하지
    않도록 엔진의 runner 주입 교체 지점으로 비-repo(rc 128)를 명시한다.
    """
    repo_files = update_cmd.repo_files
    monkeypatch.setattr(
        repo_files, "_real_git_runner", lambda _cwd: lambda _argv: (128, ""))
    fallback_warning = repo_files.RepoFilesFallbackWarning
    with pytest.warns(fallback_warning, match="filesystem 전수 순회"):
        changes, missing = update_plan.plan(pm_paths.home_at(pm_update.REPO),
            source_root, manifest_store.read_manifest(manifest),
            dest_root=dest_root, render_enabled=False, conf=_local_conf_of(pm_update, dest_root))
    assert missing == []
    return changes


def test_generated_command_copy_takes_source_then_dest_notation(tmp_path, monkeypatch):
    """생성 사본의 개행 표기 = 신규는 **source 표기**, 기존 파일은 **그 파일 표기**(T-0709).

    CRLF 체크아웃에서만 성립하던 경우라 LF 개발기에서는 늘 초록이었다 — CRLF source·CRLF 사본을
    픽스처로 만들어 이 경우를 여기서 실제로 태운다([[guard-must-cover-its-own-surface]]). 내용은
    LF 정규화 층의 기대 렌더와 대조하므로 표기가 아니라 내용이 한 글자만 달라져도 red 다.
    """
    pm_update = _load_pm_update()
    source_root = tmp_path / "source"
    skill = source_root / ".claude" / "skills" / "pm-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    body = (
        "---\nname: pm-x\n---\n"
        "본문 [references/operational-details.md](references/operational-details.md)\n"
    )
    write_crlf(skill, body)
    manifest = source_root / "engine.manifest"
    write_lf(
        manifest,
        ".opencode/command/pm-x.md @render @source=.claude/skills/pm-x/SKILL.md\n",
    )
    assert b"\r\n" in skill.read_bytes(), "픽스처가 CRLF source 를 만들지 못했다"
    dest_root = tmp_path / "dest"

    changes = _plan_one_command(pm_update, monkeypatch, source_root, manifest, dest_root)
    assert [(rel, kind) for rel, _src, _dst, kind in changes] == [
        (".opencode/command/pm-x.md", "new")
    ]
    update_render.apply(pm_paths.home_at(pm_update.REPO), changes)

    generated = dest_root / ".opencode" / "command" / "pm-x.md"
    expected_lf = _expected_command(skill, "pm-x")
    assert b"\r\n" not in expected_lf, "기대값이 LF 정규화 층이 아니다"
    assert generated.read_bytes() == renotated(expected_lf, b"\r\n"), \
        "신규 사본이 source(CRLF) 표기를 따르지 않았다"

    # 기존 사본이 있으면 그 파일의 표기를 따른다 — 채택자 체크아웃 표기를 뒤집지 않는다.
    write_lf(generated, "stale 사본\n두 줄\n")
    changes = _plan_one_command(pm_update, monkeypatch, source_root, manifest, dest_root)
    assert [(rel, kind) for rel, _src, _dst, kind in changes] == [
        (".opencode/command/pm-x.md", "update")
    ]
    update_render.apply(pm_paths.home_at(pm_update.REPO), changes)
    assert generated.read_bytes() == expected_lf, \
        "기존 사본(LF)의 표기를 보존하지 않았다"


def test_synced_crlf_command_copy_plans_no_change(tmp_path, monkeypatch):
    """표기만 CRLF인 동기 완료 사본을 다시 계획하면 변경 0이다 (내용 무변경 churn 금지).

    결함 모양([[T-0727]]): 계획의 최소 렌더 분기가 raw bytes로 대조했다. 렌더 산출물은 LF인데
    기록은 `_write_rendered_text`가 dest 표기(CRLF)를 보존하므로, CRLF 체크아웃에서는 방금 자기가
    쓴 사본을 다음 계획이 다시 `update`로 올린다. 내용이 상하진 않지만(재기록 후 bytes 동일)
    dry-run이 상시 오탐이고 실제 drift가 그 소음에 묻힌다. `render_enabled=False`(`--target`
    /`--all-targets` 전파)면 `@render` 선언 엔트리도 이 분기로 온다([[T-0724]] 실측).
    """
    pm_update = _load_pm_update()
    source_root = tmp_path / "source"
    skill = source_root / ".claude" / "skills" / "pm-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    body = (
        "---\nname: pm-x\n---\n"
        "본문 [references/operational-details.md](references/operational-details.md)\n"
    )
    write_crlf(skill, body)
    manifest = source_root / "engine.manifest"
    write_lf(
        manifest,
        ".opencode/command/pm-x.md @render @source=.claude/skills/pm-x/SKILL.md\n",
    )
    dest_root = tmp_path / "dest"
    update_render.apply(pm_paths.home_at(pm_update.REPO), _plan_one_command(pm_update, monkeypatch, source_root, manifest, dest_root))
    generated = dest_root / ".opencode" / "command" / "pm-x.md"
    # 주입 선-단언: 사본이 실제로 CRLF고, LF 렌더층과 byte가 갈려 있어야 이 경우가 시험된다.
    assert b"\r\n" in generated.read_bytes(), "픽스처가 CRLF 사본을 만들지 못했다(공허 회귀)"
    assert generated.read_bytes() != _expected_command(skill, "pm-x"), \
        "CRLF 사본이 LF 렌더층과 byte 동일하다 — 이 가드가 시험되지 않는다"

    assert _plan_one_command(pm_update, monkeypatch, source_root, manifest, dest_root) == [], \
        "표기만 CRLF인 무변경 사본을 update로 재계획했다(내용 무변경 churn)"

    # 판정 민감도는 그대로다 — 표기가 CRLF여도 내용이 한 글자 다르면 여전히 update.
    generated.write_bytes(generated.read_bytes() + "한 줄\r\n".encode("utf-8"))
    assert [(rel, kind) for rel, _src, _dst, kind
            in _plan_one_command(pm_update, monkeypatch, source_root, manifest, dest_root)] == [
        (".opencode/command/pm-x.md", "update")
    ], "CRLF 사본의 내용 차이를 놓쳤다(정규화가 판정을 무디게 만듦)"


def test_expected_command_matches_crlf_checkout_source(tmp_path):
    """T-0708 — CRLF 체크아웃 source여도 기대 렌더는 생성 산출물과 같은 층(LF 정규화 bytes)이다.

    pm_update는 source를 텍스트로 읽어 LF bytes로 기록한다(`apply` render/평탄-링크 경로). 기대값을
    source 원본 bytes로 만들면 CRLF 체크아웃에서만 항상 불일치하므로 두 값을 이 층으로 통일한다.
    """
    lf_source = tmp_path / "lf" / "SKILL.md"
    crlf_source = tmp_path / "crlf" / "SKILL.md"
    lf_source.parent.mkdir(parents=True)
    crlf_source.parent.mkdir(parents=True)
    body = "---\nname: pm-x\n---\n본문 (references/operational-details.md)\n"
    write_lf(lf_source, body)
    write_crlf(crlf_source, body)
    assert crlf_source.read_bytes() != lf_source.read_bytes(), "픽스처가 표기 차이를 못 만들었다"
    expected = _expected_command(crlf_source, "pm-x")
    assert expected == _expected_command(lf_source, "pm-x")
    assert b"\r\n" not in expected
    assert b"(../../.claude/skills/pm-x/references/operational-details.md)" in expected


def test_expected_command_bytes_on_disk_tracks_source_notation(tmp_path):
    """디스크 기대값 헬퍼가 source 표기를 실제로 따라간다 — 판정 helper 자신의 가드.

    이 헬퍼가 표기를 늘 LF로 접으면 CRLF 체크아웃에서만 틀린 기대를 만들어, 생성 사본의
    byte-exact 단언이 그 플랫폼에서만 조용히 뒤집힌다. 표기 두 가지와 **내용 1자 drift**를 함께
    못박는다.
    """
    body = "---\nname: pm-x\n---\n본문 (references/operational-details.md)\n"
    sources = {}
    for notation, write in (("lf", write_lf), ("crlf", write_crlf)):
        path = tmp_path / notation / "SKILL.md"
        path.parent.mkdir(parents=True)
        write(path, body)
        sources[notation] = path
    drifted = tmp_path / "drift" / "SKILL.md"
    drifted.parent.mkdir(parents=True)
    write_crlf(drifted, body.replace("본문", "본문 한 글자 다름"))

    expected_lf = _expected_command(sources["lf"], "pm-x")
    assert _expected_command_bytes_on_disk(sources["lf"], "pm-x") == expected_lf
    assert _expected_command_bytes_on_disk(sources["crlf"], "pm-x") == \
        renotated(expected_lf, b"\r\n")
    assert dominant_newline_bytes(sources["crlf"].read_bytes()) == b"\r\n"
    assert dominant_newline_bytes(sources["lf"].read_bytes()) == b"\n"
    # 표기가 같아도 내용이 다르면 기대값이 갈린다(정규화가 drift를 삼키지 않는다).
    assert _expected_command_bytes_on_disk(drifted, "pm-x") != \
        _expected_command_bytes_on_disk(sources["crlf"], "pm-x")


def test_entry_docs_describe_both_distinct_surfaces():
    stale = ("단일 소비", "채널 은퇴", "slash command를 뜻하지 않")
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert ".claude/skills/" in text and ".opencode/command" in text, path
        assert not any(phrase in text for phrase in stale), (path, stale)


def test_opencode_hook_cores_have_no_ancestor_engine_root_search():
    """훅 core 는 엔진 루트를 조상에서 찾지 않고 자기 설치 위치에서 낸다.

    조상 탐색은 중첩 트리(PM 홈 안 worktree 슬롯)에서 바깥 프로젝트의 엔진을 실행한다.
    """
    scanned = []
    for core_dir in HOOK_CORE_DIRS:
        assert core_dir.is_dir(), f"훅 core 디렉터리 없음: {core_dir}"
        for core in sorted(core_dir.glob("*.cjs")):
            source = core.read_text(encoding="utf-8")
            scanned.append(core)
            assert "findEngineRoot" not in source, f"조상 탐색 함수 잔존: {core}"
            assert not re.search(r"for \(let i = 0; i < 12", source), (
                f"12단 상향 반복문 잔존: {core}"
            )
            assert "parent === dir" not in source, f"조상 훑기 관용구 잔존: {core}"
    assert len(scanned) >= len(SELF_LOCATED_CORES), f"스캔 대상이 사라짐: {scanned}"

    for relative in SELF_LOCATED_CORES:
        core = REPO / relative
        source = core.read_text(encoding="utf-8")
        assert ENGINE_ROOT_DECLARATION in source, f"자기 위치 엔진 루트 선언 없음: {core}"
        bases = set(ENGINE_PATH_BASE_RE.findall(source))
        # 추출 0건은 통과가 아니라 **무구속**이다 — 그 파일은 이 단언이 놓치는 곳에 있다.
        # 4파일 전부가 엔진 경로를 실제로 조립하므로 공집합이면 추출기가 그 문법을 놓친 것이다.
        assert bases, (
            f"엔진 경로 base 추출 0건 — 이 파일이 검사에서 빠졌다: {core}. "
            "ENGINE_PATH_BASE_RE 가 이 파일의 경로 조립 문법을 못 읽는다."
        )
        assert bases <= ALLOWED_ENGINE_PATH_BASES, (
            f"엔진 경로 base 가 자기 위치·인자 밖으로 늘어남: {core} · "
            f"{sorted(bases - ALLOWED_ENGINE_PATH_BASES)}"
        )


# opencode 플러그인 코드가 사는 자리 — 출하 템플릿.
OPENCODE_ADAPTER_DIRS = ("templates/opencode/.opencode",)
SYNC_RUNS = frozenset({"spawnSync", "execFileSync", "execSync"})
_JS_TOKEN = re.compile(r"""\s+ | //[^\n]* | /\*.*?\*/
  | (?P<tok>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\.\.\.|.)""", re.S | re.X)
_JS_REGEX = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n\[])+/[a-z]*")
_REGEX_AFTER = frozenset("(,=:[!&|?{};") | {"", "return", "typeof"}
_OPEN, _CLOSE = frozenset("([{"), frozenset(")]}")
_NAME = re.compile(r"[A-Za-z_$][\w$]*")


def _js_tokens(source):
    """주석을 버리고 문자열·템플릿·정규식은 한 덩어리로 둔 토큰 목록."""
    out, i = [], 0
    while i < len(source):
        if source[i] == "/" and source[i + 1:i + 2] not in ("/", "*") and (out[-1] if out else "") in _REGEX_AFTER:
            literal = _JS_REGEX.match(source, i)
            if literal:
                out.append(literal.group())
                i = literal.end()
                continue
        match = _JS_TOKEN.match(source, i)
        if match.group("tok"):
            out.append(match.group("tok"))
        i = match.end()
    return out


def _close(toks, k):
    depth = 0
    for j in range(k, len(toks)):
        depth += (toks[j] in _OPEN) - (toks[j] in _CLOSE)
        if depth == 0:
            return j
    raise AssertionError("괄호 짝이 맞지 않음")


def _items(toks, k):
    """괄호 k 안의 맨 바깥 쉼표로 나눈 구간들."""
    end, spans, depth, start = _close(toks, k), [], 0, k + 1
    for j in range(k + 1, end):
        depth += (toks[j] in _OPEN) - (toks[j] in _CLOSE)
        if toks[j] == "," and depth == 0:
            spans.append((start, j))
            start = j + 1
    return [span for span in spans + [(start, end)] if span[0] < span[1]]


def _option_problems(toks, a, b, option_spots):
    """옵션 인자 하나 — 객체 리터럴(같은 파일 상수면 그 리터럴)에 timeout 키가 있거나 확인할 수 없으면 문제.

    이름으로 넘긴 옵션은 그 이름이 선언(const·let·var 이름 =)과 동기 실행 호출의 옵션 인자 자리(option_spots)
    밖에 한 번이라도 나오면 리터럴이 바뀌었을 수 있어 확인할 수 없음으로 본다.
    """
    if b - a == 1 and _NAME.fullmatch(toks[a]):
        name = toks[a]
        declared = {j + 1 for j in range(len(toks) - 3) if toks[j] in ("const", "let", "var") and toks[j + 1:j + 3] == [name, "="]}
        values = sorted(j + 2 for j in declared)
        elsewhere = any(tok == name and j not in declared and j not in option_spots for j, tok in enumerate(toks))
        if not values or elsewhere or any(toks[v] not in ("{", "[") for v in values):
            return [f"{name}: 같은 파일의 리터럴로 확인할 수 없음"]
        return [problem for v in values for problem in _option_problems(toks, v, _close(toks, v) + 1, option_spots)]
    if toks[a] == "[" and _close(toks, a) == b - 1:
        return []
    if toks[a] != "{" or _close(toks, a) != b - 1:
        return ["옵션을 확인할 수 없음"]
    problems = []
    for s, e in _items(toks, a):
        if toks[s] == "...":
            problems += _option_problems(toks, s + 1, e, option_spots)
        elif toks[s] == "[" or toks[s].strip("'\"") == "timeout":
            problems.append("timeout 키(또는 계산된 키)")
    return problems


def timed_sync_runs(source):
    """(동기 실행 호출 수, 시간 제한 옵션 문제 목록)."""
    toks = _js_tokens(source)
    calls = [k for k, tok in enumerate(toks)
             if tok in SYNC_RUNS and toks[k + 1:k + 2] == ["("] and toks[k - 1:k] != ["function"]]
    options = [(k, a, b) for k in calls for a, b in _items(toks, k + 1)[1:]]
    option_spots = {j for _, a, b in options for j in range(a, b)}
    return len(calls), [f"{toks[k]} 호출: {problem}" for k, a, b in options
                        for problem in _option_problems(toks, a, b, option_spots)]


def test_opencode_plugin_sync_process_runs_take_no_time_limit():
    """opencode 플러그인 코드의 동기 프로세스 실행(spawnSync·execFileSync·execSync 호출)은 시간 제한(timeout)을 받지 않는다.

    opencode 가 싣는 Bun 1.3 의 Windows spawnSync 는 시간 제한을 앞 spawnSync 가 끝난 시각부터 재어, 그 뒤 제한보다 오래
    쉬었으면 자식을 띄우자마자 끝낸다(ETIMEDOUT). 두 자리의 js·cjs·mjs 를 모두 훑어 호출마다 옵션 인자를 읽는다. 옵션은 그
    자리의 객체 리터럴이거나 같은 파일 상수의 리터럴이어야 하고, 그렇게 확인할 수 없는 호출도 실패로 본다.
    """
    found, problems = 0, []
    for base in OPENCODE_ADAPTER_DIRS:
        for path in sorted(repo_owned_paths(REPO, base, mode=OWNED)):
            if path.suffix not in (".js", ".cjs", ".mjs"):
                continue
            count, issues = timed_sync_runs(path.read_text(encoding="utf-8"))
            found += count
            problems += [f"{path.relative_to(REPO)}: {issue}" for issue in issues]
    assert found, "동기 프로세스 실행을 하나도 찾지 못함 — 훑기가 코드 문법을 놓쳤다"
    assert not problems, problems


def test_timed_sync_runs_rejects_option_name_changed_after_literal():
    """이름으로 넘긴 옵션은 선언 리터럴 뒤에 그 이름이 다른 자리에 나오면(바꿨을 수 있음) 확인할 수 없음으로 본다."""
    declared = 'const o = {encoding: "utf8"};\n'
    call = 'spawnSync("python", [], o);\n'
    changed = {
        "점 대입": declared + "o.timeout = 5;\n" + call,
        "대괄호 대입": declared + 'o["timeout"] = 5;\n' + call,
        "Object.assign": declared + "Object.assign(o, {timeout: 5});\n" + call,
        "재대입": 'let o = {encoding: "utf8"};\no = {timeout: 5};\n' + call,
        "매개변수 가림": declared + "function run(o) {\n  " + call + "}\n",
    }
    for form, source in changed.items():
        assert timed_sync_runs(source)[1], form
    assert timed_sync_runs(declared + call + call) == (2, [])
