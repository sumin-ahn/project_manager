"""source·dest 해소·부분 전파 범위·상류 변경점 읽기 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from pathlib import PureWindowsPath
import sys
from pm_engine.domain import manifest_rules
from pm_engine.domain import upstream_rules
from pm_engine.infra import install_conf
from pm_engine.infra import manifest_store
from pm_engine.infra import retired_paths
from pm_engine.infra import shipping_targets
from pm_engine.infra import upstream_git


# ── upstream baseline↔HEAD 변경점 요약 (read-only) ─────────
# git `name-status` 코드(첫 글자) → 표시용. R(rename)·C(copy)는 첫 글자만 본다(접두).
_NAME_STATUS_LABELS = {"M": "M", "A": "A", "D": "D", "R": "R", "C": "C", "T": "T"}


def _path_under_manifest(
        home, rel_path: str, manifest: list, dest_root: Path | None = None) -> bool:
    """changed relpath 가 manifest 항목(파일=동일·디렉토리=prefix)에 속하는지 — 엔진 영향 판정.

    manifest 한 줄은 파일 또는 디렉토리(repo 루트 기준·재귀). changed 파일이 manifest 의
    파일 항목과 정확히 같거나, manifest 디렉토리 항목 *아래*(posix prefix + `/`)면 이번 동기가
    덮어쓰는 엔진 경로다. _iter_files 의 디렉토리 재귀 의미와 동형(파일은 `==`·디렉토리는
    `startswith(d + "/")`). manifest 의 @render/@target-owned 마커는 ManifestEntry 가 이미
    떼어내 path-only 값이라 `str(entry)` 로 순수 경로만 비교한다.

    판정 좌표는 **엔트리마다 하나**, 그 엔트리의 *읽기* 좌표다(`_source_root_rel` 공유) — `@source=`
    매핑 엔트리는 상류 경로(예 `.claude/agents @source=templates/claude_code/.claude/agents`),
    bare 엔트리는 dest 경로(둘이 같다). 상류 변경은 읽기 좌표로 오므로 dest 축만 보면 `@source`
    엔트리 전부가 "manifest 밖(동기 안 받음)" 으로 오분류되고, 거꾸로 매핑 엔트리에 **dest 좌표까지**
    인정하면 이번 계획이 읽지도 않는 경로(`.codex/agents/x`)를 엔진 영향으로 과분류한다. 이 함수의
    의미가 "이 상류 변경이 이번 동기에 영향을 주는가" 이므로 판정은 읽기 좌표 단일이다.

    **좌표가 맞는 것만으로는 부족하다 — 소유권까지 해소한다.** 디렉토리 항목 위에 더 구체적인 파일
    항목이 다른 source 를 공급하면(codex `.agents/skills @source=.claude/skills` 위의
    `.agents/skills/<skill>/SKILL.md @source=templates/codex/…`) 그 파일의 실제 source 는 override
    쪽이라, 상위 항목의 source 변경은 그 파일에 도달하지 않는다. plan 이 쓰는 좌표 변환·우선순위
    seam(`_remap_to_dest`→`_dest_relpath_for`→`_manifest_owner_index` — `manifest_entry_shipping_
    inventory` 와 같은 순서·판정 사본 없음)을 그대로 태워, 변경된 경로가 **그 항목의 산출로 실제
    선택될 때만** 엔진으로 분류한다. dest 레이아웃 기준을 주지 않으면 실행 Home 의 루트다.
    """
    # dest 레이아웃 리매핑 기준 — 미지정은 실행 Home 루트(self-location).
    effective_dest = Path(dest_root) if dest_root is not None else home.root
    rel_norm = rel_path.replace("\\", "/").strip("/")
    for index, entry in enumerate(manifest):
        dest_declared = str(entry).replace("\\", "/").strip("/")
        # 이 엔트리의 **읽기 좌표** 하나만 본다 — bare 는 dest 와 같고, 매핑 엔트리는 상류 경로다.
        coordinate = manifest_rules._source_root_rel(entry).replace("\\", "/").strip("/")
        if not coordinate:
            continue
        if not (rel_norm == coordinate or rel_norm.startswith(coordinate + "/")):
            continue
        shipped_rel = manifest_store._dest_relpath_for(
            manifest_rules._remap_to_dest(rel_norm, coordinate, dest_declared), effective_dest)
        if manifest_store._manifest_owner_index(manifest, shipped_rel, effective_dest) == index:
            return True
    return False


# ── `--paths` 경로 스코프 (opt-in 부분 전파) ────────────────────────────────
# 기본 sync 는 manifest 전량이라 all-or-nothing 이다 — 병렬 작업 중 한 파일만 내보내려면 엔진 밖
# 수동 복사로 우회하게 된다(그 우회가 안전 판정·render 를 통째로 건너뛴다). 이 옵션은 그 필요를
# 엔진 안으로 들여 **명시한 경로만** 전파한다. 스코프 판정은 manifest 등재분에 한정하고(미등재는
# 거부), 부분 전파이므로 "전량 흡수" 를 전제하는 후속 단계(baseline 기록·마이그레이션·훅 재설치)는
# 발화하지 않는다 — 그것들이 돌면 drift-lint 가 거짓 '최신' 을 말한다.

def _normalize_scope_paths(raw_paths) -> tuple[list[str], list[str]]:
    """`--paths` 입력을 repo 기준 posix relpath 로 정규화 — (정규화 목록, 거부 사유 목록).

    거부: 빈 값(`.` 자기참조 포함)·절대경로/드라이브·`..` 컴포넌트(스코프가 저장소 밖을 가리키면
    안 된다). **저장하는 값은 정규화 결과**다 — 원본을 저장하면 `./a/b`·`a//b` 같은 동치 표기가
    매칭에서 빗나가 "등재는 통과했는데 아무것도 전파 안 되는" 조용한 rc0 이 된다. 정규화 뒤에
    중복을 입력 순서대로 접는다(`a/b` 와 `./a/b` 는 한 항목)."""
    normalized: list[str] = []
    rejected: list[str] = []
    for raw in raw_paths:
        value = str(raw).strip().replace("\\", "/").rstrip("/")
        if not value:
            rejected.append(f"{raw!r}: 빈 경로")
            continue
        candidate = PurePosixPath(value)
        # posix anchor(`/`·UNC `//host/share`)와 Windows 드라이브(`C:x`)를 한 식으로 거른다 —
        #   백슬래시는 위에서 이미 `/` 로 통일했다.
        if candidate.is_absolute() or PureWindowsPath(value).drive:
            rejected.append(f"{value!r}: 절대경로(스코프는 repo 루트 상대여야 한다)")
            continue
        if any(part == ".." for part in candidate.parts):
            rejected.append(f"{value!r}: `..` 컴포넌트(저장소 밖 지향)")
            continue
        # `PurePosixPath` 가 `.`·중복 `/` 를 접으므로 그 산출이 곧 정규형이다(`.` 만 준 경우는
        #   parts 가 비어 빈 경로로 거부된다).
        canonical = candidate.as_posix()
        if not candidate.parts:
            rejected.append(f"{value!r}: 빈 경로(자기 참조 `.`)")
            continue
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized, rejected


def _unregistered_scope_paths(manifest: list, scope: list[str],
                              effective_dest: Path | None = None) -> list[str]:
    """manifest 어느 항목과도 겹치지 않는 요청 경로 — 거부 대상(조용한 무전파 금지).

    겹침은 양방향이다: 요청이 manifest 항목 *아래*(디렉토리 항목의 파일 하나)여도, 요청이 여러
    manifest 항목을 *담는* 상위 디렉토리여도 유효하다. 판정 대상은 세 좌표다 — manifest 의 dest
    경로, `@source` 읽기 경로, 그리고 **dest 레이아웃 리매핑 결과**(`_dest_relpath_for`). 마지막을
    빼면 board 분리 인스턴스에서 실제 관리 파일인 `board/tickets/_template.md` 가 미등재로 거부된다
    (계획은 그 좌표로 착지시키는데 게이트만 옛 좌표를 본다)."""
    declared: set[str] = set()
    for entry in manifest:
        declared.add(str(entry).replace("\\", "/").strip("/"))
        declared.add(manifest_rules._source_root_rel(entry).replace("\\", "/").strip("/"))
        if effective_dest is not None:
            declared.add(
                manifest_store._dest_relpath_for(str(entry), effective_dest).replace("\\", "/").strip("/"))
    declared.discard("")
    return [
        path for path in scope
        if not any(manifest_rules._paths_overlap(path, item) for item in declared)
    ]


def _in_scope_paths(rel: str, scope: list[str]) -> bool:
    """이 relpath 가 요청 스코프 안인가 — 요청 경로 자신이거나 요청 디렉토리 아래.

    전파 대상 판정은 **단방향**이다: 요청보다 상위 경로(요청을 담는 디렉토리)는 스코프 밖 파일까지
    끌고 오므로 대상이 아니다. 반대로 source 부재 보고는 항목 단위(디렉토리)라 양방향으로 본다
    (`_paths_overlap`) — 요청 파일을 담은 디렉토리 등재가 통째로 없으면 그 사실이 신호여야 한다."""
    rel_norm = str(rel).replace("\\", "/").strip("/")
    return any(rel_norm == path or rel_norm.startswith(path + "/") for path in scope)


# ── guest 절 엔진 행 파생 백필 ────────────────────────────────────────────────
# add-harness 가 등재하는 guest 엔진 행은 `@source=templates/<flavor>/…` 로 자기 출처를 들고 있다.
# 그 채널이 없던 세대에 설치된 채택자의 절엔 렌더물 행뿐이라 provenance 가 없고, 그 코호트의
# 엔진 파일(엔진 패키지 `pm_engine` 과 짝인 드라이버·ctx 가드 등)은 add-harness 를 다시 돌리지 않는 한 설치
# 시점 사본으로 영구 동결된다. 계획 직전에 flavor 를 해소해 upstream flavor manifest 에서 엔진 행을
# **매 실행 재파생**하면 옛 채택자도 다음 sync 한 번으로 채널을 얻고, upstream 이 flavor 에 엔진
# 파일을 새로 추가해도 재동결되지 않는다. 파생은 읽기 전용이고 기록은 apply 의 절 재부착 한 곳뿐이다.
_FLAVOR_SOURCE_PREFIX = "templates/"


def _refuse_unregistered_scope_paths(
        manifest: list, scope: list[str],
        effective_dest: Path | None = None) -> int:
    """스코프 요청 중 manifest 미등재분을 loud 거부 — 있으면 rc1, 없으면 0.

    미등재 = 오타·인스턴스 소유·아직 manifest 에 안 올린 신규 파일. **guest 절은 사유가 아니다**:
    절의 렌더물 행도 엔진 행도 update 채널이 전파하므로 계획 manifest 에 실려 있고,
    따라서 정상 스코프 대상이다(옛 "guest 절 소속이라 지정 불가" 거부는 그 채널 분리와 함께 소멸)."""
    unregistered = _unregistered_scope_paths(manifest, scope, effective_dest)
    if not unregistered:
        return 0
    for path in unregistered:
        print(f"  [미등재] {path}", file=sys.stderr)
    print(
        f"오류: --paths 경로 {len(unregistered)}개가 engine.manifest 등재분이 아니다 — pm-update 는 "
        "manifest 소유 경로만 전파한다(인스턴스 소유 파일은 대상 아님). 경로 오타이거나, 그 "
        "파일이 아직 manifest 에 등재되지 않았는지 확인하라.", file=sys.stderr)
    return 1


def _scope_validation_manifest(effective_dest: Path, source_root: Path) -> list | None:
    """스코프 **소유권 선검증**용 manifest 합 — dest 것과 source 것의 합집합(best-effort).

    실 계획 manifest 는 manifest 자기치유 판정 뒤에 해소된다. 미등재 요청이 rc1 로 끝나는데
    그 전에 쓰기가 일어나면 "거부인데 파일이 바뀐다" 가 되므로, 쓰기 전에 읽기만으로 한 번 거른다.
    이 시점 판정은 **관대해야** 한다(합집합): self-heal 이 upstream 등재분을 계획 기준으로 올리는
    경우가 있어 dest manifest 만 보면 유효 요청을 오거부한다. 좁은 판정은 계획 확정 뒤 한 번 더
    돈다. 양쪽 다 못 읽으면 None(선검증 생략 — 정규 경로가 같은 오류를 더 정확히 낸다)."""
    entries: list = []
    seen: set[str] = set()
    candidates = [
        Path(effective_dest) / ".project_manager" / "engine.manifest",
        Path(source_root) / ".project_manager" / "engine.manifest",
    ]
    found = False
    for manifest_path in candidates:
        if not manifest_path.is_file():
            continue
        try:
            parsed = manifest_store.read_manifest(manifest_path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        found = True
        for entry in parsed:
            key = str(entry).replace("\\", "/").strip("/")
            if key not in seen:
                seen.add(key)
                entries.append(entry)
    return entries if found else None


def _scope_change_candidates(rel: str, source_path, source_root: Path) -> list[str]:
    """한 변경을 스코프에 대볼 후보 경로 — dest relpath + (가능하면) source relpath.

    `@source` 리매핑 항목은 dest 와 upstream 경로가 다르다(opencode 어댑터). 둘 다 후보로 두면
    "내가 고친 파일 경로" 로 지정해도, "채택자 트리에서 보이는 경로" 로 지정해도 걸린다. 인메모리
    source(manifest 합집합)는 파일 경로가 없어 dest 후보만 남는다."""
    candidates = [str(rel)]
    if isinstance(source_path, Path):
        try:
            candidates.append(source_path.relative_to(source_root).as_posix())
        except ValueError:
            pass
    return candidates


def summarize_upstream_changes(
    home,
    checkout: Path,
    baseline: str,
    manifest: list,
    *,
    git_runner=None,
    dest_root: Path | None = None,
) -> dict:
    """upstream 로컬 checkout 의 baseline..HEAD 변경점을 read-only 로 요약한다.

    채택자가 받은 baseline(`upstream.rev`) ↔ 그 이후 upstream HEAD 에 쌓인 변경을 *이미 로컬에
    있는* checkout 에서 `git log`/`diff --name-status` 로 집계한다 — **fetch/clone 안 함**
    (네트워크 0). git 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0·config
    격리)은 upstream_git._real_upstream_git_runner 를 재사용한다(git_runner 미주입 시). 테스트는
    git_runner 를 주입해 라이브 git 0 으로 결정론을 얻는다(DI seam).

    `manifest` 는 "무엇이 엔진인가"의 판별 집합 — 호출부가 **sync 와 동일한**
    `_resolve_planning_manifest(effective_dest, source, selfheal)` 로 해소해 넘긴다(self-heal
    승격분 우선·없으면 dest 우선 로컬 해소·guest 절 채널 분리 포함). 이 함수가 자체 로드하지 않는
    이유: 엔진 영향(이번 동기가 받는 것) 분류는 실 sync 가 쓰는 manifest 와 *반드시* 일치해야 하기
    때문(source 단독은 dest 커스터마이즈/--target 에서 어긋난다). 빈 manifest → 전부 'other'
    (graceful·엔진 영향 0 보수 표시).

    `dest_root` 는 dest 레이아웃 리매핑(board 분리 인스턴스) 기준 — 미지정이면 실행 Home 루트
    (self-location)다. 분류가 좌표 변환을 태우므로 미리보기 대상 인스턴스와 같은 기준을 넘겨야 어긋나지 않는다.

    반환 dict:
      - `status`: 'ok' | 'baseline_unreachable' | 'up_to_date' | 'summary_failed'
      - `head`: HEAD commit(rev-parse HEAD) 또는 '' (실패 시)
      - `count`: baseline..HEAD commit 수 (int)
      - `engine`: [(code, path)] — manifest 항목에 속하는 변경(이번 동기가 받는 것)
      - `removed_upstream`: [(code, path)] — manifest 항목의 상류 삭제(`D`)와 rename 의 낡은
        경로(`R` 의 old path). 동기는 **지우지 않는다**(dest 잔존) — "받는 것" 과 반대 사실이라
        버킷을 가른다. rename 의 *새* 경로는 상류가 실제로 공급하므로 engine 에 남는다.
      - `other`: [(code, path)] — manifest 밖 변경(동기 안 받음)
      - `log`: [(sha, subject)] — `git log --oneline baseline..HEAD` (--log 옵션용)

    호출부(main --changes 분기)가 baseline 부재·소스 해소·HEAD==baseline 등 *상위* 게이트를
    이미 처리한 뒤 진입한다. 여기선 baseline rev 가 checkout 에서 도달 가능한지(rc)만 본다 —
    도달불가(force-push·shallow)면 status='baseline_unreachable'(호출부가 재clone 권고). log/diff
    가 rc≠0(도달가능한데도 git 호출 실패·예외)면 status='summary_failed' — 빈 결과를 "변경 0"으로
    오판하지 않게 surface 한다(codex suggestion 1·advisory 오판 금지).
    """
    runner = git_runner if git_runner is not None else upstream_git._real_upstream_git_runner()
    result: dict = {
        "status": "ok",
        "head": "",
        "count": 0,
        "engine": [],
        "removed_upstream": [],
        "other": [],
        "log": [],
    }

    # HEAD 해소 (rev-parse) — checkout 의 현재 HEAD commit.
    rc, out = runner(["-C", str(checkout), "rev-parse", "HEAD"])
    if rc == 0 and out.strip():
        result["head"] = out.strip().splitlines()[0].strip()

    # baseline 도달성 검사 — baseline commit object 가 이 checkout 에 있는지(force-push·shallow
    # 시 없을 수 있다). `cat-file -e <rev>^{commit}` rc 로 판정(네트워크 0·로컬 object DB 만).
    rc, _out = runner(["-C", str(checkout), "cat-file", "-e", baseline + "^{commit}"])
    if rc != 0:
        result["status"] = "baseline_unreachable"
        return result

    # baseline == HEAD 면 변경 0 — log/diff 모두 빈 출력이라 자연히 up_to_date 로 떨어지지만,
    # 호출부가 보통 상위에서 거른다(별도 키 비교). 여기선 log 집계로 count 를 낸다.
    # rc≠0(도달가능한데도 git 실패)은 summary_failed 로 surface(빈 결과 오판 금지·suggestion 1).
    rc, out = runner(["-C", str(checkout), "log", "--oneline", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    log_entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        log_entries.append((sha.strip(), subject.strip()))
    result["log"] = log_entries
    result["count"] = len(log_entries)
    if result["count"] == 0:
        result["status"] = "up_to_date"

    # diff --name-status baseline..HEAD — 변경 파일 목록(M/A/D/R…). 첫 토큰=코드, 둘째=경로
    # (R/C 는 `R100\told\tnew` 3필드라 마지막 필드를 새 경로로 본다). rc≠0 면 summary_failed —
    # commit 수는 났지만 파일 분류가 불가능하므로 "엔진 영향 0" 오판을 피해 surface 한다.
    rc, out = runner(["-C", str(checkout), "diff", "--name-status", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        raw_code = fields[0].strip()
        code = _NAME_STATUS_LABELS.get(raw_code[:1], raw_code[:1] or "?")
        path = fields[-1].strip() if len(fields) > 1 else ""
        if not path:
            continue
        # 상류 삭제(`D`)는 이번 동기가 *받는* 변경이 아니다 — 디렉토리 엔트리 동기는 source 만
        # 열거해(추가·갱신) 삭제를 전파하지 않으므로 dest 파일은 잔존한다. "받는 것" 버킷에
        # 실으면 출력이 사실과 반대로 오보한다.
        if code == "D":
            bucket = (
                "removed_upstream" if _path_under_manifest(home, path, manifest, dest_root) else "other"
            )
            result[bucket].append((code, path))
            continue
        # rename 은 두 좌표를 낸다 — 새 경로는 상류가 공급(engine)이고, 낡은 경로는 manifest
        # 안이었다면 dest 에 잔존한다(manifest 밖으로 나가는 rename 이 대표 사례). `C`(copy)는
        # 원본이 그대로 남으므로 낡은 좌표가 없다.
        if code == "R" and len(fields) >= 3:
            old_path = fields[1].strip()
            if old_path and _path_under_manifest(home, old_path, manifest, dest_root):
                result["removed_upstream"].append((code, old_path))
        bucket = "engine" if _path_under_manifest(home, path, manifest, dest_root) else "other"
        result[bucket].append((code, path))

    return result


def _resolve_dest(home, args) -> tuple:
    """args(--target) → (rc, dest_root). rc≠0 이면 메시지는 이미 출력됨.

    `--target` 지정 시 실행 Home 의 templates/<target>/, 아니면 None(self-location — 호출부가 실행
    Home 루트를 쓴다). 완료 프로세스는 이 값으로 자기 위치를 정하고 인계 값의 dest 와 대조한다."""
    if not args.target:
        return 0, None
    try:
        return 0, shipping_targets.resolve_target_root(home, args.target)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1, None


def _resolve_source(args, effective_dest: Path) -> tuple:
    """args(--from)·dest local.conf → (rc, source_root). rc≠0 이면 메시지는 이미 출력됨.

    우선순위는 명시 --from → local.conf upstream.path= → 에러이고, URL 은 로컬 캐시 checkout 으로
    해소한다. 해소된 upstream 이 디렉토리가 아니면 자동 진행하지 않는다(stale 가드). 완료 프로세스는
    이 해소를 다시 하지 않는다 — 전달 프로세스가 해소한 source 를 인계 값으로 받는다."""
    # 교체 안내는 **어떤 분기보다 먼저** 나간다 — dry-run·read-only·해소 실패(구표기 `upstream=`
    # 만 있는 채택자는 여기서 rc=1 로 끝난다)까지 포함해 이 실행이 무엇을 하든 채택자는 기본값
    # 변경 1줄과 키 단위 지목을 받는다. 성공 반환 뒤에 두면 통지보다 새 기본값이 먼저 활성화된다.
    # 예외는 기계 판독 모드뿐이다(`--changes --count-only`/`--log` 의 stdout 은 값 하나라
    # 안내를 실으면 파싱이 깨진다) — 그 두 모드는 파일을 배달하지 않는 조회면이다.
    if not (getattr(args, "count_only", False) or getattr(args, "log", False)):
        retired_paths.print_conf_migration_notice(effective_dest)

    # ── upstream(source) 해소 — 순서: 명시 --from local.conf upstream.path= 에러.
    #    침묵 폴백 없음. stale(부재/비-디렉토리) 경로는 자동 진행하지 않고 명확한 에러로 멈춘다.
    if args.source:
        source_root = Path(args.source).resolve()
    else:
        local_conf = effective_dest / ".project_manager" / "local.conf"
        stored = install_conf._read_local_conf(local_conf).get("upstream.path", "").strip()
        if not stored:
            print(
                "오류: upstream 미등록 — --from <checkout> 를 주거나 "
                f"{local_conf} 에 `upstream.path=` 를 등록하라 "
                "(이 프로젝트를 한 번 pm_import 하면 자동 기록된다).",
                file=sys.stderr,
            )
            return 1, None
        # upstream= 이 URL(릴리스 추적 기본값)이면 그 URL 을 복사 소스로 쓸 **로컬 캐시
        #   checkout** 으로 바꾼다(최초 clone·이후 fetch). 캐시 자리는 dest PM 홈 안
        #   `.project_manager/.local/upstream-cache/<URL 해시>/` 하나이고, 만드는 코드는 엔진에
        #   하나뿐이다(upstream_git.resolve_upstream_url_cache — 스킬·관리자·명령줄 공용). clone/fetch
        #   실패는 옛 캐시로 미끄러지지 않고 여기서 rc 1 로 끝난다.
        if upstream_rules.classify_upstream(stored) == "url":
            try:
                source_root = upstream_git.resolve_upstream_url_cache(effective_dest, stored)
            except upstream_git.UpstreamCacheError as exc:
                print(f"오류: {exc}", file=sys.stderr)
                return 1, None
        else:
            source_root = Path(stored).resolve()

    origin = ("--from" if args.source
              else f"local.conf upstream.path= ({effective_dest}/.project_manager/local.conf)")
    if not _source_dir_exists(source_root, origin):
        return 1, None
    return 0, source_root


def _source_dir_exists(source_root: Path, origin: str) -> bool:
    """stale 가드 — 해소된 upstream 이 부재/디렉토리 아님이면 명확한 에러를 내고 False.

    기존 missing-manifest(rc 2)와 구분되는 메시지·rc(=1)로 "upstream 자체가 잘못됐다"를 알린다."""
    if source_root.is_dir():
        return True
    print(
        f"오류: upstream 경로가 디렉토리가 아니거나 존재하지 않음: {source_root} "
        f"(출처: {origin}). 체크아웃이 이동/삭제됐다면 --from 으로 올바른 경로를 주거나 "
        "local.conf 의 upstream.path= 을 갱신하라.",
        file=sys.stderr,
    )
    return False


def _resolve_dest_source(home, args) -> tuple:
    """args(--target·--from) → (rc, dest_root, source_root). rc≠0 이면 메시지는 이미 출력됨.

    전달 프로세스·`--changes`·내보내기가 같은 해소(`_resolve_dest` → `_resolve_source`)를 탄다.
    성공 시 rc=0 + (dest_root[None=self-loc], source_root[디렉토리 검증 통과]), 실패 시
    rc≠0 + (None, None)."""
    rc, dest_root = _resolve_dest(home, args)
    if rc:
        return rc, None, None
    rc, source_root = _resolve_source(args, dest_root if dest_root is not None else home.root)
    if rc:
        return rc, None, None
    return 0, dest_root, source_root
