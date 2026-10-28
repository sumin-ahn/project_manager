"""설치 공통 install_conf (infra).

local.conf 키 writer(`_write_conf_keys`·`_write_conf_keys_locked`)의 파일·락·원자 교체·실효값
검증을 소유한다. 키 갱신의 텍스트 규칙은 `pm_engine.domain.conf_schema._set_conf_keys` 가 소유한다.
"""
from __future__ import annotations


from pathlib import Path
from pm_engine.domain import conf_schema
from pm_engine.infra import fs
from pm_engine.infra import install_files
from pm_engine.infra import python_env
import os
import shutil
from pm_engine.infra import conf_store
import sys

# 인터프리터 탐지는 `python_env._detect_py()` 가 단일 진실. pm_import 가 자체 탐지를 신설하지
# 않는다 — 플랫폼별 python3/python 해석을 한 곳에 둔다.


def _default_test_cmd() -> str:
    """기본 test_cmd — `python_env.default_pytest_cmd` 의 xdist 가용성 판정과 같은 명령을 사용한다.

    상수 하드코딩(`python3 -m pytest`)은 Windows 에서 깨진다(`python3`=비기능 shim 또는
    엉뚱한 Store Python). `python_env._detect_py()` 의 실행검증된
    인터프리터를 쓴다 — local.conf `runtime.py=` 와 동일 소스라 일관.
    """
    return python_env.default_pytest_cmd(python_env._detect_py())


def _force_rmtree(path: Path) -> None:
    """트리를 **실제로** 지운다 — 공용 seam(`fs.force_rmtree`)·실패는 올린다.

    `shutil.rmtree(..., ignore_errors=True)` 를 쓰지 않는다: git object·packfile 은 read-only 라
    Windows 에서 삭제가 거부되는데, 그 실패를 삼키면 부분 board·임시 clone 이 채택자 트리에 남고도
    rc=0 이 된다(`.git/modules` 잔재 실측). 부재는 성공이다(정리의 목적은 "없다").
    """
    target = Path(path)
    if not os.path.lexists(target):
        return
    fs.force_rmtree(target)


def _write_conf_keys(path: Path, updates: dict[str, str | None]) -> bool:
    """local.conf 키들을 중복 없이 atomic 기록하고 실효값을 검증한다(값 `None` 은 삭제).

    `conf_schema._set_conf_keys`(텍스트 규칙 소유자)로 모든 갱신 키를 유일화한 뒤 같은 디렉터리의
    임시파일을 원자 교체하여 부분 쓰기를 막는다. 변경이 없어도 reader와 같은 last-wins 파서로
    postcondition을 확인하며, 불일치는 성공으로 흡수하지 않고 fail-loud한다.

    읽기→교체→검증 전체가 **전 writer 공용 배타락** 안이다. 이 writer 는 커밋 전 내용을 읽고
    나중에 통째로 갈아끼우므로, 그 사이에 다른 진입이 원자적으로 append 한 결정(추가 리뷰어·
    위임 opt-in)이 교체본에 없어 사라진다 — 원자성만으로는 못 막고 같은 락을 공유해야 막힌다.
    실효값 검증도 같은 구간에 둔다(락 밖에서 다시 읽으면 남의 정상 append 를 불일치로 오판한다).

    **재진입 금지** — 호출부는 이 함수를 conf 락을 쥔 채 부르지 않는다(현 호출부는 전부 락 밖:
    pm_import 의 sync_local_conf/record_*, pm_config upstream set). 이미 락을 쥔 구간
    (install_conf.reapply_preserved_conf_keys·app.upstream_revisions.record_upstream_revs)은
    `_write_conf_keys_locked` 를 직접 부른다.
    """
    with fs.local_conf_write_lock(path):
        return _write_conf_keys_locked(path, updates)


def _write_conf_keys_locked(path: Path, updates: dict[str, str | None]) -> bool:
    """`_write_conf_keys` 의 임계 구간 본문 — 락을 **이미 쥔** 호출부 전용."""
    text = fs.read_text_shared(path, encoding="utf-8", newline="")
    new_text = conf_schema._set_conf_keys(text, updates)
    changed = new_text != text
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8", newline="")
        fs.atomic_replace(tmp, path)
    effective = conf_schema.parse(fs.read_text_shared(path, encoding="utf-8"))

    def _mismatched(key: str, value: str | None) -> bool:
        # 삭제 요청(`None`)의 실효 조건은 값 일치가 아니라 **그 키가 없음** 이다.
        if value is None:
            return key in effective
        return effective.get(key) != value.strip()

    mismatches = {
        key: (value, effective.get(key))
        for key, value in updates.items()
        if _mismatched(key, value)
    }
    if mismatches:
        detail = ", ".join(
            f"{key}: 요청={'삭제' if requested is None else repr(requested)}, 실효={actual!r}"
            for key, (requested, actual) in mismatches.items()
        )
        raise RuntimeError(f"local.conf 기록 후 실효값 불일치 — {detail}; 파일={path}")
    return changed


