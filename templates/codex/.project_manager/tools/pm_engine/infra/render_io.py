"""렌더 텍스트의 기존 개행 정책과 쓰기 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import fs


# ── 렌더 산출의 개행 표기 ─────────────────────────────────────────────────────
# 렌더 소스는 universal-newline 으로 읽어 LF 본문이 되지만, **dest 는 채택자 체크아웃 표기**다
# (Windows `core.autocrlf=true` 면 CRLF). pm_import 의 재렌더는 사본 표기를 보존하는데
# (`read/write_text_keeping_newlines`) self-update 가 같은 산출을 LF 로 되쓰면 같은 소스인데
# 전파 트리가 매 sync 전면 재기록된다(pm_import↔pm_update 렌더 drift). 그래서 **판정은 개행
# 정규화 후, 쓰기는 dest 의 현재 표기**로 한다. 플랫폼 분기는 쓰지 않는다 — 진실은 그 파일이다.
_DEFAULT_TEXT_NEWLINE = "\n"


def _normalize_newlines(text: str) -> str:
    """개행 표기 차이를 지운 판정용 본문 (CRLF → LF)."""
    return text.replace("\r\n", "\n")


def _dominant_newline(text: str, default: str = _DEFAULT_TEXT_NEWLINE) -> str:
    """번역 없이 읽은 본문의 지배 개행 — 다수결, 동수면 첫 등장, 개행 0 이면 `default`.

    `install_files.dominant_newline` 과 같은 규칙이다(테스트가 두 구현의 일치를 못박는다). 엔진
    모듈 간 import 를 늘리지 않으려고 각자 이 작은 원시 함수만 둔다."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return default
    if crlf != lf:
        return "\r\n" if crlf > lf else "\n"
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def _path_newline(path: Path) -> str:
    """그 파일의 현재 지배 개행 — 부재·읽기 실패·비-UTF8 은 `_DEFAULT_TEXT_NEWLINE`."""
    try:
        with fs.open_shared(Path(path), binary=False, encoding="utf-8", newline="") as handle:
            return _dominant_newline(handle.read(), _DEFAULT_TEXT_NEWLINE)
    except (OSError, UnicodeError):
        return _DEFAULT_TEXT_NEWLINE


def _write_rendered_text(dst: Path, source, rendered: str) -> None:
    """LF 렌더 산출물을 **dest 의 현재 표기**로 쓴다 (신규 파일은 소스 표기 = byte-copy 동형).

    신규는 소스 표기를 따른다 — pm_import 는 소스를 byte-copy 한 뒤 그 사본을 표기 보존으로
    재렌더하므로, 같은 파일을 sync 가 처음 깔 때도 같은 bytes 가 나와야 두 채널이 안 갈린다.
    `source` 가 파일이 아닌 인메모리 소스(`_ManifestTextSource`)면 표기 근거가 없어 LF 다."""
    dst = Path(dst)
    if dst.exists():
        newline = _path_newline(dst)
    elif isinstance(source, Path):
        newline = _path_newline(source)
    else:
        newline = _DEFAULT_TEXT_NEWLINE
    payload = rendered if newline == "\n" else rendered.replace("\n", newline)
    dst.write_bytes(payload.encode("utf-8"))
