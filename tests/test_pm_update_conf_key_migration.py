"""갱신 시점 local.conf 키 이름 이관 (`pm_update.migrate_renamed_conf_keys`).

이름을 바꾼 것은 갱신이므로 갱신이 옮긴다 — 채택자가 손으로 두 줄을 고칠 때까지 그 값을
소비하는 명령이 fail-loud 로 서 있지 않게 하는 자리다. 이 파일이 못박는 계약:

  - 값은 그대로 신표기로 가고 **옛 줄은 사라진다**(남으면 소비 지점이 계속 멈춘다).
  - 신표기에 이미 값이 있으면 그 값이 이긴다 — 옛 줄만 지운다.
  - 대체 키가 없는 폐지 키는 옮기지 않는다(값이 갈 자리가 없다 · 안내가 계속 지목한다).
  - `write=False` 는 판정만이고, 재실행은 byte 를 바꾸지 않는다(멱등).

키 이름을 테스트가 적지 않는다 — 무엇이 무엇으로 바뀌었는지의 정본은 엔진
`local_conf.LEGACY_KEY_MAP` 이고 여기서는 그 표에서 꺼내 쓴다(적으면 rename 마다 이 파일이
같이 틀린다).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCAL_CONF = _load("local_conf")
# 표에서 꺼낸 실 사례 — 이름이 바뀐 키 하나와 대체 없이 폐지된 키 하나.
RENAMED, RENAMED_TO = next(
    (old, new) for old, new in LOCAL_CONF.LEGACY_KEY_MAP.items() if new is not None)
RETIRED = next(
    old for old, new in LOCAL_CONF.LEGACY_KEY_MAP.items()
    if new is None and old not in LOCAL_CONF.NON_BLOCKING_LEGACY_KEYS)


def _conf(dest: Path, body: str) -> Path:
    path = dest / ".project_manager" / "local.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def _values(path: Path) -> dict[str, str]:
    return LOCAL_CONF.load(path).values


def test_a_renamed_key_moves_with_its_value_and_the_old_line_is_gone(tmp_path):
    pm_update = _load("pm_update")
    path = _conf(tmp_path / "home", f"runtime.py=python3\n{RENAMED}=keep-me\n")

    moved = pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=True)

    assert moved == {RENAMED: RENAMED_TO}
    values = _values(path)
    assert values[RENAMED_TO] == "keep-me", "값이 따라오지 않았다"
    assert RENAMED not in values, "옛 줄이 남으면 소비 지점이 계속 멈춘다"
    assert values["runtime.py"] == "python3", "관계없는 줄을 건드렸다"


def test_an_explicit_new_value_wins_and_only_the_old_line_is_dropped(tmp_path):
    pm_update = _load("pm_update")
    path = _conf(tmp_path / "home", f"{RENAMED}=old\n{RENAMED_TO}=new\n")

    pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=True)

    values = _values(path)
    assert values[RENAMED_TO] == "new", "사람이 새 이름으로 적은 값을 옛 값이 덮었다"
    assert RENAMED not in values


def test_a_retired_key_without_a_replacement_is_never_moved(tmp_path):
    pm_update = _load("pm_update")
    path = _conf(tmp_path / "home", f"{RETIRED}=whatever\n")
    before = path.read_bytes()

    moved = pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=True)

    assert moved == {}
    assert path.read_bytes() == before, "값이 갈 자리가 없는 키를 지우는 건 사람이 정한다"


def test_a_dry_run_decides_only_and_a_rerun_changes_nothing(tmp_path):
    pm_update = _load("pm_update")
    path = _conf(tmp_path / "home", f"{RENAMED}=keep-me\n")
    before = path.read_bytes()

    assert pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=False) == {
        RENAMED: RENAMED_TO}
    assert path.read_bytes() == before, "dry-run 이 파일을 바꿨다"

    assert pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=True) == {
        RENAMED: RENAMED_TO}
    after = path.read_bytes()
    assert pm_update.migrate_renamed_conf_keys(tmp_path / "home", write=True) == {}
    assert path.read_bytes() == after, "재실행이 byte 를 바꿨다"


def test_a_tree_without_a_conf_is_not_a_target(tmp_path):
    pm_update = _load("pm_update")

    assert pm_update.migrate_renamed_conf_keys(tmp_path / "nope", write=True) == {}


@pytest.mark.parametrize("new_value", [None, "88"])
def test_t0927_conf_contract(tmp_path, new_value):
    updater = _load("pm_update")
    path = _conf(tmp_path / "home", "")
    original = ("# 사용자 주석\r\ncustom.key = 보존\r\nctx.stop_pct=10\r\n"
                f"{RETIRED}=keep\r\n" +
                (f"ctx.precompact_at_pct={new_value}\r\n" if new_value else ""))
    path.write_bytes(original.encode("utf-8"))
    updater.migrate_renamed_conf_keys(path.parent.parent, write=False)
    assert path.read_bytes() == original.encode("utf-8")
    updater.migrate_renamed_conf_keys(path.parent.parent, write=True)
    after = path.read_bytes()
    assert b"ctx.stop_pct=" not in after
    assert f"ctx.precompact_at_pct={new_value or '90'}\r\n".encode() in after
    assert "# 사용자 주석\r\ncustom.key = 보존\r\n".encode() in after
    assert f"{RETIRED}=keep\r\n".encode() in after
    assert updater.migrate_renamed_conf_keys(path.parent.parent, write=True) == {}
    assert path.read_bytes() == after


def test_t0927_conf_contract_partial_writer_is_failure(tmp_path, monkeypatch):
    updater = _load("pm_update")
    writer = _load("pm_import")
    path = _conf(tmp_path / "home", "ctx.stop_pct=10\n")
    monkeypatch.setattr(updater, "_load_pm_import", lambda: writer)
    monkeypatch.setattr(writer, "_write_conf_keys_locked", lambda *args: False)
    with pytest.raises(RuntimeError, match="ctx.stop_pct"):
        updater.migrate_renamed_conf_keys(path.parent.parent, write=True)