def backup_existing_local_conf(dest_root: Path, backup_root: Path | None) -> str | None:
    """--into 재-import 전, 기존 local.conf 가 있으면 백업하고 원본 텍스트를 반환한다.

    MF1: board.py init 은 local.conf 를 무조건 write_text 로 덮으므로, 이미 프레임워크를
    쓰던 프로젝트(재-import/업그레이드)면 기존 per-clone 설정(추가 리뷰어 대상 튜플
    `additional_reviewer.harness`/`.model`/`.reasoning` 등)이
    무백업 손실된다. local.conf 는 pm_import 의
    copy/backup 대상 트리 밖이라 CopyAction 의 백업 로직을 안 탄다 — init 호출 전 여기서
    명시적으로 백업한다.

    백업은 형제 `*.backup.<DATE>` 가 아니라 중앙 디렉토리
    `backup_root/.project_manager/local.conf` 로 라우팅한다(한 곳 원칙). local.conf 는
    보통 git-ignored(미추적)라 git-safe 아님 — 중앙 백업 유지(내용을 git 이 복원 못 함).
    backup_root=None(--new)이면 빈 디렉토리 보장이라 기존 local.conf 가 없으므로 호출되지 않음.
    중앙 경로 충돌은 _free_backup_path 로 순번 부여(원본 보존). 반환값(원본 텍스트)은
    reapply_preserved_conf_keys 가 사용자 키 재병합에 쓴다. None = 기존 local.conf 없음.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return None
    original_text = fs.read_text_shared(local_conf, encoding="utf-8")
    if backup_root is None:
        return original_text  # --new 빈 디렉토리 — 보존만(백업 위치 없음·실질 도달 안 함).
    install_files.ensure_backup_dir_ignored(dest_root)
    backup = install_files._free_backup_path(backup_root / local_conf.relative_to(dest_root))
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_conf, backup)
    rel = backup.relative_to(dest_root).as_posix()
    print(f"✓ 기존 local.conf 백업: {rel}")
    return original_text


def reapply_preserved_conf_keys(dest_root: Path, original_text: str) -> bool:
    """board.py init 이 새로 쓴 local.conf 위에, 기존 파일의 사용자 키를 재병합한다.

    MF1: board.py init 은 local.conf 를 통째로 덮으므로, init 이 *안 쓴* 사용자 키
    (추가 리뷰어 대상 튜플 `additional_reviewer.harness`/`.model`/`.reasoning`
    등)는 init 후 사라진다. 따라서 init 산출 local.conf 에 *없는* 기존 키만
    `_write_conf_keys_locked` 로 다시 얹는다. init 이 쓴 키(`runtime.py`·`test.cmd`·`project.name`·ctx 예산)는
    init/operational sync 값을 우선해 덮지 않는다. 결과: import 후 local.conf = board init 기본 + operational sync
    + 사용자 기존 설정 보존. 재병합으로 변경 시 True.

    보존은 **키 이름을 열거하지 않는 일반 규칙**이다 — `_parse_conf_keys` 가 `key=value` 를
    문자열로만 다루므로 점 표기(`additional_reviewer.harness`)든 채택자가 만든 커스텀
    `additional_reviewer.<임의>` 든 그대로 왕복한다. 사라진 이름만 예외다 — 구표기·제거 키는
    되얹지 않고 한 줄로 알린다. 신표기로 **자동 마이그레이션하지 않고**(값 추측 금지), 사용자가
    이미 쓴 튜플 값도 덮지 않는다(원문 보존).

    보존 대상 계산은 **쓰기와 같은 락 구간 안**이다. 현재 conf 를 락 밖에서 읽어 계획을 세우면
    그사이 다른 진입(추가 리뷰어 대상 튜플 기록 등)이 남긴 새 결정이 "현재 conf 에 없는 키" 로
    남아, 백업에 있던 **옛 값이 새 결정을 덮는다**(예: 백업 `additional_reviewer.model=old` 가
    방금 기록된 값을 되돌린다). 백업 텍스트 파싱은 대상 conf 와 경쟁하지 않으므로 락 밖이다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        # conf 자체가 없는 형상에 락 파일을 만들지 않는 값싼 단축(app.upstream_revisions.record_upstream_revs
        # 와 같은 규약) — "쓰지 않는다" 만 결정하고 어떤 write 계획의 입력도 아니다. 권위 판정은
        # 락 안에서 다시 한다.
        return False
    original_keys = conf_schema.parse(original_text)  # 대상 conf 와 무경쟁 — 락 밖.
    legacy = conf_schema.is_legacy_key
    with fs.local_conf_write_lock(local_conf):
        if not local_conf.is_file():
            return False
        current_keys = conf_schema.parse(fs.read_text_shared(local_conf, encoding="utf-8"))
        # board init 이 새로 쓴 local.conf 에 *없는* 기존 사용자 키만 복원(init 값 우선).
        # 사라진 이름(`conf_schema.LEGACY_KEY_MAP`)은 되얹지 않는다 — 재병합이 그것을 되살리면
        # 그 clone 은 다음 실행부터 구표기 정지로 멈춘다. 판정 원장은 로더 하나다.
        missing = {key: value for key, value in original_keys.items()
                   if key not in current_keys}
        preserved = {key: value for key, value in missing.items() if not legacy(key)}
        dropped = sorted(key for key in missing if legacy(key))
        # 락을 이미 쥐었으므로 임계 구간 본문을 직접 부른다(`_write_conf_keys` 재호출 = 재진입).
        changed = _write_conf_keys_locked(local_conf, preserved) if preserved else False
    if dropped:
        # 조용히 지우지 않는다 — 채택자가 자기 파일에서 무엇이 빠졌는지 이 줄로 안다.
        print(f"✓ 백업의 사라진 키는 재병합하지 않음(제거): {'·'.join(dropped)}")
    if changed:
        kept = "·".join(sorted(preserved))
        print(f"✓ 기존 local.conf 사용자 키 보존: {kept}")
        return True
    return False


