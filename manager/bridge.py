"""JS 브리지 — 화면이 부를 수 있는 표면 전부.

묶음 다섯 티켓이 한 파일을 고치는 일을 없애려고 **평범한 다중 상속**으로 나눈다(동적 등록·
플러그인 기계 0). 이 파일이 기본 여섯 메서드(`BaseApi`)와 최종 합성(`Api`)을 소유하고,
탭별 동작은 형제 mixin 파일이 하나씩 채운다.

반환 계약:
  - 정상은 **엔진 JSON 그대로**다(감싸지 않는다). 화면은 `"error" in resp` 로 갈린다.
  - 실패는 `{"error": {"kind", "message", "stderr", "rc"}}` 다(`CallError.as_error_dict`).
    예외를 삼켜 None·빈 값으로 바꾸는 자리는 없다.
"""
from __future__ import annotations

from pathlib import Path

from . import CallError
from . import registry, runner, upstream
from .bridge_run import RunApi
from .bridge_settings import SettingsApi
from .bridge_update import UpdateApi


class BaseApi:
    """프로젝트 목록·현황·최신 태그 — 어느 탭에도 속하지 않는 골격 여섯."""

    def __init__(self, *, folder_picker, registry_file: Path | str | None = None) -> None:
        # 폴더 선택 대화는 창이 소유한다 — 브리지는 pywebview 를 import 하지 않는다.
        self._folder_picker = folder_picker
        self._registry_file = registry_file

    # ── 등록부 ────────────────────────────────────────────────────────────
    def list_projects(self) -> dict:
        try:
            return self._projects(registry.load(self._registry_file))
        except CallError as exc:
            return exc.as_error_dict()

    def add_project(self, path: str) -> dict:
        try:
            return self._projects(registry.add(path, self._registry_file))
        except CallError as exc:
            return exc.as_error_dict()

    def remove_project(self, path: str) -> dict:
        try:
            return self._projects(registry.remove(path, self._registry_file))
        except CallError as exc:
            return exc.as_error_dict()

    # ── 프로젝트 사본 조회 ────────────────────────────────────────────────
    def status(self, path: str, with_board: bool = False, with_adapters: bool = False) -> dict:
        try:
            return runner.status(path, with_board=with_board, with_adapters=with_adapters)
        except CallError as exc:
            return exc.as_error_dict()

    def latest_tags(self) -> dict:
        """등록 프로젝트의 upstream 별 최신 태그 — 같은 upstream 은 조회 1회로 합친다."""
        try:
            homes = registry.load(self._registry_file)
        except CallError as exc:
            return exc.as_error_dict()
        projects: dict[str, dict] = {}
        for home in homes:
            projects[str(home)] = {"upstream": runner.upstream(home)}
        values = [entry["upstream"] for entry in projects.values() if entry["upstream"]]
        return {"projects": projects, "upstreams": upstream.latest_tags(values)}

    # ── 창 ────────────────────────────────────────────────────────────────
    def pick_folder(self) -> dict:
        """OS 폴더 선택 대화. 취소하면 `path` 가 null 이다."""
        selected = self._folder_picker()
        return {"path": None if selected is None else str(selected)}

    # ── 내부 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _projects(homes) -> dict:
        return {"projects": [{"path": str(home), "name": home.name} for home in homes]}


class Api(BaseApi, SettingsApi, UpdateApi, RunApi):
    """창에 붙는 최종 브리지 — 골격 + 탭별 mixin 셋."""
