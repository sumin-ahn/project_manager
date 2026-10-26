"""리뷰 diff 측정과 기계 사본 판정 (층: infra)."""
from __future__ import annotations

from typing import Callable
from typing import NamedTuple
from pathlib import Path
from typing import Sequence
from pm_engine.infra import manifest_store
import re
from pm_engine.domain import reviewer_budget
import subprocess


# ── git diff 추출 ─────────────────────────────────────────────────────────
# "이번 base 의 diff 는 어디까지인가"(=폭)를 여기서 한 번만 서술한다. 등록 슬롯 후보 판정
# (`_candidate_has_diff`)과 실제 추출(`extract_diff`)이 같은 단계 표를 소비해야, 후보 판정에서
# '변경 없음'으로 탈락한 슬롯을 추출은 리뷰하는(또는 그 반대) 한 칸 어긋남이 생기지 않는다.

_COMMIT_FALLBACK_BASE = "HEAD~1..HEAD"


def _diff_bases(base: str) -> tuple[str, ...]:
    """`base` 한 폭을 이루는 diff 단계 목록 — 앞 단계가 비었을 때만 다음 단계가 유효하다.

    'HEAD' 는 작업트리(스테이징+언스테이징)가 첫 단계이고, 그게 비면 직전 커밋 한 칸까지가
    같은 폭이다. 명시 base 는 단계가 하나뿐이다.
    """
    return (base, _COMMIT_FALLBACK_BASE) if base == "HEAD" else (base,)


def _slot_selection_bases(base: str) -> tuple[str, ...]:
    """등록 슬롯 후보 판정에 **소유 근거로 쓸 수 있는** 단계 — `_diff_bases` 의 부분집합.

    사용자가 지정한 base 자신만 근거다. 암묵 폴백 단계(`HEAD~1..HEAD`)는 이 슬롯이 무엇을 했는지
    말해주지 않는다 — 슬롯이 아무 것도 안 해도 **공유 base 의 마지막 커밋**이 검토 경로를
    건드렸으면 비어 있지 않아, 놀고 있는 슬롯을 '변경 슬롯'으로 뽑고 그 커밋을 이번 작업물인 양
    리뷰어에게 보낸다. 그래서 그 단계는 *이미 고른 repo 안에서 무엇을 리뷰할지*(추출 폭)에만 남기고
    *어느 repo인지*의 근거에서는 뺀다. 커밋만 된 변경으로 슬롯을 고르려면 앵커를 명시해야 한다 —
    `--base <공통 base ref>`(그 자체로 단계 하나) 또는 repo 를 직접 지정하는 절대 `--paths`.
    """
    return tuple(stage for stage in _diff_bases(base) if stage == base)


# untracked 신규 파일의 '새 파일 diff' 는 `--no-index` 로 뽑는다 — **index 를 건드리지 않는
# 방식**이어야 한다. `git add -N`(intent-to-add) 선행이 더 짧지만 그건 read-only 여야 할 리뷰·측정
# 경로가 채택자 index 를 바꾸는 일이고, 위임 스코프 가드가 `git status --porcelain` 지문으로
# 작업트리 상태를 재는 축과 정면으로 부딪힌다(리뷰가 지문을 흔들면 범위 위반으로 오판된다).
# `/dev/null` 은 git 이 **문자열 그대로** 특수 취급하는 이름이라 Windows 에서도 같은 인자다.
_DEV_NULL_DIFF_OPERAND = "/dev/null"
# `--no-index` 는 `--exit-code` 를 내장해 **차이가 있으면 rc 1** 이다. 소비자는 rc 0 만 읽으므로
# (실패한 실행 = 그 단계에 변경 없음) 여기서 '차이 있음'을 rc 0 으로 접는다. rc≥2(진짜 오류)는
# 그대로 두어 종전 폴백 규칙을 탄다.
_NO_INDEX_DIFFERENCES_RC = 1


def _untracked_paths(
    root: Path, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[str, ...]:
    """검토 폭 안의 untracked 신규 파일 경로 (`.gitignore` 제외 · 조회 실패는 빈 튜플).

    pathspec 을 그대로 넘겨 폭이 diff 와 같게 유지한다. 출력 경로는 cwd(=`root`) 기준 상대라
    이어지는 `--no-index` 실행과 측정 제외 판정(`is_machine_mirror_path`)이 같은 좌표를 본다."""
    _run = run_fn or subprocess.run
    result = _run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z",
         "--", *paths],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return ()
    return tuple(entry for entry in (result.stdout or "").split("\0") if entry)


