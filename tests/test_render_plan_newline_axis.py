"""render 계열 '변경 없음' 판정의 개행 축 — 표기만 다르면 unchanged, 1자 다르면 update.

결함 형상([[T-0723]]): 전면 렌더(`@render`) 분기는 개행 정규화 후 대조하는데, 호출 표기·flat
command·codex operational **최소 렌더** 분기만 raw bytes 로 대조했다. 쓰기 축
(`_write_rendered_text`)은 dest 표기를 보존하므로, CRLF 체크아웃(Windows `core.autocrlf=true`)
채택자에게는 그 분기가 **영원히 수렴하지 않는다** — 매 sync 가 내용 동일 파일을 update 로 계획하고
같은 상태로 다시 써넣는다. 실측 2건: 공유 wiki 4파일(zero-change RUN2 경계 붕괴)·flat command
사본 14개.

이 파일은 그 대조 규칙(`_rendered_text_matches_dest`)을 **양방향으로** 못박는다. 정규화가 판정을
무디게 만드는 반대 사고(내용 차이까지 삼킴)를 같은 자리에서 막는다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from _textio import write_crlf, write_lf

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 렌더 산출물은 항상 LF 본문이다(소스를 universal-newline 으로 읽는다). dest 표기만 갈린다.
_RENDERED_LF = "# 제목\n\n한 줄\n두 줄\n"


@pytest.fixture(scope="module")
def pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _crlf_dest(tmp_path: Path, text: str) -> Path:
    """CRLF 표기 dest 를 만들고 **주입이 실제로 걸렸는지** 그 자리에서 확인한다."""
    dest = tmp_path / "dest.md"
    write_crlf(dest, text)
    raw = dest.read_bytes()
    assert b"\r\n" in raw, "픽스처가 CRLF dest 를 만들지 못했다(공허 회귀)"
    assert raw != text.encode("utf-8"), \
        "CRLF dest 가 LF 렌더 산출물과 byte 동일하다 — 이 가드가 시험되지 않는다"
    return dest


def test_crlf_dest_with_same_content_is_unchanged(pm_update, tmp_path):
    """표기만 다른 dest 는 '변경 없음' 이다 — 아니면 sync 가 수렴하지 않는다."""
    dest = _crlf_dest(tmp_path, _RENDERED_LF)

    assert pm_update._rendered_text_matches_dest(_RENDERED_LF, dest) is True


@pytest.mark.parametrize(
    ("label", "changed"),
    (
        ("한 글자", "# 제목\n\n한 줄\n두 줄!\n"),
        ("공백 하나", "# 제목\n\n한 줄 \n두 줄\n"),
        ("줄 추가", "# 제목\n\n한 줄\n두 줄\n세 줄\n"),
        ("줄 삭제", "# 제목\n\n한 줄\n"),
    ),
)
def test_crlf_dest_with_different_content_is_still_update(
        pm_update, tmp_path, label, changed):
    """개행 정규화는 **표기만** 지운다 — 내용이 한 글자라도 다르면 여전히 update 다."""
    dest = _crlf_dest(tmp_path, _RENDERED_LF)

    assert pm_update._rendered_text_matches_dest(changed, dest) is False, \
        f"정규화가 내용 차이({label})까지 삼켰다 — 판정이 무뎌졌다"


def test_lf_dest_axis_is_unchanged_by_normalization(pm_update, tmp_path):
    """LF dest(기존 다수 형상)의 판정은 그대로다 — 같으면 unchanged, 다르면 update."""
    dest = tmp_path / "dest.md"
    write_lf(dest, _RENDERED_LF)

    assert pm_update._rendered_text_matches_dest(_RENDERED_LF, dest) is True
    assert pm_update._rendered_text_matches_dest(_RENDERED_LF + "꼬리\n", dest) is False


def test_unreadable_or_binary_dest_is_conservative_difference(pm_update, tmp_path):
    """읽을 수 없는·비-UTF8 dest 는 '같음' 으로 접지 않는다(침묵 폴백 금지)."""
    binary = tmp_path / "binary.md"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    missing = tmp_path / "absent.md"

    assert pm_update._rendered_text_matches_dest(_RENDERED_LF, binary) is False
    assert pm_update._rendered_text_matches_dest(_RENDERED_LF, missing) is False


def test_full_render_branch_shares_the_same_comparison_seam(pm_update):
    """전면 렌더 판정은 이 대조 규칙을 **호출**한다 — 축이 두 벌로 갈리지 않는다."""
    source = (TOOLS / "pm_update.py").read_text(encoding="utf-8")
    body = source.split("def _render_eq_dst(", 1)[1].split("\ndef ", 1)[0]
    assert "_rendered_text_matches_dest(" in body, \
        "전면 렌더 분기가 자기 대조 규칙을 되살렸다(판정 사본)"
    plan_body = source.split("\ndef plan(", 1)[1].split("\ndef ", 1)[0]
    assert "!= dst.read_bytes()" not in plan_body, \
        "plan 의 렌더 분기에 raw bytes 대조가 되살아났다(CRLF 수렴 불가 재발)"