def _instance_project_name(dest_root: Path) -> str:
    """라이브 인스턴스의 프로젝트 이름을 local.conf `project.name` 에서 읽는다(없으면 디렉토리명 폴백).

    add_harness 의 operational 토큰 치환이 인스턴스의 실제 이름을 존중하도록 —
    최초 import 가 local.conf 에 박아 둔 값을 재사용한다(_parse_conf_keys). local.conf 부재·
    `project.name` 미설정이면 dest 디렉토리명(main 의 --name 기본값과 동형).
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if local_conf.is_file():
        try:
            conf = conf_schema.parse(fs.read_text_shared(local_conf, encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            conf = {}
        name = conf.get("project.name", "").strip()
        if name:
            return name
    return dest_root.name


def _local_conf_has_blocking_legacy(dest_root: Path) -> bool:
    """dest 의 local.conf 에 **값 공급을 잃는** 구표기 키가 있는가 (안내 전용 키는 제외).

    판정 기준은 공용 로더 하나다(`conf_schema.blocking_legacy`) — import 가 자기 목록을 들면
    소비 지점과 갈린다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return False
    return bool(conf_schema.blocking_legacy(conf_store.load(local_conf).legacy))


def print_conf_migration_notice(dest_root: Path) -> None:
    """local.conf 표기 통일 교체 안내 — import 도 채택자가 이 사실을 만나는 진입이다.

    안내만 낸다(자동 이관 없음·엔진은 채택자 conf 를 대신 고쳐 쓰지 않는다). 문구의 단일 진실은
    공용 로더(`conf_schema.migration_notice`)이고 pm_update 사본과 같은 문장을 쓴다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return
    for line in conf_schema.migration_notice(conf_store.load(local_conf)):
        print(f"  {line}")


def sync_local_conf(dest_root: Path, project_name: str) -> bool:
    """board.py init 직후 local.conf 의 operational 해소값을 pm_import 치환값과 일치시킨다.

    board.py init 의 감지값을 import 인자의 operational 값과 맞춰 엔진 문서(local.conf 해소)와
    CLAUDE.md(sed 치환)가 *다른 값*을 보지 않게 한다.
    `project.name`·`test.cmd`·`runtime.py` 3개 키만 키 단위 갱신해 정렬한다. 나머지 키
    (ctx 예산·additional_reviewer 등)와 주석은 보존. clobber 금지. 파일 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — operational 값 동기화 건너뜀.",
              file=sys.stderr)
        return False
    updates = {
        "project.name": project_name,
        "test.cmd": _default_test_cmd(),
        "runtime.py": python_env._detect_py(),
    }
    return _write_conf_keys(local_conf, updates)


