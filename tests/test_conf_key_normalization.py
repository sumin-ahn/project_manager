"""local.conf 텍스트 키 갱신 규칙 — `conf_schema._set_conf_keys` 한 정의와 두 IO 소비자.

순수 규칙(첫 활성 자리 교체·뒤 활성 중복 제거·`None` 삭제·LF/CRLF·EOF·요청 순서)은
`pm_engine.domain.conf_schema` 가 소유하고, 파일·락·기록은 각 IO 소비자가 소유한다.
init(`conf_store._write_init_local_conf`)은 universal newline 으로 읽고 LF 로 직접 기록하며,
설치 key writer(`install_conf._write_conf_keys`)는 원문 개행 그대로 읽어 원자 교체한다.
정상 개행 계약은 LF·CRLF 다(CR 단독·Unicode 줄 구분자는 범위 밖).
"""
from __future__ import annotations

from types import MappingProxyType

import pytest

from pm_engine.domain import conf_schema
from pm_engine.infra import conf_store, fs, install_conf, pm_paths, python_env


def _apply_twice(text, updates):
    """한 번 적용한 결과와 같은 요청의 재적용 결과가 같고 `updates` 가 그대로인지 확인한다."""
    before = dict(updates)
    once = conf_schema._set_conf_keys(text, updates)
    assert conf_schema._set_conf_keys(once, updates) == once
    assert dict(updates) == before
    return once


def test_set_conf_keys_preserves_lf_crlf():
    assert _apply_twice("a=1\nk=old\nb=2\n", {"k": "new"}) == "a=1\nk=new\nb=2\n"
    assert _apply_twice("a=1\r\nk=old\r\nb=2\r\n", {"k": "new"}) == "a=1\r\nk=new\r\nb=2\r\n"
    # 혼합 표기 — 교체 행은 자기 끝 표기를, 다른 행은 원문을 각각 유지한다.
    assert _apply_twice("a=1\nk=old\r\nb=2\n", {"k": "new"}) == "a=1\nk=new\r\nb=2\n"
    assert _apply_twice("a=1\r\nk=old\nb=2\r\n", {"k": "new"}) == "a=1\r\nk=new\nb=2\r\n"
    assert _apply_twice("이름=옛값\r\n# 주석\r\n", {"이름": "새값"}) == "이름=새값\r\n# 주석\r\n"


def test_set_conf_keys_collapses_active_duplicates():
    text = "# k=comment\n  k = a\nother=1\nk=b\nx=1\nx=2\n"
    updates = MappingProxyType({"k": "new"})
    out = _apply_twice(text, updates)
    # 첫 활성 자리에 한 줄, 뒤 활성 중복은 제거, 주석 속 `k=` 와 대상 밖 중복 `x` 는 보존.
    assert out == "# k=comment\nk=new\nother=1\nx=1\nx=2\n"
    parsed = conf_schema.parse(out)
    assert parsed["k"] == "new"
    assert parsed["x"] == "2"
    assert _apply_twice("k=a=b\nk=c\n", {"k": "x=y"}) == "k=x=y\n"


def test_set_conf_keys_none_removes_active_keys():
    out = _apply_twice("k=a\nx=1\n# k=memo\nk=b\n", {"k": None})
    assert out == "x=1\n# k=memo\n"
    assert "k" not in conf_schema.parse(out)
    # 없는 키 삭제는 원문 무변경(문자 `None` 을 추가하지 않는다).
    assert _apply_twice("x=1", {"gone": None}) == "x=1"
    assert _apply_twice("x=1\nk=a", {"k": None}) == "x=1\n"
    # 빈 문자열은 빈 값, 문자열 "None" 은 그 문자열이다 — 삭제와 구분한다.
    empty = _apply_twice("k=a\nk=b\n", {"k": ""})
    assert empty == "k=\n"
    assert conf_schema.parse(empty) == {"k": ""}
    assert _apply_twice("k=a\nk=b\n", {"k": "None"}) == "k=None\n"


def test_set_conf_keys_appends_with_existing_notation_and_order():
    assert _apply_twice("", {"a": "1", "b": "2"}) == "a=1\nb=2\n"
    # 요청 순서대로 추가하고, 개행 없는 마지막 줄 뒤에는 경계를 먼저 만든다.
    assert _apply_twice("x=1", {"z": "3", "a": "2"}) == "x=1\nz=3\na=2\n"
    # 원문에 CRLF 가 한 번이라도 있으면 추가 행은 CRLF — 다수/마지막 행 표기로 고르지 않는다.
    assert _apply_twice("x=1\r\ny=2\nw=3\n", {"k": "v=w"}) == "x=1\r\ny=2\nw=3\nk=v=w\r\n"
    assert _apply_twice("x=1\r\ny=2", {"k": "v"}) == "x=1\r\ny=2\r\nk=v\r\n"
    assert _apply_twice("x=1\r\n", {"k": "v", "x": "2"}) == "x=2\r\nk=v\r\n"


def test_set_conf_keys_preserves_final_newline_state():
    assert _apply_twice("a=1\nk=old", {"k": "new"}) == "a=1\nk=new"
    assert _apply_twice("k=old\r\n", {"k": "new"}) == "k=new\r\n"
    assert _apply_twice("k=old\nb=2", {"k": "new"}) == "k=new\nb=2"
    # 마지막 키 삭제 — 남은 마지막 행의 원문 끝을 그대로 둔다(LF 를 더하거나 빼지 않는다).
    assert _apply_twice("a=1\r\nk=old\n", {"k": None}) == "a=1\r\n"
    assert _apply_twice("a=1\nb=2", {"a": None}) == "b=2"


