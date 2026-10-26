"""상류 관찰값·성공 baseline 기록 (층: app)."""
from __future__ import annotations

from pathlib import Path
import sys
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import upstream_git


# baseline 과 *함께* 기록하는 현재-관찰 키 (board._DRIFT_SEEN_KEY 동명).
_SEEN_REV_KEY = "upstream.seen_rev"


def _upstream_rev_updates(rev: str) -> dict[str, str]:
    """이번에 기록할 rev 키 계획 — baseline 과 관찰값을 같은 rev 로 함께 쓴다(형상 무관).

    엔진이 URL upstream 도 직접 캐시 clone/fetch 하게 된 뒤로 두 형상 모두 *이 실행의 소스
    checkout HEAD* 가 곧 관찰값이다 — 관찰을 따로 하는 층이 없어졌으므로 한쪽을 비워 두면 그
    자리를 채울 writer 가 없어 두 키가 영구히 어긋난다(정상 흡수 직후에도 상시 거짓 drift).
    """
    return {"upstream.rev": rev, _SEEN_REV_KEY: rev}


def _warn_missing_conf_for_rev(local_conf: Path) -> tuple[bool, dict[str, str]]:
    """local.conf 부재 — rev 기록을 graceful 생략하고 `record_upstream_revs` 반환값을 낸다."""
    print(f"경고: local.conf 없음 ({local_conf}) — upstream.rev 기록 건너뜀.", file=sys.stderr)
    return False, {}


def record_upstream_revs(dest_root: Path, source_root: Path) -> tuple[bool, dict[str, str]]:
    """매 sync 후 upstream rev 키들을 dest local.conf 에 **단일 write** 로 기록.

    반환 `(변경 여부, 이번에 기록한 {키: rev})` — 호출부가 그 rev 를 안내 문구에 싣는다.
    기록 생략 시 `(False, {})`.

    기록 키:
      - `upstream.rev`      (baseline·항상) — drift-lint의 "마지막 동기 이후" 기준점
        pm_import(import 시)와 여기(매 sync) 둘 다 갱신해야 그 의미가 성립한다.
      - `upstream.seen_rev` (현재 관찰값·형상 무관) — *동기 시점 소스 checkout 의 rev 가 곧
        관찰값*이다(URL 은 이 실행이 clone/fetch 한 캐시 HEAD·경로는 그 checkout HEAD).
        baseline 만 갱신하면 두 키가 영구히 어긋나 정상 흡수 직후에도 drift 거짓 경보가 상시
        뜬다(실측).

    두 키를 한 번의 공용 atomic writer로 묶는다 — 중간 중단에도 baseline 만 앞선 반쪽
    상태가 생기지 않는다(어긋난 두 키 = 거짓 drift 의 원인이었다). rev 읽기는 `infra.upstream_git` 의
    read_upstream_rev(URL 안전 git 호출), 파일 갱신은 `infra.install_conf` 의 `_write_conf_keys_locked`(키 중복
    정규화·atomic replace·실효값 검증·record_upstream_rev·pm_config upstream set 과 동일 백엔드의
    임계 구간 본문)를 재사용한다. git repo 아님·HEAD 해소 실패·local.conf 부재는 **graceful
    생략**이다. 기록 실패는 그대로 올라간다.

    conf 존재 판정·atomic write·실효값 검증은 **한 락 구간**이다. source rev 읽기(git·네트워크)는
    대상 conf 와 무관하므로 락 밖이다 — 사람/네트워크 대기를 임계 구역에 넣지 않는다는 seam
    규약 그대로다.
    """
    rev = upstream_git.read_upstream_rev(source_root)  # 대상 conf 와 무관(git) — 락 밖.
    if not rev:
        return False, {}  # git repo 아님·HEAD 해소 실패 — graceful 생략.

    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        # init 전 트리·출하 템플릿처럼 conf 자체가 없는 형상에 락 파일을 만들지 않는 값싼 단축이다
        # (권위 판정은 아래 락 안에서 다시 한다 — 이 단축은 "쓰지 않는다" 만 결정하고 어떤 write
        # 계획의 입력도 아니다). conf 가 그사이 생기면 종전처럼 다음 sync 가 기록한다.
        return _warn_missing_conf_for_rev(local_conf)
    with fs.local_conf_write_lock(local_conf):
        if not local_conf.is_file():
            return _warn_missing_conf_for_rev(local_conf)
        updates = _upstream_rev_updates(rev)
        changed = install_conf._write_conf_keys_locked(local_conf, updates)
    return changed, updates


def record_upstream_rev_baseline(dest_root: Path, source_root: Path) -> bool:
    """`record_upstream_revs` 의 변경-여부 전용 wrapper (시그니처 보존·기존 호출부/테스트)."""
    return record_upstream_revs(dest_root, source_root)[0]


def converge_upstream_revs(
    dest_root: Path, source_root: Path, skew_status: str, skew_new: list[str]
) -> bool:
    """sync 뒤 revision 키를 기록·안내한다 — manifest skew 면 기록을 억제하고 False, 아니면 True.

    manifest skew(로컬 manifest 구형·신규 등재분 미도달)에서 baseline 을 최신으로 박으면 drift-lint
    가 "최신" 으로 침묵해 신규 엔진 파일 누락을 은폐한다. 반환값을 쓰는 제품 호출자는 없다."""
    if skew_status == "skew":
        print(
            f"→ manifest skew({len(skew_new)}건)로 upstream.rev baseline(+upstream.seen_rev "
            "관찰값) 갱신을 **억제**한다 — drift-lint 가 계속 이 skew 를 울리게 "
            "둔다. 로컬 engine.manifest 를 reconcile 한 뒤 다시 pm-update 하라(신규 등재분 "
            ")."
        )
        return False
    changed, recorded = record_upstream_revs(dest_root, source_root)
    if changed:
        print("✓ local.conf upstream.rev baseline 갱신 (drift-lint 기준점): "
              f"{recorded['upstream.rev']} (+upstream.seen_rev 동시 기록)")
    return True
