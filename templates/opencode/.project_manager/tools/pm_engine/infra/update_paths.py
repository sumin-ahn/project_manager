"""갱신 계획의 경로 운반 값과 인메모리 manifest 원본 (층: infra)."""
from __future__ import annotations

from pathlib import Path


class _RenderDst:
    """change tuple 의 dst — 내부 Path 에 위임하되 `.render` 플래그를 운반하는 thin 래퍼.

    plan 이 dst 에 render 여부를 실어 apply 가 byte-copy vs render 를 분기하게 한다. change
    tuple 을 4-요소로 유지(`(rel, src, dst, kind)`)해 기존 unpack 호출부/테스트를 깨지 않으면서
    render 정보를 운반한다. Path 직접 서브클래싱(버전별 `_flavour` 함정·하위 호환 약화)을 피하고
    `__fspath__`/`__eq__`/`__getattr__` 위임으로 테스트가 쓰는 표면(`dst.exists()`·`dst.parent`·
    `dst == Path(...)`·`str(dst)`·`Path(dst)`)을 모두 지원한다. 평문 Path dst(레거시 apply
    직접 호출)는 이 래퍼가 아니므로 `getattr(dst, "render", False)` 가 False → copy2(후방호환).
    """

    __slots__ = (
        "_path", "render", "entry_notation_template", "flat_command_skill",
        "codex_operational_skill", "guest_backfill_lines",
    )

    def __init__(
        self,
        path: Path,
        render: bool = False,
        entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
        flat_command_skill: str | None = None,
        codex_operational_skill: str | None = None,
        guest_backfill_lines: list[str] | None = None,
    ) -> None:
        self._path = Path(path)
        self.render = render
        self.entry_notation_template = entry_notation_template
        self.flat_command_skill = flat_command_skill
        self.codex_operational_skill = codex_operational_skill
        # engine.manifest self-prop 전용 — apply 의 guest 절 재부착에 함께 기록할 파생 엔진 행.
        #   change tuple 에 실어 나르므로 `apply` 시그니처가 바뀌지 않는다(`entry_notation_template`
        #   과 같은 운반 방식).
        self.guest_backfill_lines = guest_backfill_lines

    def __fspath__(self) -> str:
        return str(self._path)

    def __getattr__(self, name):
        # _path 의 메서드/속성(exists·parent·read_text 등)으로 위임. __slots__ 정의 속성은
        # 이 메서드 진입 전 처리되므로 무한재귀 없음.
        return getattr(self._path, name)

    def __eq__(self, other) -> bool:
        if isinstance(other, _RenderDst):
            return self._path == other._path
        return self._path == other

    def __hash__(self) -> int:
        return hash(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return (
            f"_RenderDst({self._path!r}, render={self.render}, "
            f"entry_notation_template={self.entry_notation_template!r}, "
            f"flat_command_skill={self.flat_command_skill!r}, "
            f"codex_operational_skill={self.codex_operational_skill!r}, "
            f"guest_backfill_lines={self.guest_backfill_lines!r})"
        )


class _ManifestTextSource:
    """선택된 flavor manifest 합집합 텍스트를 change tuple에 싣는 인메모리 source.

    합집합은 upstream checkout 안의 단일 파일로 존재하지 않는다. 임시 파일을 만들지 않고도
    plan/apply 4-tuple 계약을 유지하도록 ``read_text``만 제공한다. engine.manifest 전용이며
    apply의 self-prop 분기가 이 객체를 직접 소비한다.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.text
