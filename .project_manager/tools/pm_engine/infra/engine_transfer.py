"""엔진 코드·훅 파일의 원자 복사 (층: infra)."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import tempfile
from pm_engine.infra import fs


def _atomic_copy2(source: Path, dest: Path) -> None:
    """같은 디렉토리의 완성된 임시 사본을 원자적으로 dest에 교체한다.

    dest 가 symlink 면 원자 교체는 **링크 자체를 대체**한다(일반 copy2 는 링크 너머 타깃에
    쓴다) — 의도된 동작이다. 엔진이 관리하는 훅 세트 파일은 manifest 소유 실파일이고, 그 자리를
    링크로 바꿔 둔 트리에 링크 너머로 쓰면 엔진이 자기 관리 밖 경로를 갱신하게 된다."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent,
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        fs.atomic_replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
