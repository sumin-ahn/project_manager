"""ADR 발행 (층: app).

채번, 대상 ADR 탐색, 발행 계획 산출과 적용(신규 파일 → 대상 back-ref → README 색인 → log 항목)을 소유한다.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from pm_engine.domain import adr_rules
from pm_engine.infra import fs, log_store


# ── 1. 채번 ──────────────────────────────────────────────────────────────────

def next_adr_number(decisions_dir: Path) -> int:
    """decisions/ 의 `NNNN-*.md` 를 스캔해 다음 번호를 반환한다.

    decisions/ 부재(신규 clone)면 1. 파일이 있으면 max+1. `.stem` 앞 숫자 토큰만 본다
    (README.md·_template.md 등 비-숫자 파일 자연 제외)."""
    if not decisions_dir.is_dir():
        return 1
    nums = []
    for p in decisions_dir.glob("[0-9]*.md"):
        m = re.match(r"^(\d+)", p.stem)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _find_target_file(decisions_dir: Path, num: int) -> Path | None:
    """대상 ADR 번호 → `decisions/NNNN-*.md` 파일 경로 (없으면 None)."""
    matches = sorted(decisions_dir.glob(f"{num:04d}-*.md"))
    return matches[0] if matches else None


# ── 오케스트레이션 ────────────────────────────────────────────────────────────

class AdrIssuer:
    """ADR 발행/개정 원자화 핵심 로직.

    경로는 DI 로 주입해 hermetic 테스트(실 wiki 미접촉). apply 는 모든 계획 산출을 모아
    마지막에 파일 쓰기 — 중간 실패 시 부분 상태를 남기지 않는다(채번→scaffold→back-ref→
    README→log 순으로 계획하고, dry-run 이면 미리보기만)."""

    def __init__(
        self,
        *,
        decisions_dir: Path,
        readme_file: Path | None = None,
        log_file: Path,
        date: str | None = None,
    ) -> None:
        self._decisions_dir = decisions_dir
        self._readme_file = readme_file if readme_file is not None else decisions_dir / "README.md"
        self._log_file = log_file
        self._date = date or datetime.date.today().isoformat()

    def plan(
        self,
        *,
        title: str,
        slug: str,
        scope: str,
        author: str,
        status: str,
        amends: list[int],
        supersedes: list[int],
        refines: list[int],
        related: list[str],
        tags: list[str],
    ) -> dict:
        """발행 계획을 산출한다 — 실제 쓰기 없이 (경로, 내용, warnings)."""
        number = next_adr_number(self._decisions_dir)
        warnings: list[str] = []

        adr_path = self._decisions_dir / adr_rules.adr_filename(number, slug)
        adr_text = adr_rules.build_adr_file(
            number=number, title=title, scope=scope, author=author, date=self._date,
            status=status, amends=amends, supersedes=supersedes, refines=refines,
            related=related, tags=tags,
        )

        # 대상 ADR frontmatter back-ref (load-bearing·lint 정합).
        target_edits: list[tuple[Path, str]] = []
        for verb, nums in (("amends", amends), ("supersedes", supersedes)):
            for tnum in nums:
                tpath = _find_target_file(self._decisions_dir, tnum)
                if tpath is None:
                    warnings.append(f"{verb} 대상 {adr_rules.adr_id(tnum)} 파일을 decisions/ 에서 못 찾음 — back-ref skip")
                    continue
                original = fs.read_text_shared(tpath, encoding="utf-8")
                edited = adr_rules.apply_lifecycle_backref(original, adr_rules.adr_id(number), verb)
                if edited == original and adr_rules._split_frontmatter(original) is None:
                    warnings.append(f"{adr_rules.adr_id(tnum)} frontmatter 파싱 실패 — back-ref skip")
                    continue
                target_edits.append((tpath, edited))

        # README 색인.
        readme_text = None
        if self._readme_file.exists():
            readme_text = fs.read_text_shared(self._readme_file, encoding="utf-8")
            readme_text, w = adr_rules.insert_accepted_row(
                readme_text, number=number, slug=slug, title=title, date=self._date, tags=tags,
            )
            if w:
                warnings.append(w)
            for verb, nums in (("amends", amends), ("supersedes", supersedes)):
                for tnum in nums:
                    readme_text, w = adr_rules.move_or_append_backref_row(
                        readme_text, target_num=tnum, new_id=adr_rules.adr_id(number), verb=verb,
                    )
                    if w:
                        warnings.append(w)
        else:
            warnings.append(f"README({self._readme_file}) 부재 — 색인 skip")

        # log decide entry.
        log_entry = adr_rules.build_decide_log_entry(number, title, self._date)

        return {
            "number": number,
            "adr_path": adr_path,
            "adr_text": adr_text,
            "target_edits": target_edits,
            "readme_text": readme_text,
            "log_entry": log_entry,
            "warnings": warnings,
        }

    def apply(self, plan: dict) -> None:
        """계획을 파일에 쓴다 — **신규 ADR 파일을 맨 먼저**, 이어 대상 back-ref·README·log 순차 적용.

        full staged-write(전 파일 tmp→원자 rename)는 과설계로 보류한다 —
        ADR 발행은 단일-PM·저빈도 수동 작업이라 다중 파일 트랜잭션이 과하다. 대신 **부분 실패 시 어느
        단계까지 됐는지(수행/미수행) 명시**해 사람이 복구하게 한다. 신규 ADR 파일을 **가장 먼저** 써서
        최악의 부분 실패에서도 결정 본문 자체는 디스크에 보존되게 한다(주 산출물 우선)."""
        self._decisions_dir.mkdir(parents=True, exist_ok=True)
        steps: list[tuple[str, "callable"]] = [
            ("신규 ADR 파일", lambda: plan["adr_path"].write_text(
                plan["adr_text"], encoding="utf-8", newline="\n")),
            ("대상 back-ref", lambda: self._write_targets(plan)),
            ("README 색인", lambda: self._write_readme(plan)),
            ("log decide entry", lambda: self._append_log(plan)),
        ]
        done: list[str] = []
        for label, fn in steps:
            try:
                fn()
            except OSError as exc:
                remaining = [lbl for lbl, _ in steps[len(done):]]
                raise RuntimeError(
                    f"ADR 발행 부분 실패 — '{label}' 단계에서 중단({exc}). "
                    f"수행 완료: {done or ['없음']} · 미수행: {remaining}. "
                    "미수행 단계를 손으로 마저 적용하라 (신규 ADR 파일은 가장 먼저 기록되므로 "
                    "결정 본문은 보존됨)."
                ) from exc
            done.append(label)

    def _write_targets(self, plan: dict) -> None:
        for tpath, edited in plan["target_edits"]:
            tpath.write_text(edited, encoding="utf-8", newline="\n")

    def _write_readme(self, plan: dict) -> None:
        if plan["readme_text"] is not None:
            self._readme_file.write_text(
                plan["readme_text"], encoding="utf-8", newline="\n")

    def _append_log(self, plan: dict) -> None:
        payload = "\n" + plan["log_entry"]
        log_store.append_log(self._log_file, payload)
