"""T-0908 — URL upstream 을 엔진이 직접 캐시 clone/fetch 한다 (스킬 셸 절차 폐지).

`pm-config update`(=`pm_update.py`)는 `--from` 없이도 URL upstream 을 소스로 쓴다. URL 을 로컬
트리로 바꾸는 코드는 엔진 한 자리(`pm_import.resolve_upstream_url_cache`)뿐이고, 캐시는 dest PM
홈 안 `.project_manager/.local/upstream-cache/<URL 해시>/`(git-ignored)에 산다. 같은 자리를
`add-harness`·`sync-adapter-config` 의 소스 해소도 부른다.

여기 픽스처는 **로컬 bare + `file://`** 만 쓴다(네트워크 clone 0). git 을 흉내 내는 fake runner
로는 "실제 clone/fetch 가 무엇을 만들었나" 를 잴 수 없어 실 git 산출물로 판정한다.

이 파일이 고정하는 계약:
  - URL + `--from` 생략 결과 == 같은 rev 의 캐시를 `--from` 으로 준 결과 (파일 diff 0).
  - 두 번째 실행은 clone 이 아니라 fetch 다(캐시 재사용) — 상류 새 커밋이 작업트리까지 온다.
  - clone/fetch 실패는 rc≠0 · dest 파일 변경 0 · `upstream.rev`/`upstream.seen_rev` 미기록
    (옛 캐시로 조용히 진행하지 않는다).
  - 경로 upstream 은 종전과 같고 캐시 디렉터리를 만들지 않는다(골든).
  - pm-update 스킬 카드에 셸 clone 절차·`$GIT` env 6키 복제가 없다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

SENTINEL_REL = ".project_manager/tools/__t0908_sentinel__.py"
CACHE_RELDIR = Path(".project_manager") / ".local" / "upstream-cache"

# 실 git 호출용 identity — 사용자 global config 유무와 무관하게 커밋이 선다.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "PM Test",
    "GIT_AUTHOR_EMAIL": "pm@example.invalid",
    "GIT_COMMITTER_NAME": "PM Test",
    "GIT_COMMITTER_EMAIL": "pm@example.invalid",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


@pytest.fixture(scope="module")
def pm_config():
    return _load("pm_config")


def _git(args: list[str], cwd: Path | None = None, *, binary: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
        text=not binary, encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        env={**os.environ, **_GIT_ENV}, check=False)
    assert result.returncode == 0, f"git {args}: {result.stdout}{result.stderr}"
    return result


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_framework_repo(root: Path, *, sentinel_body: str = "# v1\n") -> Path:
    """상류 프레임워크 checkout — manifest 등재 sentinel + codex 어댑터 템플릿 + 커밋."""
    _write(root / SENTINEL_REL, sentinel_body)
    _write(root / ".project_manager" / "engine.manifest", SENTINEL_REL + "\n")
    _write(root / "templates" / "codex" / ".codex" / "hooks.json", _HOOKS)
    _write(root / "templates" / "codex" / ".codex" / "config.toml", "upstream = true\n")
    _git(["init", "-q", "-b", "main"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "framework seed"], root)
    return root


def _make_bare(source: Path, bare: Path) -> str:
    """`file://` 로 줄 bare 저장소 (네트워크 0) — 반환은 upstream 에 적을 URL."""
    _git(["clone", "--bare", "-q", str(source), str(bare)])
    return bare.as_uri()


def _push_commit(source: Path, bare: Path, body: str, message: str) -> None:
    """상류에 새 커밋을 얹는다 — bare 는 clone 원본이라 remote 이름 없이 경로로 민다."""
    _write(source / SENTINEL_REL, body)
    _git(["add", "-A"], source)
    _git(["commit", "-qm", message], source)
    _git(["push", "-q", str(bare), "main"], source)


def _dest_with_url_upstream(dest: Path, url: str, extra: str = "") -> Path:
    conf = dest / ".project_manager" / "local.conf"
    _write(conf, f"# per-clone\nupstream.path={url}\n{extra}")
    return conf


def _conf(pm_update, dest: Path) -> dict:
    return pm_update._read_local_conf(dest / ".project_manager" / "local.conf")


def _head(repo: Path) -> str:
    return _git(["-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def _cache_root(dest: Path) -> Path:
    return dest / CACHE_RELDIR


def _only_cache(dest: Path) -> Path:
    caches = sorted(p for p in _cache_root(dest).iterdir() if p.is_dir())
    assert len(caches) == 1, f"캐시 디렉터리가 1개가 아님: {caches}"
    return caches[0]


def _snapshot(root: Path) -> dict:
    """dest 트리의 {relpath: bytes} — 캐시(.local)는 빼고 본다(그 자리는 엔진 소유 스크래치)."""
    local = root / ".project_manager" / ".local"
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if local in path.parents:
            continue
        out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


_HOOKS = '{"hooks": {"PreCompact": ["new"]}}\n'


def test_t0933_long_cache_clone_uses_the_real_runner(pm_import, tmp_path, monkeypatch):
    """265자 이상 pack keep 경로의 실제 file URL clone과 HEAD를 검증한다."""
    source = _make_framework_repo(tmp_path / "fw")
    url = _make_bare(source, tmp_path / "fw.git")
    # 작업 파일 개행과 커밋 blob 개행을 분리해 Git 산출물 비교를 검증한다.
    (source / SENTINEL_REL).write_bytes(b"# v1\r\n")
    parent = tmp_path / "long"
    while len(str(parent / "cache-new")) < 196:
        remaining = 196 - len(str(parent / "cache-new"))
        parent /= "x" * min(50, max(1, remaining - 1))
    parent.mkdir(parents=True)
    dest = parent / "cache-new"
    keep_path = dest / ".git" / "objects" / "pack" / ("pack-" + "0" * 40 + ".keep")
    assert len(str(keep_path)) >= 265

    if os.name == "nt":
        old_dest = parent / "cache-old"
        assert len(str(old_dest)) == len(str(dest))
        # 모듈 로컬 대역으로 longpaths 추가 전의 설정 6항목만 전달한다.
        with monkeypatch.context() as before:
            before.setattr(pm_import, "os", SimpleNamespace(
                name="posix", environ=os.environ, devnull=os.devnull))
            old_runner = pm_import._real_upstream_git_runner(
                timeout=pm_import.UPSTREAM_CLONE_TIMEOUT_SECONDS)
            old_rc, old_out = old_runner(["clone", "--", url, str(old_dest)])
        assert old_rc == 128, old_out
        assert "Filename too long" in old_out, old_out

    runner = pm_import._real_upstream_git_runner(
        timeout=pm_import.UPSTREAM_CLONE_TIMEOUT_SECONDS)
    rc, out = runner(["clone", "--", url, str(dest)])
    assert rc == 0, out
    committed = _git(["-C", str(source), "show", f"HEAD:{SENTINEL_REL}"], binary=True).stdout
    assert (dest / SENTINEL_REL).read_bytes() == committed
    assert _head(dest) == _head(source)
    assert list((dest / ".git" / "objects" / "pack").glob("*.pack"))


# ── URL upstream + --from 생략 (AT-001 본류) ─────────────────────────────────

def test_url_upstream_omitted_from_clones_cache_and_syncs(
        pm_update, tmp_path, monkeypatch, capsys):
    """URL upstream 에서 `--from` 없이 sync — 캐시 clone 후 파일 배달 + 두 rev 키 기록."""
    source = _make_framework_repo(tmp_path / "fw")
    url = _make_bare(source, tmp_path / "fw.git")
    dest = tmp_path / "dest"
    _dest_with_url_upstream(dest, url)
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_update.main([]) == 0, capsys.readouterr().err

    assert (dest / SENTINEL_REL).read_text(encoding="utf-8") == "# v1\n", \
        "URL upstream 에서 엔진 파일이 배달되지 않음"
    cache = _only_cache(dest)
    assert (cache / ".git").is_dir(), f"캐시가 git checkout 이 아님: {cache}"
    assert _cache_root(dest) in cache.parents, "캐시가 dest .local/upstream-cache 밖에 생성됨"
    conf = _conf(pm_update, dest)
    assert conf["upstream.rev"] == conf["upstream.seen_rev"] == _head(cache), \
        f"두 rev 키가 캐시 HEAD 로 함께 기록되지 않음: {conf!r}"


def test_url_cache_result_matches_explicit_from(pm_update, tmp_path, monkeypatch, capsys):
    """`--from` 생략(URL→캐시) 결과가 같은 캐시를 `--from` 으로 명시한 결과와 파일 diff 0."""
    source = _make_framework_repo(tmp_path / "fw")
    url = _make_bare(source, tmp_path / "fw.git")

    auto = tmp_path / "auto"
    _dest_with_url_upstream(auto, url)
    monkeypatch.setattr(pm_update, "REPO", auto)
    assert pm_update.main([]) == 0, capsys.readouterr().err
    cache = _only_cache(auto)

    explicit = tmp_path / "explicit"
    _dest_with_url_upstream(explicit, url)
    monkeypatch.setattr(pm_update, "REPO", explicit)
    assert pm_update.main(["--from", str(cache)]) == 0, capsys.readouterr().err

    assert _snapshot(auto) == _snapshot(explicit), \
        "URL 자동 해소와 캐시 명시 --from 의 산출물이 다르다"
    assert not _cache_root(explicit).exists(), \
        "명시 --from 인데 캐시를 또 만들었다(불필요한 네트워크 표면)"


def test_second_run_fetches_into_same_cache(pm_update, tmp_path, monkeypatch, capsys):
    """두 번째 실행은 clone 이 아니라 fetch — 같은 캐시가 살아 있고 상류 새 커밋이 도달한다."""
    source = _make_framework_repo(tmp_path / "fw")
    bare = tmp_path / "fw.git"
    url = _make_bare(source, bare)
    dest = tmp_path / "dest"
    _dest_with_url_upstream(dest, url)
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_update.main([]) == 0, capsys.readouterr().err
    cache = _only_cache(dest)
    marker = cache / ".git" / "T0908_SAME_CACHE"     # 재clone 이면 사라진다.
    marker.write_text("same\n", encoding="utf-8")

    _push_commit(source, bare, "# v2\n", "upstream v2")

    assert pm_update.main([]) == 0, capsys.readouterr().err

    assert marker.is_file(), "두 번째 실행이 캐시를 다시 clone 했다(fetch 재사용 아님)"
    assert (dest / SENTINEL_REL).read_text(encoding="utf-8") == "# v2\n", \
        "fetch 뒤 작업트리가 전진하지 않아 새 상류 파일이 배달되지 않았다"
    conf = _conf(pm_update, dest)
    assert conf["upstream.rev"] == conf["upstream.seen_rev"] == _head(bare), \
        f"fetch 후 두 rev 키가 새 상류 HEAD 로 전진하지 않음: {conf!r}"


# ── 실패는 loud · 무변경 (불변식 3) ──────────────────────────────────────────

def test_clone_failure_is_loud_and_changes_nothing(pm_update, tmp_path, monkeypatch, capsys):
    """도달 불가 URL(존재하지 않는 file:// 경로) — rc≠0 · dest 무변경 · rev 미기록 · 캐시 잔재 0.

    잔재 0 은 git 이 실패한 clone 의 목적지를 스스로 되돌린 결과다 — 엔진은 삭제를 대신하지
    않는다(지울 것이 남으면 다음 clone 이 "빈 디렉터리 아님" 으로 loud 하게 실패한다).
    """
    dest = tmp_path / "dest"
    url = (tmp_path / "absent.git").as_uri()
    _dest_with_url_upstream(dest, url)
    _write(dest / SENTINEL_REL, "# local\n")
    monkeypatch.setattr(pm_update, "REPO", dest)
    before = _snapshot(dest)

    rc = pm_update.main([])

    assert rc != 0, "clone 실패인데 rc 0(조용한 진행)"
    err = capsys.readouterr().err
    assert "캐시 clone 실패" in err, f"실패 사유가 표면화되지 않음: {err!r}"
    assert _snapshot(dest) == before, "clone 실패 실행이 dest 파일을 바꿨다"
    conf = _conf(pm_update, dest)
    assert "upstream.rev" not in conf and "upstream.seen_rev" not in conf, \
        f"clone 실패인데 rev 가 기록됨: {conf!r}"
    leftovers = (sorted(p.name for p in _cache_root(dest).iterdir())
                 if _cache_root(dest).exists() else [])
    assert not leftovers, f"실패한 clone 의 잔재가 캐시 자리에 남았다: {leftovers}"


def test_fetch_failure_does_not_advance_or_reuse_old_cache(
        pm_update, tmp_path, monkeypatch, capsys):
    """캐시가 있어도 fetch 실패면 멈춘다 — 옛 캐시로 진행 금지·rev 미전진·dest 무변경."""
    source = _make_framework_repo(tmp_path / "fw")
    bare = tmp_path / "fw.git"
    url = _make_bare(source, bare)
    dest = tmp_path / "dest"
    _dest_with_url_upstream(dest, url)
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main([]) == 0, capsys.readouterr().err
    first_rev = _conf(pm_update, dest)["upstream.rev"]

    bare.rename(tmp_path / "fw.git.moved")           # 상류 소실 → fetch 실패
    before = _snapshot(dest)

    rc = pm_update.main([])

    assert rc != 0, "fetch 실패인데 옛 캐시로 진행했다(rc 0)"
    assert "캐시 fetch 실패" in capsys.readouterr().err
    assert _snapshot(dest) == before, "fetch 실패 실행이 dest 파일을 바꿨다"
    assert _conf(pm_update, dest)["upstream.rev"] == first_rev, "실패 실행이 baseline 을 전진시켰다"


# ── 조회면(--changes·--count-only)도 같은 캐시 ───────────────────────────────

def test_changes_uses_cache_for_url_upstream(pm_update, tmp_path, monkeypatch, capsys):
    """`--changes` 가 URL upstream 에서 baseline..HEAD 를 요약한다(같은 캐시·rc 0)."""
    source = _make_framework_repo(tmp_path / "fw")
    bare = tmp_path / "fw.git"
    url = _make_bare(source, bare)
    dest = tmp_path / "dest"
    _dest_with_url_upstream(dest, url)
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main([]) == 0, capsys.readouterr().err

    _push_commit(source, bare, "# v2\n", "upstream v2 for changes")
    capsys.readouterr()

    assert pm_update.main(["--changes"]) == 0
    out = capsys.readouterr().out
    assert "upstream v2 for changes" in out or "엔진 영향" in out, \
        f"URL upstream 에서 변경점 요약이 나오지 않음: {out!r}"


def test_count_only_keeps_machine_output_on_url_upstream(
        pm_update, tmp_path, monkeypatch, capsys):
    """`--changes --count-only` stdout 은 값 한 줄이다 — 캐시 진행 표시가 stdout 을 오염하지 않는다."""
    source = _make_framework_repo(tmp_path / "fw")
    bare = tmp_path / "fw.git"
    url = _make_bare(source, bare)
    dest = tmp_path / "dest"
    _dest_with_url_upstream(dest, url)
    monkeypatch.setattr(pm_update, "REPO", dest)
    assert pm_update.main([]) == 0, capsys.readouterr().err

    _push_commit(source, bare, "# v2\n", "one more")
    capsys.readouterr()

    assert pm_update.main(["--changes", "--count-only"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "1", f"기계 판독 stdout 이 한 줄 값이 아님: {captured.out!r}"
    assert "캐시" in captured.err, "캐시 동기 표시가 stderr 로 나가지 않았다"


# ── 경로 upstream 골든 (불변식 1·5) ─────────────────────────────────────────

def test_path_upstream_creates_no_cache(pm_update, tmp_path, monkeypatch, capsys):
    """경로 upstream 은 종전대로 그 checkout 에서 복사한다 — 캐시 디렉터리 0."""
    source = _make_framework_repo(tmp_path / "fw")
    dest = tmp_path / "dest"
    _write(dest / ".project_manager" / "local.conf", f"upstream.path={source}\n")
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_update.main([]) == 0, capsys.readouterr().err

    assert (dest / SENTINEL_REL).is_file()
    assert not _cache_root(dest).exists(), "경로 upstream 인데 캐시를 만들었다"
    conf = _conf(pm_update, dest)
    assert conf["upstream.rev"] == conf["upstream.seen_rev"] == _head(source)


# ── 어댑터 소스 해소 (add-harness · sync-adapter-config) ─────────────────────

def _imported_codex_instance(dest: Path, url: str) -> Path:
    """codex 어댑터가 설치된 imported 인스턴스(templates/ 부재) — 소스는 upstream 뿐이다."""
    _dest_with_url_upstream(dest, url)
    _write(dest / ".codex" / "hooks.json", _HOOKS)
    _write(dest / ".codex" / "config.toml", "upstream = true\n")
    _write(dest / "AGENTS.md", "# adopter\n")
    (dest / ".agents").mkdir(parents=True, exist_ok=True)
    _write(dest / ".project_manager" / "install.json",
           '{"schema": 1, "harnesses": ["codex"]}\n')
    return dest


def test_add_harness_source_resolves_url_upstream_via_cache(pm_import, tmp_path):
    """URL upstream 인 imported 인스턴스의 어댑터 소스가 캐시로 해소된다(`[중단]` 대상 아님)."""
    source = _make_framework_repo(tmp_path / "fw")
    url = _make_bare(source, tmp_path / "fw.git")
    dest = _imported_codex_instance(tmp_path / "inst", url)

    got = pm_import._resolve_add_harness_source(dest, "codex", None)

    assert got == _only_cache(dest), f"캐시가 아닌 다른 소스로 해소됨: {got}"
    assert (got / "templates" / "codex").is_dir()


def test_local_source_wins_over_url_cache(pm_import, tmp_path):
    """dest 가 프레임워크 checkout 이면 URL upstream 이어도 네트워크를 타지 않는다(차단 추가 0)."""
    dest = _make_framework_repo(tmp_path / "fwdest")
    _dest_with_url_upstream(dest, (tmp_path / "absent.git").as_uri())

    got = pm_import._resolve_add_harness_source(dest, "codex", None)

    assert got == dest.resolve(), f"로컬 templates 가 있는데 캐시로 갔다: {got}"
    assert not _cache_root(dest).exists(), "로컬 소스로 끝나는 형상에서 캐시를 만들었다"


def test_sync_adapter_config_check_resolves_url_upstream(
        pm_config, pm_import, tmp_path, capsys):
    """`sync-adapter-config --check` 가 URL upstream 에서 `--from` 없이 판정을 낸다."""
    source = _make_framework_repo(tmp_path / "fw")
    url = _make_bare(source, tmp_path / "fw.git")
    dest = _imported_codex_instance(tmp_path / "inst", url)
    import hashlib
    ledger = {"schema": 1, "files": {".codex/hooks.json": {
        "sha256": hashlib.sha256(_HOOKS.encode("utf-8")).hexdigest(),
        "recorded_at": "2026-01-01T00:00:00+09:00", "template_rev": "old"}}}
    _write(dest / ".project_manager" / "adapter_baseline.json",
           json.dumps(ledger, indent=2) + "\n")
    args = argparse.Namespace(list=False, check=True, accept=None, source=None)

    rc = pm_config.cmd_sync_adapter_config(args, pm_import=pm_import, dest_root=dest)

    captured = capsys.readouterr()
    assert "[중단]" not in captured.err, f"URL upstream 소스 미해소로 멈춤: {captured.err!r}"
    assert rc == 0, f"수렴 형상인데 rc={rc}: {captured.out!r} {captured.err!r}"
    assert _only_cache(dest).is_dir()


def test_sync_adapter_config_reports_cache_failure_loudly(
        pm_config, pm_import, tmp_path, capsys):
    """캐시 clone 실패는 traceback 이 아니라 `[중단]` + rc 1 로 나온다."""
    dest = _imported_codex_instance(tmp_path / "inst", (tmp_path / "absent.git").as_uri())
    args = argparse.Namespace(list=False, check=True, accept=None, source=None)

    rc = pm_config.cmd_sync_adapter_config(args, pm_import=pm_import, dest_root=dest)

    assert rc == 1
    err = capsys.readouterr().err
    assert "[중단]" in err and "캐시 clone 실패" in err, err


# ── 캐시 자리·재사용 판정의 단위 (경로 안전) ─────────────────────────────────

def test_cache_dir_is_per_url_inside_dest_local(pm_import, tmp_path):
    """캐시 자리는 dest `.project_manager/.local/upstream-cache/<해시>` 이고 URL 마다 갈린다."""
    dest = tmp_path / "dest"
    one = pm_import.upstream_cache_dir(dest, "https://example.invalid/a.git")
    two = pm_import.upstream_cache_dir(dest, "https://example.invalid/b.git")

    assert one.parent == (dest.resolve() / CACHE_RELDIR) and one != two
    assert one.name.isalnum() and len(one.name) == 16, f"캐시 이름에 URL 문자가 샜다: {one.name}"


def test_rejected_upstream_value_never_reaches_git(pm_import, tmp_path):
    """형태 안전 계약 위반 값(평문 http)은 clone 도 시도하지 않고 거부된다."""
    called: list = []

    with pytest.raises(pm_import.UpstreamCacheError):
        pm_import.resolve_upstream_url_cache(
            tmp_path / "dest", "http://example.invalid/x.git",
            git_runner=lambda argv: (called.append(argv), (0, ""))[1])

    assert called == [], "거부돼야 할 값으로 git 을 호출했다"


# ── AT-004: 스킬 카드에서 셸 절차 폐지 ──────────────────────────────────────

_SKILL_CARDS = (
    ".claude/skills/pm-update/SKILL.md",
    "templates/claude_code/.claude/skills/pm-update/SKILL.md",
    "templates/codex/.agents/skills/pm-update/SKILL.md",
    "templates/opencode/.claude/skills/pm-update/SKILL.md",
    "templates/opencode/.opencode/command/pm-update.md",
)
# 카드가 다시 들이면 안 되는 것 — 셸 clone 절차와 엔진 git 안전 계약(_UPSTREAM_GIT_CONFIG_KV)의 복제.
_RETIRED_CARD_TOKENS = (
    "$GIT", "protocol.allow", "credential.helper", "followRedirects",
    "fetch --all", "rev-parse HEAD",
)


@pytest.mark.parametrize("relpath", _SKILL_CARDS)
def test_skill_card_shell_procedure_retired(relpath):
    """URL 캐시는 엔진 소유다 — 카드는 절차도 git env 6키도 복제하지 않는다."""
    card = REPO / relpath
    assert card.is_file(), f"스킬 카드 부재: {relpath}"
    text = card.read_text(encoding="utf-8")

    assert "git clone" not in text.lower(), f"{relpath}: 셸 clone 절차 잔존"
    residual = [token for token in _RETIRED_CARD_TOKENS if token in text]
    assert not residual, f"{relpath}: 폐지된 셸 절차 조각 잔존 {residual}"
    assert "./pm-update.sh" in text, f"{relpath}: facade 한 줄 절차가 없다"


# 같은 계열 — pm-env 카드 5벌(canonical + templates 4벌)의 **소유자 서술**. pm-update 카드가
# "URL: 스킬이 할 일이 없다" 로 바뀐 뒤에도 pm-env 가 캐시 clone 을 스킬 소관으로 가르치면 두
# 카드가 서로 다른 소유자를 가르친다(같은 하네스 세션이 둘 다 읽는다).
_PM_ENV_CARDS = (
    ".claude/skills/pm-env/SKILL.md",
    "templates/claude_code/.claude/skills/pm-env/SKILL.md",
    "templates/codex/.agents/skills/pm-env/SKILL.md",
    "templates/opencode/.claude/skills/pm-env/SKILL.md",
    "templates/opencode/.opencode/command/pm-env.md",
)


@pytest.mark.parametrize("relpath", _PM_ENV_CARDS)
def test_pm_env_card_attributes_the_url_cache_to_the_engine(relpath):
    """pm-env 카드 5벌이 URL 캐시 clone/fetch 를 엔진 소관으로 서술한다(스킬 소관 서술 0)."""
    card = REPO / relpath
    assert card.is_file(), f"pm-env 카드 부재: {relpath}"
    text = card.read_text(encoding="utf-8")

    assert "URL이면 엔진" in text, f"{relpath}: 캐시 주체가 엔진으로 적혀 있지 않다"
    assert "upstream-cache" in text, f"{relpath}: 캐시 자리(엔진 소유 스크래치)를 가리키지 않는다"
    assert "로컬 경로 upstream의 pull" in text, f"{relpath}: 스킬 몫(경로 pull 판단)이 없다"
    for retired in ("URL이면 cache clone", "가 URL이면 cache clone, 경로면 pull"):
        assert retired not in text, f"{relpath}: 옛 소유자 서술 잔존 {retired!r}"
    # `upstream show|set` 검증 서술은 그대로 남아야 한다(문구 교체가 그 절을 지우지 않았다).
    assert "git ls-remote" in text and "https/ssh/file" in text, \
        f"{relpath}: upstream 검증·판별 규칙 서술이 사라졌다"