def record_upstream(dest_root: Path, upstream_value) -> bool:
    """upstream 값(URL 또는 로컬 경로)을 dest local.conf 에 `upstream.path=` 로 기록한다.

    `--upstream`(future update 기록·URL 선호)↔`--from`(이번 import 파일 소스) 디커플:
    이 함수는 *기록할 upstream 값*을 받아 그대로 박는다. `--upstream` 생략 시 호출부가
    `--from`(=source_root)을 넘겨 **기존 동작(경로 기록)을 회귀 보존**한다 — `Path` 를 받으면
    `str()` 로 직렬화하므로 옛 `record_upstream(dest_root, source_root)` 호출 형태도 그대로 동작.

    이후 pm_update 가 --from 생략 시 이 값을 기본 upstream 으로 쓴다. 공용 writer의 키 단위
    중복 정규화·atomic replace·실효값 검증을 거친다 — 따라서 재-import(--into)
    는 reapply_preserved_conf_keys 가 백업의 *stale upstream 을 되살려도*(board init 은 upstream 을
    쓰지 않으므로 preserve 가 옛 값을 복원한다) 마지막에 *현재 값* 으로 제자리 확정 갱신된다(stale
    보존 아님). 바로 그 때문에 board init·conf sync·preserve 단계 *이후* 에 호출해야 갱신이 보장된다. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream 기록 건너뜀.", file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"upstream.path": str(upstream_value)})


def record_upstream_rev(dest_root: Path, rev: str) -> bool:
    """upstream baseline revision 을 dest local.conf 에 `upstream.rev=<commit>` 로 기록한다.

    drift-lint의 baseline 입력 — "마지막 동기 이후 upstream 변경분"을 재는 기준점이다
    import 시(이 함수)와 pm_update 매 sync 시 갱신된다. 현재 관찰값 `upstream.seen_rev` 는
    **별개 키**이고 writer 도 하나뿐이다 — `app.upstream_revisions.record_upstream_revs`(한 키 2역
    금지·race/자기비교 회피). rev 가
    빈 값(git repo 아님·HEAD 해소 실패)이면 호출부가 이 함수를 부르지 않는다(기록 생략·graceful).
    공용 writer의 중복 정규화·atomic replace·실효값 검증을 거치며 다른 키·주석은 보존한다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream.rev 기록 건너뜀.", file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"upstream.rev": rev})


def record_opencode_model(dest_root: Path, model: str) -> bool:
    """해소된 opencode 모델을 dest local.conf 에 `harness.opencode.pro_model=` 로 기록한다.

    {{OPENCODE_PRO_MODEL}} 가 import 때 파일에 직접 치환되지만, local.conf 엔 안 들어가
    pm_update 의 @render 가 그 토큰을 local.conf 에서 재유도할 때(`harness.opencode.pro_model` →
    OPENCODE_PRO_MODEL · app.update_render._LOCAL_CONF_TO_OPERATIONAL) 키 부재로 leak assertion 에
    걸려 채택자 렌더가 crash 한다. 따라서 *실제로 모델이 해소된* 경로(flag·interactive)에서만
    그 값을 local.conf 에 박아 둔다 — todo(미해소)는 토큰이 YAML 주석으로 남아 렌더 leak 이
    없으므로 기록하지 않는다(호출부 게이트). 공용 writer의 중복 정규화·atomic replace·실효값
    검증을 거치며 다른 키·주석은 보존한다. local.conf 부재면 graceful skip. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — opencode 모델 기록 건너뜀.",
              file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"harness.opencode.pro_model": model})


def _read_local_conf(path: Path) -> dict[str, str]:
    """local.conf → key=value dict (공용 로더 `conf_store.load` · **판정 없음**).

    apply 경로가 이 함수를 지난다 — 구표기 conf 를 가진 채택자도 **엔진 파일은 받아야** 안내대로
    고칠 수 있다. 그래서 여기서는 `assert_no_legacy` 를 부르지 않는다(값을 실제로 소비하는
    지점이 부른다·§교체 절차). 파일 부재 → {}, 읽기 실패는 전파한다.

    파일이 없으면 공용 로더를 부르지 않고 {} 다(`print_conf_migration_notice` 와 같은 규칙) — conf 가
    없는 트리(init 전·출하 템플릿)에서도 갱신 입구가 계획을 세울 수 있어야 한다.
    """
    if not Path(path).is_file():
        return {}
    return conf_store.load(path).values