def _untracked_diff_runs(
    root: Path, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, extra_args: Sequence[str] = (),
) -> list[subprocess.CompletedProcess]:
    """untracked 신규 파일 각각의 '새 파일 diff' 실행 결과 (index 미변경).

    `git diff` 는 index/커밋에 있는 것만 보므로 신규 파일이 통째로 빠진다 — 리뷰는 새 파일을 못
    보고, 완료 기록의 사설 참조 검사는 재고 나서 stage 하는 순서라 신규 파일의 줄을 보지 못한다."""
    _run = run_fn or subprocess.run
    runs: list[subprocess.CompletedProcess] = []
    for path in _untracked_paths(root, paths, run_fn):
        result = _run(
            ["git", "-C", str(root), "diff", "--no-index", *extra_args, "--",
             _DEV_NULL_DIFF_OPERAND, path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == _NO_INDEX_DIFFERENCES_RC:
            result = subprocess.CompletedProcess(
                result.args, 0, result.stdout, result.stderr)
        runs.append(result)
    return runs


def _stage_diff_runs(
    root: Path, stage_base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, extra_args: Sequence[str] = (), untracked: bool | None = None,
) -> list[subprocess.CompletedProcess]:
    """한 diff 단계를 이루는 git 실행 결과 — 'HEAD' 단계만 스테이징/언스테이징+untracked 다.

    `extra_args` 는 같은 폭을 **다른 형식**으로 뽑는 데 쓴다(문맥 없는 diff). 폭 자체(단계 표)는
    한 곳이 정하고 형식만 갈리게 해, 리뷰가 본 diff 와 사설 참조 검사가 본 diff 가 어긋나지 않게 한다.

    untracked 는 기본적으로 **작업트리 단계('HEAD')에만** 붙는다 — 명시 base·폴백 커밋 단계는
    커밋 사이의 폭이라 아직 커밋되지 않은 신규 파일이 그 폭의 구성원이 아니다. `untracked` 를
    명시하면 그 판정을 호출부가 소유한다: claim 앵커 단계(`git diff <claim 시점 rev>`)는 커밋
    이름을 base 로 쓰지만 비교 대상이 **현재 작업트리**라 신규 파일도 그 폭의 구성원이다.
    """
    _run = run_fn or subprocess.run
    arg_sets = (("--cached",), ()) if stage_base == "HEAD" else ((stage_base,),)
    runs = [
        _run(["git", "-C", str(root), "diff", *extra_args, *args, "--", *paths],
             capture_output=True, text=True, encoding="utf-8", errors="replace")
        for args in arg_sets
    ]
    include_untracked = stage_base == "HEAD" if untracked is None else untracked
    if include_untracked:
        runs.extend(
            _untracked_diff_runs(root, paths, run_fn, extra_args=extra_args))
    return runs


def _canonical_measure_path(path: str) -> str:
    """측정 판정 전 표기 변형을 POSIX 한 좌표로 모은다(`repo_coordinates` 정규화와 동형)."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


# ── 어댑터층 기계 mirror — engine.manifest `@source=` 방향에서 파생 ────────────────
# 판정 입력은 그 타깃 engine.manifest 항목 하나뿐이다(새 설정 키·CLI 플래그 0). 항목이
# bare(마커 없음)면 관례상 소스는 같은 상대경로를 **레포 루트**에서 읽는다는 뜻이라(`manifest_rules.
# _source_root_rel` 동형) templates/<타깃>/ 접두가 없는 다른 물리 경로를 가리키므로 늘 진짜 교차
# 위치 복사다. `@source=<path>` 항목은 그 경로가 명시 소스다. 두 경우 다 "그 항목이 가리키는 실제
# 소스 경로가 이 파일 자신의 templates/<타깃>/ 물리 경로와 같은가"만 보면 된다 — 같으면(자기참조)
# `pm_update`가 이 파일을 재생성하지 않는다는 뜻이라(no-op self-copy) 그 flavor 가 손으로 관리하는
# canonical 원본이고(예: `templates/codex/.agents/skills/pm-dev-delegate/SKILL.md` — 각 flavor 가
# 자기 소스를 갖는 자리라 제외 대상이 아니다), 다르면 다른 위치(대개 레포 루트의 canonical)에서
# 기계로 복사돼 들어온 mirror 다. manifest 미등재 경로(CLAUDE.md·AGENTS.md·local.conf·설정 파일 등
# 인스턴스/flavor 전용 문서)는 애초에 이 판정에 들어오지 않아 손작업으로 남는다. 새 타깃이
# `templates/<신규>/.project_manager/engine.manifest` 를 갖추면 이 로직은 그 파일 이름만으로
# 갈리므로 패턴을 손보지 않아도 따라온다.
_TEMPLATES_FLAVOR_RE = re.compile(r"^templates/([^/]+)/(.+)$")

# flavor 이름 → manifest 항목 리스트(또는 manifest 부재 시 None). flavor 당 1회만 읽고 재사용한다 —
# 측정 제외 판정이 diff 경로마다 호출하므로 매번 재파싱하면 큰 diff에서 I/O 가 선형으로 늘어난다.
_adapter_manifest_entries_cache: dict[tuple[Path, str], list | None] = {}


def _adapter_flavor_manifest(home, flavor: str) -> list | None:
    """`<flavor>` 출하 manifest 항목 (flavor 당 1회 로드·캐시). 부재는 None, 파싱 실패는 전파."""
    cache_key = (Path(home.root), flavor)
    if cache_key not in _adapter_manifest_entries_cache:
        manifest_path = (
            home.root / "templates" / flavor / ".project_manager" / "engine.manifest"
        )
        try:
            entries = manifest_store.read_manifest(manifest_path)
        except FileNotFoundError:  # 부재는 그 flavor 판정만 보류.
            entries = None
        _adapter_manifest_entries_cache[cache_key] = entries
    return _adapter_manifest_entries_cache[cache_key]


def _adapter_layer_machine_mirror(home, canonical_path: str) -> bool:
    """`templates/<타깃>/`의 `.project_manager/` 밖 경로 — manifest `@source=` 방향으로 판정.

    가장 구체적인(깊은) manifest 항목을 소유자로 삼는다 — 디렉토리 항목(`.claude/skills`) 위에
    파일 단위 override(`.claude/skills/pm-dev-delegate/SKILL.md @source=...`)가 있으면 override
    가 이긴다(동률은 나중 선언이 더 구체적이라고 본다). 소유 항목이 없으면(미등재) 손작업(False).

    ``@target-owned`` 항목은 **source 방향 비교 전에** 손작업으로 접는다(F-001
    리뷰) — 그 마커는 "upstream 에 source 가 아예 없는 게 정상"이라는 명시 선언이라
    (`manifest_rules.TARGET_OWNED_TAG`), bare 경로의 관례적 "레포 루트가 source" 폴백을 이 항목에
    적용하면 실재하지 않는 교차 위치와 비교해 기계 mirror로 오분류한다 — 수기 소유 파일이
    측정에서 통째로 빠져 사설 참조 검사를 우회하는 방향이라(과다 제외) 이 판정의 불변식(과다
    제외 0)을 정면으로 어긴다."""
    match = _TEMPLATES_FLAVOR_RE.match(canonical_path)
    if match is None:
        return False
    flavor, relpath = match.group(1), match.group(2)
    entries = _adapter_flavor_manifest(home, flavor)
    if not entries:
        return False
    relpath_norm = relpath.strip("/")
    owner = None
    owner_depth = -1
    for entry in entries:
        dest = str(entry).replace("\\", "/").strip("/")
        if relpath_norm == dest or relpath_norm.startswith(dest + "/"):
            depth = dest.count("/")
            if depth >= owner_depth:
                owner = entry
                owner_depth = depth
    if owner is None:
        return False
    if getattr(owner, "target_owned", False):
        return False
    dest_physical = f"templates/{flavor}/{str(owner)}".replace("\\", "/").strip("/")
    source_physical = (
        getattr(owner, "source_rel", None) or str(owner)
    ).replace("\\", "/").strip("/")
    return source_physical != dest_physical


def is_machine_mirror_path(home, path: str) -> bool:
    """완료 기록 측정의 **측정 제외 subtree**인가.

    정책은 pm_update 엔진 사본인 `templates/<타깃>/.project_manager/` 아래와 패키지 설치
    산출물인 `.opencode/node_modules/` 아래, 그리고 그 타깃 engine.manifest 가 `@source=` 방향으로
    선언한 어댑터층 경로(`_adapter_layer_machine_mirror`)를 제외한다. template subtree 의
    파일 전부는 아니다 — manifest 밖 손편집 파일도 있어 통째 제외하면 과소 측정이 생기지만,
    오차의 방향이 **측정 축소**라 정당한 작업을 오차단하지 않는다.

    **측정 제외 규칙의 단일 진실** — 완료 기록(ticket_finish)의 사설 참조 표면 해소가 이 판정을
    쓴다(사본 0). 판정은 경로 문자열뿐이라 트리 상태에 의존하지 않는다."""
    canonical = _canonical_measure_path(path)
    if reviewer_budget._MACHINE_MIRROR_RE.match(canonical) is not None:
        return True
    return _adapter_layer_machine_mirror(home, canonical)


# ── 완료 기록 측정 폭 (앵커) ────────────────────────────────────────────
# 완료 기록의 사설 참조 검사가 재야 하는 것은 "이 작업이 통합 브랜치에서 갈라진 뒤 남긴 변경"이다.
# 작업트리+직전 커밋 한 칸이라는 옛 폭은 dev 브랜치를 `--no-ff` merge 로 흡수하는 형상에서
# 0 에 수렴한다 — 완료 기록 시점 트리는 clean 이고 마지막 커밋이 전파/기록 커밋이면 경로
# 교집합이 비기 때문이다(실측).
#
# 기준점은 `merge-base(<통합 브랜치>, HEAD)` 다. 그 값은 커밋·재배치 순서와 무관하게 같으므로
# 폭이 조작 순서의 함수가 되지 않고, 재배치로 흡수한 남의 커밋은 그 기준점의 조상이라 폭에서
# 빠진다(claim 시점 rev 를 기준점으로 쓰면 그 흡수분이 자기 폭으로 실렸다·실측). 통합 브랜치를
# 선언하지 않았거나 이 트리에서 해소되지 않으면 **멈춘다** — 기준 없이 잰 폭은 그 자체가 판정
# 불능이고, 다른 기준으로 접으면 같은 게이트가 실행마다 다른 것을 재게 된다.

# merge-base 산출은 git argv 를 타고 오는 값이라 형태를 확인한 뒤에만 앵커로 쓴다(경고 한 줄이
# 값으로 둔갑하지 않게 한다). git 이 내는 값은 40자 sha 다.
_MERGE_BASE_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

INTEGRATION_TIP_UNRESOLVED_NOTE = (
    "통합 브랜치 {ref} 를 이 트리에서 해소하지 못했다 — 측정 폭의 기준점이 없다. 장부가 선언한 "
    "통합 브랜치가 이 트리에 있는지 확인하라."
)


def integration_anchor(
    root: Path, integration_ref: str,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[str | None, str | None]:
    """측정 폭의 기준점 `merge-base(<통합 브랜치>, HEAD)` — `(앵커, None)` 또는 `(None, 사유)`.

    이 값이 폭의 기준점이면 커밋·재배치가 언제 일어나도 폭이 같다: 통합 브랜치가 이미 가진
    커밋은 이 기준점의 조상이라 빠지고, 이 작업이 갈라진 뒤 남긴 것만 남는다.

    해소 실패는 사유를 돌려 호출부가 **멈추게** 한다. 다른 기준점으로 접으면 `git diff <rev>`
    가 실패한 단계를 '변경 없음'으로 접는 경로와 만나 **조용한 0 줄**(false-green)이 되고,
    무엇보다 같은 게이트가 실행마다 다른 폭을 재게 된다."""
    _run = run_fn or subprocess.run
    try:
        result = _run(
            ["git", "-C", str(root), "merge-base", integration_ref, "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None, INTEGRATION_TIP_UNRESOLVED_NOTE.format(ref=integration_ref)
    merge_base = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not _MERGE_BASE_RE.match(merge_base):
        return None, INTEGRATION_TIP_UNRESOLVED_NOTE.format(ref=integration_ref)
    return merge_base, None


def _measure_stages(
    base: str, anchor: str | None,
) -> tuple[tuple[str, bool], ...]:
    """완료 기록 측정의 단계 표 — `(git diff 기준, untracked 포함 여부)`.

    앵커(`integration_anchor` 가 고른 기준점 — 통합 브랜치와의 merge-base)가 있고
    base 가 기본('HEAD')이면 단계는 하나다: `git diff <앵커>` 는 그 커밋 트리와 **현재
    작업트리**를 비교하므로 기준점 이후의 커밋·스테이징·언스테이징이 한 폭에 들어온다.
    untracked 신규 파일도 작업트리 구성원이라 함께 본다(stage 전 신규 파일이 측정에서
    빠지는 창을 닫아 둔다).

    `--base` 를 명시하면 그 폭이다 — 사용자가 고른 폭을 앵커가 덮어쓰지 않는다(앵커 없는
    호출은 그 명시 폭 하나뿐이다 · 기본 폭에서 기준점을 못 고르면 호출부가 거부한다)."""
    if anchor and base == "HEAD":
        return ((anchor, True),)
    return tuple((stage, stage == "HEAD") for stage in _diff_bases(base))


def measured_diff_text(
    root: Path, base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, claimed_rev: str | None = None,
) -> str:
    """완료 기록 측정 폭의 0-컨텍스트(`--unified=0`) unified diff 원문 — 컨텍스트 없는 라인
    번호가 필요한 소비자(완료 기록의 사설 참조 preflight)용.

    폭의 단일 정의는 `_measure_stages`뿐이고 여기서 사본을 두지 않는다 — 형식만 `extra_args`로
    갈린다(`_stage_diff_runs`)."""
    for stage_base, untracked in _measure_stages(base, claimed_rev):
        text = "".join(
            result.stdout
            for result in _stage_diff_runs(
                root, stage_base, paths, run_fn, extra_args=("-U0",),
                untracked=untracked,
            )
            if result.returncode == 0
        )
        if text.strip():
            return text
    return ""


def _stage_diff_text(
    root: Path, stage_base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, strict: bool = False,
) -> str:
    """한 diff 단계의 원문(성공한 실행분만 이어붙임).

    strict 면 실패한 git 실행을 RuntimeError 로 올린다 — 명시 base 의 오타/미존재 리비전은
    조용한 빈 diff 가 아니라 오류여야 한다. 폴백 단계와 후보 판정은 실패를 '이 단계에는 변경
    없음'으로 본다(그 단계가 없는 repo 도 정상 형상).
    """
    runs = _stage_diff_runs(root, stage_base, paths, run_fn)
    if strict:
        for result in runs:
            if result.returncode != 0:
                raise RuntimeError(
                    f"git diff 실패 (rc={result.returncode}): {result.stderr.strip()}"
                )
    return "".join(result.stdout for result in runs if result.returncode == 0)


class ReviewDiff(NamedTuple):
    """선택된 변경의 내부 본문과 리뷰어가 읽을 좌표."""

    text: str
    base: str
    target: str
    paths: tuple[str, ...]
    read_commands: tuple[tuple[str, ...], ...]


def extract_diff(
    home, base: str,
    paths: list[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> ReviewDiff:
    """기존 비교 단계에서 본문을 측정하고 같은 실행의 읽기 좌표를 반환한다."""
    runner = run_fn or subprocess.run

    def run(argv, **kwargs):
        result = runner(argv, **kwargs)
        if argv[3] == "ls-files" and result.returncode != 0:
            raise RuntimeError(
                f"git ls-files 실패 (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result

    for stage_base in _diff_bases(base):
        runs = _stage_diff_runs(home.root, stage_base, paths, run)
        raw_diff = "".join(result.stdout for result in runs if result.returncode == 0)
        for result in runs:
            if result.returncode != 0 and (
                base != "HEAD" or stage_base == "HEAD" or raw_diff.strip()
            ):
                raise RuntimeError(
                    f"git diff 실패 (rc={result.returncode}): {result.stderr.strip()}"
                )
        if not raw_diff.strip():
            continue
        changed_paths: dict[str, None] = {}
        read_commands: list[tuple[str, ...]] = []
        for result in runs:
            argv = list(result.args)
            diff_index = argv.index("diff")
            names_argv = argv[:diff_index + 1] + ["--name-only", "-z"] + argv[diff_index + 1:]
            names = run(names_argv, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
            if names.returncode != 0 and not (
                "--no-index" in argv and names.returncode == _NO_INDEX_DIFFERENCES_RC
            ):
                raise RuntimeError(
                    f"git diff 파일 목록 조회 실패 (rc={names.returncode}): {names.stderr.strip()}"
                )
            entries = tuple(path for path in names.stdout.split("\0") if path)
            if result.stdout.strip() and not entries:
                raise RuntimeError("git diff 본문과 변경 파일 목록이 일치하지 않습니다")
            changed_paths.update(dict.fromkeys(entries))
            read_commands.append(tuple(
                argv[:diff_index] + ["--no-pager", "diff", "--no-ext-diff", "--no-color"]
                + argv[diff_index + 1:]
            ))
        if stage_base == "HEAD":
            target = "INDEX/WORKTREE (untracked 포함)"
        elif stage_base == _COMMIT_FALLBACK_BASE:
            target = f"HEAD (커밋 비교 {stage_base})"
        elif ".." in stage_base:
            target = f"커밋 비교 범위: {stage_base}"
        else:
            target = f"Git 비교식: {stage_base} (index/작업트리 비교 시 untracked 미포함)"
        return ReviewDiff(raw_diff, stage_base, target, tuple(changed_paths), tuple(read_commands))
    return ReviewDiff(raw_diff, stage_base, "변경 없음", (), ())
