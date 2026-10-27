"""설정 현황 텍스트·기계 JSON 절 선택과 출력 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
import datetime
import json
import subprocess
import sys
from pm_engine.app import adapter_accept
from pm_engine.app import adapter_report
from pm_engine.app import adapter_templates
from pm_engine.app import board_dashboard
from pm_engine.app import harness_ctx
from pm_engine.app import worktree_status
from pm_engine.cli import config_repo
from pm_engine.domain import conf_schema
from pm_engine.domain import install_rules
from pm_engine.domain import worktree_records
from pm_engine.infra import adapter_baseline
from pm_engine.infra import branch_store
from pm_engine.infra import conf_store
from pm_engine.infra import console
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.infra import install_receipt
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store
from pm_engine.infra import task_store
from pm_engine.infra import upstream_git
from pm_engine.infra import worktree_git
import pm_engine.app.worktree_pool


# areas.md canonical 칼럼 순서 (`areas_rules._AREAS_COLUMNS` 와 같은 순서). 구 헤더는 이 순서의
# *prefix* 라, 헤더보다 넓은 행(구 헤더에 신 canonical row 가 append 된 업그레이드 행)은 이
# 순서로 이어 매핑해 `base`/`protected`/`area_owner` 유실을 막는다(`areas_rules._areas_row_dict` 동형).
_AREAS_CANONICAL_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base",
                            "protected", "area_owner")


def _areas_rows(home) -> list[dict[str, str]]:
    """areas.md 데이터 행 → `{칼럼명: 값}` 목록 — pm_config 자체 파싱(board.py 미로드).

    `areas_store._parse_areas` 와 **같은 매핑 규칙**이다. `cmd_status`
    (정체성 posture)와 기계 조회(`status --json` 의 `repos`)가 이 파서 하나를 공유한다 — 같은
    파일을 두 번 파싱하지 않는다. 구분선(`|---|`)은 건너뛰고 첫 table row 를 헤더로 본다.
    파일 부재·읽기 실패·손상 UTF-8 은 빈 목록(fail-soft — 조회가 크래시하지 않는다).

    자리는 명시 Home 의 `pm_paths.areas_file` 이다 — 보드가 별도 저장소(`.project_manager/board/tickets`
    실재)면 그 안, 아니면 `.project_manager/areas.md`. 고정 경로로 읽던 때는 보드 분리 홈에서
    `repos` 가 늘 빈 목록이었다.
    """
    areas_file = pm_paths.areas_file(home)
    try:
        lines = fs.read_text_shared(areas_file, encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and all(c and set(c) <= {"-", ":"} for c in cells):
            continue  # 구분선(|---|:--:|) skip.
        if not header:
            header = [c.lower() for c in cells]
            continue
        if len(cells) > len(header):
            rows.append({
                (_AREAS_CANONICAL_COLUMNS[i] if i < len(_AREAS_CANONICAL_COLUMNS)
                 else f"col{i}"): cells[i]
                for i in range(len(cells))
            })
        else:
            rows.append({header[i]: (cells[i] if i < len(cells) else "")
                         for i in range(len(header))})
    return rows


def _distinct_area_owners(home) -> int:
    """areas.md 의 distinct non-empty `area_owner` 수 — 다중사용자 최소 신호.

    `cmd_status` 의 isolation posture(strict/degrade/single-user) 판정용 *coarse* 신호다. board.py 를
    import 없이 areas.md를 직접 읽는다(`_areas_rows`) — board 의 격리 *판정*(`_ticket_is_mine`·티켓
    스캔 `_distinct_ticket_users`)은 복제하지 않고, 공유 레지스트리(areas.md)의 `area_owner` 칼럼만
    헤더-인식으로 센다. 실 strict-exclude 여부는 `board list --mine` loud-warn이
    authoritative — 여긴 정체성·모드 posture 만.

    파일/칼럼 부재·파싱 실패는 0(fail-soft·단일 사용자 취급·크래시 0).
    """
    return len({owner for owner in (row.get("area_owner") for row in _areas_rows(home))
                if owner})


def _slot_git_summary(status: dict, *, readonly: bool = False) -> str:
    """슬롯 git 상태 dict(`worktree_status.slot_git_status`)를 한 줄 요약으로 렌더한다.

    포맷 = `<branch>@<head 단축> (부모: <parent>@<갈라진 커밋 단축> · 앞 N · 뒤 M)`.
      - 부모 기록이 없으면 `(부모 기록 없음)`, 부모 브랜치가 지워졌으면 앞/뒤 대신 `부모 브랜치 없음`.
      - readonly 슬롯 = branch 축은 `(detached)`·부모는 따라갈 브랜치.
    """
    if readonly:
        branch_disp = "(detached)"
    else:
        branch_disp = status.get("branch") or "(detached/조회불가)"
    head_disp = worktree_records._short_sha(status.get("head"))
    parent = status["parent"]
    if parent is None:
        return f"{branch_disp}@{head_disp} (부모 기록 없음)"
    named = f"부모: {parent['name']}@{worktree_records._short_sha(parent['fork'])}"
    if parent["ref"] is None:
        return f"{branch_disp}@{head_disp} ({named} · 부모 브랜치 없음)"
    return f"{branch_disp}@{head_disp} ({named} · 앞 {parent['ahead']} · 뒤 {parent['behind']})"


# ── 기계 현황 계약 (`status --json`) ──────────────────────────────────────────
# 관리자(설치본)가 프로젝트 사본에서 현황을 읽는 **유일한 계약**이다. 새 명령을 만들지 않고
# 이미 읽기 전용인 `status` 에 기계 출력을 붙였다 — 사람 출력(텍스트)은 바이트 그대로다.
#
# 절은 비용으로 갈린다: 기본은 가벼운 절(슬롯·슬롯 없는 보존 브랜치·task·git·repo·하네스·설정)이고,
# `--with-board`(티켓·lint·게이트 기록)와 `--with-adapters`(어댑터 판정·diff·기능 표)가 무거운
# 절을 붙인다. 기본 호출은 board 집계(`app.board_dashboard.status_snapshot`)를 실행하지 않는다(티켓 파일을 읽지 않는다).
STATUS_JSON_SCHEMA = 1


def _write_machine_line(text: str) -> None:
    """기계 판독 한 줄(JSON 페이로드)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    사람 출력(`print`)은 PowerShell 캡처에서 콘솔 codepage(cp949 등)로 강등될 수 있고 그 치환은
    되돌릴 수 없다 — 다른 프로세스가 파싱하는 출력은 공용 seam 으로 UTF-8 bytes 를 직접 쓴다
    (board `_write_machine_line` 동형).
    """
    console.write_machine_line(text)


class RemoteCheckError(RuntimeError):
    """`--with-remote` 의 원격 대조를 끝내지 못했다 — 반쪽 대조를 싣지 않고 조회를 멈춘다."""


def _git_ok(git_runner: worktree_records.GitRunner, argv: list, what: str) -> str:
    """git 한 번 — rc≠0 이면 무엇을 하다 실패했는지 말하며 멈춘다."""
    rc, out = git_runner(argv)
    if rc != 0:
        raise RemoteCheckError(f"{what} 실패 (rc={rc}): {out.strip()[:300]}")
    return out.strip()


_UPSTREAM_FORMAT = "%(upstream)%09%(upstream:short)%09%(upstream:remotename)%09%(upstream:remoteref)"


def _ahead_behind(git_runner: worktree_records.GitRunner, path: str, ref: str, what: str) -> tuple[int, int]:
    """`HEAD...ref` 의 (슬롯에만 있는 커밋 수, ref 에만 있는 커밋 수)."""
    counts = _git_ok(git_runner, ["-C", path, "rev-list", "--left-right", "--count", f"HEAD...{ref}"],
                     what)
    ahead, behind = (int(part) for part in counts.split())
    return ahead, behind


def _status_json_remote(home, leases, git_runner: worktree_records.GitRunner) -> dict[str, dict]:
    """슬롯별 원격 정보 — origin 주소 · 추적 원격 브랜치(이름·끝 커밋·앞/뒤).

    `upstream` 은 git 이 슬롯 브랜치에 연결해 둔 원격 브랜치(`@{u}`)다. 브랜치 없이 커밋에 멈춘 슬롯과
    원격 브랜치에 연결되지 않은 브랜치는 null 이고, 연결은 있는데 원격에서 지워진 브랜치는
    `head`·`ahead`·`behind` 가 null 이다.

    원격마다 `ls-remote` 1회로 슬롯들의 추적 원격 브랜치가 원격에 있는지 묻고, 있는 것만 fetch 1회로
    받는다(repo·원격 당 1회 — 슬롯들이 git 저장소를 공유한다). 받는 자리를 refspec 으로 명시하므로
    저장소의 fetch 설정과 무관하다. `ahead` = 슬롯에만 있는 커밋 수, `behind` = 원격 브랜치에만 있는
    커밋 수. 네트워크·인증·origin 부재는 `RemoteCheckError` 다(빈 값으로 접지 않는다).
    """
    slots: list[tuple] = []
    wanted: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}   # (repo, 원격) → (경로, 원격 ref → 받을 자리)
    for lease in leases:
        path = str(pm_paths.slot_path(home, lease.slot))
        rc, ref = git_runner(["-C", path, "symbolic-ref", "-q", "HEAD"])
        if rc not in (0, 1):                          # 1 = 브랜치 없이 커밋에 멈춘 슬롯
            raise RemoteCheckError(f"{lease.slot}: 현재 브랜치 조회 실패 (rc={rc}): {ref.strip()[:300]}")
        upstream = None
        if rc == 0:
            fields = _git_ok(git_runner, ["-C", path, "for-each-ref", f"--format={_UPSTREAM_FORMAT}",
                                          ref.strip()], f"{lease.slot}: 추적 원격 브랜치 조회").split("\t")
            if fields[0] and fields[2] != ".":        # "." = 원격이 아닌 로컬 브랜치에 연결
                upstream = dict(zip(("ref", "name", "remote", "remote_ref"), fields))
                wanted.setdefault((lease.repo, upstream["remote"]), (path, {}))[1][upstream["remote_ref"]] = upstream["ref"]
        slots.append((lease, path, upstream))

    present: dict[tuple[str, str], set[str]] = {}     # (repo, 원격) → 원격에 있는 추적 대상 ref
    for (repo, remote), (path, targets) in wanted.items():
        listed = _git_ok(git_runner, ["-C", path, "ls-remote", remote, *sorted(targets)],
                         f"{repo}: {remote} 원격 조회")
        advertised = {line.partition("\t")[2] for line in listed.splitlines()}
        present[(repo, remote)] = set(targets) & advertised
        if present[(repo, remote)]:
            _git_ok(git_runner, ["-C", path, "fetch", remote,
                                 *(f"+{source}:{targets[source]}" for source in sorted(present[(repo, remote)]))],
                    f"{repo}: git fetch {remote}")

    rows: dict[str, dict] = {}
    for lease, path, upstream in slots:
        tracked = None
        if upstream is not None:
            tracked = {"name": upstream["name"], "head": None, "ahead": None, "behind": None}
            if upstream["remote_ref"] in present[(lease.repo, upstream["remote"])]:
                ahead, behind = _ahead_behind(git_runner, path, upstream["ref"],
                                              f"{lease.slot}: 추적 원격 브랜치 대비 커밋 수")
                tracked.update(head=_git_ok(git_runner, ["-C", path, "rev-parse", upstream["ref"]],
                                            f"{lease.slot}: {upstream['ref']} 해소"),
                               ahead=ahead, behind=behind)
        rows[lease.slot] = {
            "url": _git_ok(git_runner, ["-C", path, "remote", "get-url", "origin"],
                           f"{lease.slot}: origin 주소"),
            "upstream": tracked,
        }
    return rows


def _status_json_slots(home, leases, task_names: set,
                       remote: dict[str, dict] | None = None) -> list[dict]:
    """슬롯 축 — 리스 장부 + 슬롯당 git 요약. 판정은 전부 `worktree_pool` 것을 그대로 싣는다.

    `remote` 는 `--with-remote` 로 부른 때만 슬롯마다 원격 대조(`_status_json_remote`)이고 그
    밖에는 null 이다(네트워크를 쓰는 절이라 가벼운 조회에 넣지 않는다).

    `alive` 는 `worktree_records.lease_is_open` 그대로다(판정 재구현 0) — 열림 기록(leased +
    session)이 있고 그에 대응하는 종료 기록(release/force_release/end_task 의 idle+session 비움)이
    없으면 참이다. 프로세스 조회는 0회다. `role` 은 장부에 키가 없는 행이 있어 `work`/`readonly` 두 값으로 정규화한다 —
    표시 축이 아니라 판정 축이라 빈 값을 남기지 않는다.
    """
    rows: list[dict] = []
    repo_reads: dict = {}   # 저장소 공용 git 값(부모 기록·stash·부모 비교 ref)은 조회 1회에 한 번만 읽는다.
    for lease in leases:
        git = worktree_status.slot_git_status(home, lease.slot, repo_reads=repo_reads)
        rows.append({
            "slot": lease.slot,
            "repo": lease.repo,
            "state": lease.state,
            "session": lease.session or None,
            "alive": bool(worktree_records.lease_is_open(lease)),
            "role": getattr(lease, "role", "work") or "work",
            "task": lease.session if (lease.session and lease.session in task_names) else None,
            "branch": git.get("branch"),
            "head": git.get("head"),
            "parent": git["parent"],
            "children": git["children"],
            "stashes": git["stashes"],
            "dirty": bool(git.get("dirty")),
            "folders": git.get("folders"),
            "remote": None if remote is None else remote[lease.slot],
        })
    return rows


def _status_json_tasks(home, tasks) -> list[dict]:
    """task 축 — 정체성 + 보유 슬롯(`slots_for_task` · session 축 귀속).

    `alive` = 장부 task 행의 열림 기록(`open`) 그대로다 — 프로세스 조회 0회.
    """
    return [{
        "name": task.name,
        "prefix": getattr(task, "prefix", None) or None,
        "alive": bool(task.open),
        "started": getattr(task, "started", "") or None,
        "slots": [lease.slot for lease in (pool_store.slots_for_task(home, task.name) or [])],
        # 열린 todo — 관리자 화면이 task 줄에 그대로 그린다(가벼운 절이라 5초 주기로 갱신된다).
        # task 별 파일(`.local/tasks/<task>/todo.json`)이 출처다.
        "todos": [dict(todo) for todo in task_store.read_task_todos(home, task.name)],
    } for task in tasks]


def _status_json_git(home, git_runner: worktree_records.GitRunner) -> dict:
    """PM 홈 checkout 의 git 요약 — branch·head·dirty.

    슬롯 축(`slots[].branch`)과 다른 축이다 — 이건 board·wiki·장부를 담은 **이 사본 자신**이고,
    회귀·라이브게이트 기록이 어느 HEAD 에 대한 판정인지 대조하는 기준이다. git checkout 이
    아니면 세 값 모두 None(그 사실이 곧 신호다).

    브랜치는 `worktree_git.current_branch` 와 **같은 primitive** 하나로 읽는다 —
    `symbolic-ref HEAD`(full ref·`--short` 없이) rc 0 이면 `refs/heads/` 접두만 정확히 벗겨
    브랜치명, rc≠0 이면 detached 라 None 이다. 두 축이 같은 상태를 같은 값(null)으로 말해야
    읽는 쪽이 판정을 다시 구현하지 않는다. `rev-parse --abbrev-ref HEAD` 를 쓰지 않는 이유는
    `current_branch` docstring 에 기록된 실측 사고 그대로다 — detached 를 문자열 `"HEAD"` 로,
    브랜치명과 동명의 태그가 있으면 `heads/<branch>` 으로, 커밋 0 인 브랜치는 rc≠0 으로 준다.

    커밋 0 인 저장소(`pm_import --new` 직후 fresh clone)는 정상 형상이다 — `symbolic-ref` 가
    `refs/heads/<branch>` 을 rc 0 으로 주므로 브랜치명은 있고 `head` 만 None 이다
    (`rev-parse HEAD` 가 rc≠0 · `pm_bootstrap._collect_git` 과 같은 규약).
    """
    rc, ref = git_runner(["-C", str(home.root), "symbolic-ref", "HEAD"])
    branch: str | None = None
    if rc == 0 and ref.strip().startswith(worktree_git._SYMREF_BRANCH_PREFIX):
        branch = ref.strip()[len(worktree_git._SYMREF_BRANCH_PREFIX):] or None
    head_rc, head_out = git_runner(["-C", str(home.root), "rev-parse", "HEAD"])
    dirty_rc, dirty_out = git_runner(["-C", str(home.root), "status", "--porcelain"])
    return {
        "branch": branch,
        "head": head_out.strip() if head_rc == 0 else None,
        "dirty": bool(dirty_out.strip()) if dirty_rc == 0 else None,
    }


def _status_json_repos(home) -> list[dict]:
    """등록 repo — areas.md 레지스트리 행(`repo list` 와 같은 칼럼·빈 셀은 null)."""
    return [
        {column: (row.get(column) or None) for column in config_repo._REPO_LIST_COLUMNS}
        for row in _areas_rows(home) if row.get("repo")
    ]


def _source_missing_required(source_root: Path, values: dict) -> list[str]:
    """**소스** 레지스트리 기준 필수 누락 — "갱신하면 필요" 축.

    비교 기준은 소스 checkout 의 레지스트리다(이 사본의 선언이 아니라 갱신하면 오게 될 선언).
    이 프로세스에는 이 사본의 `pm_engine` 이 이미 적재돼 있어 소스 패키지를 import 하면 이 사본
    선언을 읽게 된다. 그래서 소스의 진입 파일 `local_conf.py --keys --json` 을 하위 프로세스로
    돌려 그 키 표에서 `kind == "key"` 이고 `required` 가 참인 키를 선언 순서로 모으고 `values` 에
    없는 것을 돌려준다(`conf_schema.missing_required` 와 같은 판정). 하위 프로세스 실패·JSON 파싱
    실패·필드 부재는 그대로 올라간다(비교 못 하는 상태를 빈 목록으로 접지 않는다).

    **신뢰 경계**: 이 호출은 소스 트리의 Python 을 **실행**한다 — 채택자가
    `upstream.path` 로 등록한 checkout(URL 이면 이 실행이 clone/fetch 한 캐시) 한정이고,
    그 트리의 엔진은 `pm-config update` 가 이미 복사·실행하는 것과 같은 경계다.
    """
    entry = Path(source_root) / ".project_manager" / "tools" / "local_conf.py"
    result = subprocess.run(
        [sys.executable, str(entry), "--keys", "--json"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"소스 키 표를 읽지 못했다 ({entry} --keys --json · rc={result.returncode}): "
            f"{result.stderr.strip()}")
    document = json.loads(result.stdout)
    return [row["key"] for row in document["keys"]
            if row["kind"] == "key" and row["required"] and row["key"] not in values]


def _status_json_conf(home, source_root: Path | None) -> dict:
    """설정 완전성 — 필수 누락·폐지 키·미지 키·표준과 다른 값·최신 기준 누락 다섯 목록.

    판정은 `conf_schema` 레지스트리 하나가 낸다(값 해석 사본 0). 압축 한도(`ctx_budget`)는 기록과
    도구 설정 파일을 함께 읽는 `harness_ctx.ctx_budget_status` 가 낸다. `missing_for_latest` 는 소스
    checkout 이 해소된 호출(`--from` 또는 `--with-adapters`)에서만 채워지고 그 밖에는 null 이다.
    """
    result = conf_store.load(home.local_conf)
    values = result.values
    return {
        "ctx_budget": harness_ctx.ctx_budget_status(home.root, values),
        "missing_required": list(conf_schema.missing_required(values)),
        "legacy": [{"key": key, "replacement": replacement}
                   for key, replacement in sorted(result.legacy.items())],
        "unknown": list(result.unknown),
        "nonstandard": [{"key": key, "value": value, "default": default}
                        for key, (value, default) in sorted(conf_schema.nonstandard(values).items())],
        "missing_for_latest": (None if source_root is None
                               else _source_missing_required(source_root, values)),
    }


def _status_json_adapters(home, source_root: Path) -> dict:
    """어댑터 파일별 판정·diff 둘·기능 표 + 문서 파일(검사 없음) 한 줄.

    판정 어휘(`mode`·`status`)는 엔진 값 그대로다 — 표시 라벨은 읽는 쪽(관리자) 몫이라 여기서
    번역하지 않는다. 문서 파일(`CLAUDE.md`·`AGENTS.md`)은 채널이 없어 검사 대상이 아니고,
    그 사실 자체를 한 줄로 싣는다(목록에서 빠지면 "없는 파일" 과 구분되지 않는다).
    """
    files: list[dict] = [{
        "path": report.relpath,
        "harness": report.harness,
        "mode": report.mode,
        "status": report.status,
        "template_rev": report.template_rev,
        "diff_lines": {"added": report.added, "removed": report.removed},
        "diff_vs_baseline": report.diff_vs_baseline,
        "diff_vs_latest": report.diff_vs_latest,
        "features": [{"name": feature.name, "project": feature.project,
                      "latest": feature.latest, "verdict": feature.verdict}
                     for feature in report.features],
        "distance": report.distance,
    } for report in adapter_report.adapter_config_reports(home.root, source_root)]
    files.extend(
        {"path": target.relpath, "ownership": "instance", "status": "unchecked"}
        for target in adapter_templates.instance_owned_template_targets(home.root, source_root)
        if target.mode == install_rules.ADAPTER_CONFIG_NO_CHANNEL
    )
    files.sort(key=lambda row: row["path"])
    return {"source": str(Path(source_root).resolve()), "files": files}


def _status_json_document(home, args, *, source_root: Path | None,
                          git_runner: worktree_records.GitRunner, engine_rev: str) -> dict:
    """현황 JSON 한 벌 — 절 순서는 계약이 아니고 키가 계약이다(스키마 번호는 상수 하나)."""
    leases = pool_store.list_leases(home)
    tasks = pool_store.list_tasks(home) or []
    task_names = {name for name in (getattr(task, "name", None) for task in tasks) if name}
    remote = (_status_json_remote(home, leases, git_runner)
              if getattr(args, "with_remote", False) else None)
    return {
        "schema": STATUS_JSON_SCHEMA,
        "engine_rev": engine_rev,
        "pm_home": str(home.root.resolve()),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "slots": _status_json_slots(home, leases, task_names, remote),
        "preserved_branches": branch_store.preserved_branches(home),
        "tasks": _status_json_tasks(home, tasks),
        "git": _status_json_git(home, git_runner),
        "repos": _status_json_repos(home),
        "harnesses": list(install_receipt.installed_harnesses(home.root)),
        "conf": _status_json_conf(home, source_root),
        "board": (board_dashboard.status_snapshot(home)
                  if getattr(args, "with_board", False) else None),
        "adapters": (None if not getattr(args, "with_adapters", False)
                     else _status_json_adapters(home, source_root)),
    }


def _emit_status_json(home, args, *, engine_rev: str) -> int:
    """`status --json` — 문서를 **전부 만든 뒤** 한 번에 낸다(부분 산출 없음).

    소스가 필요한 절(`--with-adapters`·`--from`)에서 소스를 못 잡으면 stdout 에 아무것도 쓰지
    않고 rc≠0 이다 — 반쪽 JSON 을 파싱한 관리자가 "그 절이 비어 있다" 를 정상으로 읽는 것이
    이 계약이 막으려는 상태다(텍스트 경로 `sync-adapter-config` 의 `[중단]` + rc 1 과 같은 판정).
    """
    source_root: Path | None = None
    if getattr(args, "with_adapters", False) or getattr(args, "source", None):
        try:
            source_root = adapter_accept.resolve_adapter_config_source(
                home.root, getattr(args, "source", None))
        except FileNotFoundError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        except upstream_git.UpstreamCacheError as exc:
            # URL upstream 캐시 clone/fetch 실패 — 옛 캐시로 진행하지 않고 조회를 멈춘다.
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
    try:
        document = _status_json_document(
            home, args, source_root=source_root,
            git_runner=git._real_clone_runner(), engine_rev=engine_rev)
    except (FileNotFoundError, adapter_baseline.AdapterConfigChannelUnavailable) as exc:
        # 판정 기준이 없는 상태 둘만 rc 로 번역한다 — 소스에 대조할 파일이 없거나(잘못된 `--from`)
        # 어댑터 판정 채널이 unavailable 인 경우. 그 밖의 실패는 삼키지 않는다(정상 산출로 위장 금지).
        print(f"[중단] 현황 JSON 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except RemoteCheckError as exc:
        print(f"[중단] 원격 대조 실패 — {exc}", file=sys.stderr)
        return 1
    _write_machine_line(json.dumps(document, ensure_ascii=False))
    return 0


def _status_json_usage_error(args) -> str | None:
    """`--json` 없이 준 범위 플래그 — usage error 문구(없으면 None).

    조용히 무시하면 관리자가 "무거운 절을 받았다" 고 오독한다. 텍스트 경로는 이 플래그들의
    소비 지점이 아니므로 받는 척하지 않는다.
    """
    given = [flag for flag, attribute in (
        ("--with-board", "with_board"), ("--with-adapters", "with_adapters"),
        ("--with-remote", "with_remote"), ("--from", "source")) if getattr(args, attribute, None)]
    if not given or getattr(args, "json", False):
        return None
    return (f"[중단] {' · '.join(given)} 은 `--json` 과 함께만 쓴다 — 텍스트 출력에는 그 절이 "
            "없다. `pm-config status --json " + " ".join(given) + "` 로 다시 실행하라.")


def cmd_status(home, args: argparse.Namespace, *, engine_rev: str) -> int:
    """`status | whoami` — 2축 cockpit(task 상황 + slot 풀·슬롯당 git 요약).

    **2축 cockpit** — 다슬롯 관리 부담을 한눈에 보이게 두 축으로 나눈다:
      - **task 축** — 사람이 명명한 task 별 {보유 작업공간(`work/<repo>_<N>` 목록)·prefix}
        (`list_tasks`+`slots_for_task`). slot 축과 깔끔히 분리된다.
      - **slot 풀 축** — 슬롯별 {state·보유 task·role} + **슬롯당 git 요약**
        (`<branch>@<head> (base: <b>@<sha> · N behind)`·`slot_git_status`). behind 는 base 기록
        (`git.base`)이 있을 때만·미기록은 `-`+이유(자동 추론 금지).

    브랜치는 `worktree_git.current_branch(slot)` 로 슬롯 worktree 의 git HEAD 에서 **live**
    조회한다(git=진실). 사용자가 슬롯서 직접 `git
    checkout` 해도 즉시 반영·드리프트 없음. detached/조회불가는 "(detached/조회불가)".

    status·whoami 는 같은 데이터·같은 핸들러 — whoami 는 이 세션 줄을 머리에 둔다. 데이터는
    전부 장부(기계)에서 파생([[living-truth-docs-anti-ossification]]) — 신설 저장소 없음. 전
    surface(task/slot/git)의 조회 실패는 전파한다.

    `status --json` 은 같은 수집을 **기계 계약**으로 낸다(`_emit_status_json` — 관리자 읽기
    표면). 텍스트 출력은 그 플래그가 없을 때 종전 바이트 그대로다. `--json` 은 `status` 전용이라
    `whoami` 는 텍스트 별칭으로 남는다(계약 하나).

    경로는 명시 Home 에서 해소하고, `engine_rev` 는 JSON 문서의 엔진 세대 값 필드에만 쓴다.
    """
    usage_error = _status_json_usage_error(args)
    if usage_error is not None:
        print(usage_error, file=sys.stderr)
        return 2                              # 인자 오용 — argparse 와 같은 종료코드.

    if getattr(args, "json", False):
        return _emit_status_json(home, args, engine_rev=engine_rev)
    leases = pool_store.list_leases(home)
    sess = lease_store.session_name(home)
    mine = [l for l in leases if l.state == "leased" and l.session == sess]

    # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회한다
    # git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영.
    # detached/조회불가는 None → "(detached/조회불가)" 로 surface.
    def _live_branch(slot: str) -> str:
        return worktree_git.current_branch(home, slot) or "(detached/조회불가)"

    print(f"# pm-config {args.command} — 세션: {sess or '(비바인딩)'}")

    # 정체성·세션격리 posture surface: resolved user + isolation
    # 상태(strict/degrade/single-user) + remedy. board.py 를 import 하지 않고
    # user 는 `_default_user`(자체 해소·`_local_conf_user` 동형), 다중사용자 여부는 areas.md
    # `area_owner` 자체 파싱(`_distinct_area_owners`)으로 판정한다 — board 의 격리 *판정*은 복제
    # 하지 않고 최소 신호만.
    #
    # **정직화(should-fix·오안심 방지)**: 여기 posture 는 areas.md `area_owner`(registry) *coarse*
    # 신호다. board `list --mine` 의 실 strict-exclude 는 티켓 귀속(created_by/claimed_by·
    # `_distinct_ticket_users`)으로 판정하며 — 부분마이그레이션 보드(티켓은 2명·area_owner 미채움/
    # 1개)에서 두 신호가 갈린다(그게 degrade-risk 케이스). 그래서 무조건 "정상"
    # 단언을 금하고, 신호 출처·한계를 문구에 노출하고 authoritative 신호로 `board list --mine`
    # (strict-exclude loud-warn)을 가리킨다([[robustness-value-connections-before-ship]] silent-degrade
    # 근절 취지). board 티켓 스캔 복제는 하지 않는다.
    resolved_user = lease_store.user_name(home)
    multi_user = _distinct_area_owners(home) > 1
    _remedy = "`board init --owner <you>` 또는 `board migrate-identity --task <task>`"
    _authoritative = "실 격리는 `board list --mine`(strict-exclude loud-warn)이 authoritative"
    print(f"## 정체성(user): {resolved_user or '(미해소 — local.conf identity.user= / git config user.email 미설정)'}")
    if not multi_user:
        print("## 세션격리(registry/area_owner 기준): single-user (단일/미등록 registry) — 단, 티켓 귀속"
              "(created_by/claimed_by)이 다중이면 세션 뷰가 strict-exclude 될 수 있다. "
              f"{_authoritative}.")
    elif resolved_user is not None:
        print("## 세션격리(registry/area_owner 기준): strict (다중사용자·정체성 해소 — 세션 뷰 = "
              f"내 소유 open + claim). {_authoritative}.")
    else:
        print("## 세션격리(registry/area_owner 기준): ⚠ degrade-risk (다중사용자·정체성 미해소 — "
              f"소유 미해소 open 이 `board list --mine` 에서 strict-exclude). remedy: {_remedy}")

    if mine:
        print("## 이 세션의 리스:")
        for l in mine:
            print(f"  - {l.slot} (repo={l.repo} · branch={_live_branch(l.slot)})")
    else:
        print("## 이 세션의 리스: (없음)")

    # task 축 — 사람이 명명한 task 별 {보유 작업공간·prefix}. list_tasks
    # + slots_for_task(session==task 명의 leased 슬롯). task 명 집합은 slot 풀 축의 "보유 task"
    # 귀속에도 쓴다. 공급자는 소유 모듈을 직접 부른다 — 있는지 더듬지 않는다.
    tasks = pool_store.list_tasks(home) or []
    # truthy name 만 — 레코드가 name 미보유(None/빈값)면 집합에 넣지 않는다(방어). None 이 섞이면
    # idle lease(session=None/"")가 `보유 task=None` 으로 오귀인될 수 있다(reviewer suggestion).
    task_names = {n for n in (getattr(t, "name", None) for t in tasks) if n}
    print("## task 상황 (사람이 명명한 작업스트림):")
    if not tasks:
        print("  (task 없음 — 명명한 task 미등록·세션은 아래 풀 축에만 나타남)")
    for t in tasks:
        slots = pool_store.slots_for_task(home, t.name) or []
        ws = ", ".join(s.slot for s in slots) or "(보유 작업공간 없음)"
        prefix = getattr(t, "prefix", None) or "없음(기본)"
        print(f"  - {t.name} · prefix={prefix} · 작업공간: {ws}")

    # 슬롯 git 요약 헬퍼 — slot_git_status를 한 줄로.
    # 조회 실패는 전파한다.
    # 저장소 공용 git 값(부모 기록·stash·부모 비교 ref)은 조회 1회에 한 번만 읽는다.
    repo_reads: dict = {}

    def _slot_git_line(slot: str, *, readonly: bool) -> str:
        status = worktree_status.slot_git_status(home, slot, repo_reads=repo_reads)
        summary = _slot_git_summary(status, readonly=readonly)
        folders = status.get("folders")
        return summary + (" · 폴더 " + ", ".join(folders) if folders is not None else "")

    print("## slot 풀 (풀 전체 리스 장부 · 슬롯별 state·보유 task·role + git 요약):")
    if not leases:
        print("  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)")
    for l in leases:
        # role(work/readonly) — readonly 공유 슬롯은 무소유(session 없음)·detached.
        role = getattr(l, "role", "work")
        role_str = f" · role={role}" if role != "work" else ""
        print(
            f"  - {l.slot} · repo={l.repo} · branch={_live_branch(l.slot)} · "
            f"state={l.state} · session={l.session or '-'} · "
            f"{'열림' if worktree_records.lease_is_open(l) else '닫힘'}{role_str}"
        )
        # 보유 task 귀속(장부 축) — lease.session 이 task 명이면 그 task 가 이 슬롯을 보유(session
        # 축). idle 슬롯(session 이 task 명 아님/빈값)은 `-`. **장부 유래라 항상 출력** —
        # slot 풀 축 명세({state·보유 task·role})는 git 요약과 분리다.
        task_disp = l.session if (l.session and l.session in task_names) else "-"
        try:
            git_line = _slot_git_line(l.slot, readonly=role == "readonly")
        except worktree_records.SlotFoldersRefused as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        print(f"      ↳ 보유 task={task_disp} · git: {git_line}")

    # 슬롯 없는 보존 브랜치 — 슬롯 제거의 삭제 판정이 남긴 브랜치(공용 git 설정 기록). 있을 때만 절을 낸다.
    preserved = branch_store.preserved_branches(home)
    if preserved:
        print("## 슬롯 없는 보존 브랜치 (슬롯 제거 때 삭제 판정이 남긴 브랜치 · 정리는 사용자):")
    for row in preserved:
        parent_part = f"부모 {row['parent']}" if row["parent"] is not None else "부모 기록 없음"
        unmerged_part = ("합쳐짐 판정 불가" if row["unmerged"] is None
                         else f"합쳐지지 않은 커밋 {row['unmerged']}개")
        print(f"  - {row['repo']} · {row['name']} · 쓰던 슬롯 {row['slot']} · "
              f"보존 {row['preserved_at']} · 사유 {row['reason']} · {parent_part} · {unmerged_part}")

    # git worktree × 장부 정합(reconcile) — 실 git worktree 와 장부를 대조해 drift 를
    # surface 한다. **조회 전용·부작용 0**: 삭제/prune/이동 안 함 — 판정·복구 안내만(자동삭제는
    # 사용자 위임·파일 삭제 원칙). reconcile 실패는 전파한다.
    #   - orphan = git worktree 존재·장부 미등록(중단된 create/수동 add 잔존·audit #2·다음 create
    #     번호 충돌·audit #4 의 근원). status 가 이걸 못 보던 게 audit #3.
    #   - stale = 장부 등록·worktree 없음(dir 삭제/prune).
    #   - incomplete = provisional("creating") — worktree add 후 확정 전 중단된 create 흔적.
    recon = pm_engine.app.worktree_pool.reconcile_worktrees(home)
    if recon.orphans or recon.stale or recon.incomplete:
        print("## ⚠ worktree × 장부 drift (조회 전용 — 자동삭제 안 함·정리는 사용자):")
        for w in recon.orphans:
            print(
                f"  - [orphan] {w.slot} · git worktree 존재·장부 미등록 "
                "(중단된 create/수동 add 잔존) — 다음 create 번호 충돌·status blind 원인"
            )
        for l in recon.stale:
            print(
                f"  - [stale] {l.slot} · 장부 등록(state={l.state})·git worktree 없음 "
                "(dir 삭제/prune) — 장부 잔여"
            )
        for l in recon.incomplete:
            print(
                f"  - [incomplete] {l.slot} · 중단된 생성(state=creating·provisional) "
                "— worktree add 후 확정 전 중단(SIGKILL 등) 가능성"
            )
        print(
            "  복구:\n"
            "    - orphan worktree(git 측·disk 에 존재·작업 있을 수 있음) → 확인 후 "
            "`git -C .repos/<repo>.git worktree remove <경로>` 로 사용자가 삭제(위임 원칙).\n"
            "    - stale/incomplete 중 worktree dir 이 사라진 dangling 장부 엔트리 → "
            "`pm-config worktree prune-stale`(안전·이미 없는 worktree 의 기록만 정리).\n"
            "    - incomplete 인데 worktree 가 남아있으면 위 `git worktree remove` 후 prune-stale."
        )
    return 0
