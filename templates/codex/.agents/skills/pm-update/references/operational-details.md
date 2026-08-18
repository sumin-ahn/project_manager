# $pm-update 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

### 2.5 변경점 미리보기 (sync 전)

baseline(local.conf `upstream_rev`)과 cache/경로 HEAD 사이 commit 수 및 받을 엔진파일을 read-only(`git log`/`diff`, fetch 0)로 본다.

```bash
./pm-update.sh --changes --from <cache-or-path>   # commit 수 + 4버킷 분리
```

출력 4버킷 (분류 기준은 실 sync의 계획 manifest와 같다 — guest 절 행 합류 포함):

| 버킷 | 뜻 |
|---|---|
| 헤더 | `baseline → HEAD (N commits)` |
| 엔진 영향 (manifest 경로·이번 동기가 받는 것) | 이번 sync가 덮어쓸 파일 |
| 그 외 변경 (manifest 밖·동기 안 받음) | upstream은 바뀌었으나 이 인스턴스가 등재하지 않은 경로 |
| 상류 삭제·rename (동기가 지우지 않음) | upstream에서 사라진 등재 경로. 이어지는 `상류 부재 파일` 보고가 dest에 잔존하는 실물을 나열한다(동기는 지우지 않음·수동 정리 판단) |

- **변경 0**: `--changes`는 미리보기일 뿐이므로 동기를 생략하지 않는다. facade를 한 번 실행해
  manifest 밖 instance-owned config 수렴 채널을 태운다. updater 자체가 바뀐 RUN1 뒤에는 새
  updater로 **zero-change RUN2**를 실행한다.
- **변경 > 0**: "엔진 영향(이번 동기가 받는 것)" 목록을 PM에게 보고한 뒤 reconcile → sync.
- baseline 미기록(첫 동기·구 import): "다음 sync 후 추적" 안내가 정상이며 그대로 진행.

### 3. manifest reconcile (pm_update 전·PM 주도·사용자 개입 0)

**기본 경로 = 선-cp 없이 4단계 sync 만 실행한다.** `@source` self-prop 채택자(현행 import 전원)는 엔진이 manifest 신 항목 도달을 스스로 처리한다 — 등재 행이 지워진 채택자도 sync 1회가 행 복구 + 파일 전파 + guest 절 보존을 함께 수행한다(실측 게이트 박제).

수동 cp 는 self-prop 이 없는 구세대 manifest 형상에서만 쓴다:

```bash
cp <cache-or-path>/templates/<harness>/.project_manager/engine.manifest .project_manager/engine.manifest
```

- ⚠️ **루트 manifest가 아니라 `templates/<harness>/` manifest**(`<harness>`=이 채택자의 claude_code | opencode)를 쓴다. 루트는 claude-scoped라 opencode 채택자를 clobber한다.
- ⚠️ **cp 는 파일 덮어쓰기라 add-harness guest 절이 통째로 사라진다**(보존 로직이 읽을 절이 dest 에 없어짐). cp 를 썼고 guest 하네스가 있다면 **직후 `./pm-config.sh add-harness <guest>` 재실행**으로 절을 되살린다 — sync 출력의 `미등재 flavor 파일 관측` 경고가 같은 복구를 안내한다.

### 5. drift 표면화

```bash
python3 .project_manager/tools/board.py lint
```

`adapter-drift` advisory가 남으면 PM에게 보고하고 수기 검토를 안내한다(never-block). 이는 baseline↔관찰 rev 불일치로 manifest 제외 facade·진입문서 등이 낡았을 수 있음을 뜻한다. lint는 git을 하지 않아 어느 rev가 앞섰는지 판정하지 않는다. 자동전파는 customization을 clobber할 수 있어 금지하며, 실제 선후·변경분은 `--changes`로 본다.