def test_set_conf_keys_empty_updates_and_writer_boundaries(monkeypatch, tmp_path):
    for text in ("k=a\r\n", "k=a", "", "# c\nk=a\nk=b\n"):
        assert conf_schema._set_conf_keys(text, {}) == text
    # 두 IO 소비자에 사본·별칭이 없다 — 규칙은 conf_schema 한 곳이다.
    assert not hasattr(conf_store, "_set_conf_keys")
    assert not hasattr(install_conf, "_set_conf_keys")

    held: list[bool] = []
    real_lock = fs.local_conf_write_lock

    def _observed_lock(conf_path):
        lock = real_lock(conf_path)

        class _Held:
            def __enter__(self):
                value = lock.__enter__()
                held.append(True)
                return value

            def __exit__(self, *exc):
                held.pop()
                return lock.__exit__(*exc)

        return _Held()

    calls: list[tuple[str, dict, bool]] = []
    real_set = conf_schema._set_conf_keys

    def _counting_set(text, updates):
        calls.append((text, dict(updates), bool(held)))
        return real_set(text, updates)

    monkeypatch.setattr(fs, "local_conf_write_lock", _observed_lock)
    monkeypatch.setattr(conf_schema, "_set_conf_keys", _counting_set)
    monkeypatch.setattr(python_env, "_detect_py", lambda: "python3")
    monkeypatch.setattr(python_env, "default_pytest_cmd", lambda py: f"{py} -m pytest tests/ -q")
    defaults = "".join(
        f"{key}={conf_schema.default_for(key)}\n"
        for key in ("ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct"))

    # init 첫 생성 — 규칙 함수를 부르지 않고 default 전체를 LF 로 직접 기록한다.
    home = pm_paths.home_at(tmp_path / "init")
    home.local_conf.parent.mkdir(parents=True)
    conf_store._write_init_local_conf(home)
    assert calls == []
    assert b"\r" not in home.local_conf.read_bytes()

    # init 병합 — 락 안에서 공유 규칙을 부르고 없는 기본키만 LF 로 덧붙인다.
    home.local_conf.write_bytes(b"session=pm")
    conf_store._write_init_local_conf(home)
    assert len(calls) == 1 and calls[0][2] is True
    assert home.local_conf.read_bytes() == (
        "session=pm\nruntime.py=python3\ntest.cmd=python3 -m pytest tests/ -q\nproject.name=\n"
        + defaults).encode("utf-8")

    # 기본키가 다 있는 CRLF conf — 요청은 비어도 init 의 LF 판독·마지막 LF 보장·직접 기록은 그대로다.
    full_crlf = ("runtime.py=py\r\ntest.cmd=t\r\nproject.name=n\r\nctx.checkpoint_at_pct=1\r\n"
                 "ctx.precompact_at_pct=2\r\nctx.compact_at_pct=3")
    home.local_conf.write_bytes(full_crlf.encode("utf-8"))
    conf_store._write_init_local_conf(home)
    assert calls[-1][1] == {}
    assert home.local_conf.read_bytes() == (full_crlf.replace("\r\n", "\n") + "\n").encode("utf-8")

    # 설치 key writer — 원문 개행 그대로 정규화하고 바뀔 때만 원자 교체한다.
    replaced: list[str] = []
    real_replace = fs.atomic_replace

    def _counting_replace(src, dst):
        replaced.append(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(fs, "atomic_replace", _counting_replace)
    conf = tmp_path / "writer" / "local.conf"
    conf.parent.mkdir()
    conf.write_bytes(b"# c\r\nk=a\r\nx=1\r\nk=b\r\n")
    assert install_conf._write_conf_keys(conf, {"k": "new"}) is True
    assert conf.read_bytes() == b"# c\r\nk=new\r\nx=1\r\n"
    assert calls[-1][2] is True
    assert replaced == [str(conf)]
    assert install_conf._write_conf_keys(conf, {"k": "new"}) is False
    assert replaced == [str(conf)]
    assert sorted(path.name for path in conf.parent.iterdir() if path.suffix == ".tmp") == []

    # IO 실패는 bool 성공으로 바뀌지 않고 그대로 올라온다.
    def _denied_replace(src, dst):
        raise PermissionError("replace denied")

    monkeypatch.setattr(fs, "atomic_replace", _denied_replace)
    with pytest.raises(PermissionError, match="replace denied"):
        install_conf._write_conf_keys(conf, {"k": "other"})
    assert conf.read_bytes() == b"# c\r\nk=new\r\nx=1\r\n"
    with pytest.raises(FileNotFoundError):
        install_conf._write_conf_keys(conf.parent / "absent.conf", {"k": "v"})

    # 재읽기 실효값 검증 — 규칙이 요청을 반영하지 못하면 RuntimeError 로 멈춘다.
    monkeypatch.setattr(fs, "atomic_replace", real_replace)
    monkeypatch.setattr(conf_schema, "_set_conf_keys", lambda text, updates: text)
    with pytest.raises(RuntimeError, match="실효값 불일치"):
        install_conf._write_conf_keys(conf, {"k": "other"})
