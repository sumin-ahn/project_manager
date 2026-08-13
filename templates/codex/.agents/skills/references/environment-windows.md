# Windows 환경 명령 문법

부트스트랩이 `현재 환경: windows`를 표시할 때 이 문서를 따른다. 이 판정은 실제 셸 감지가 아니라
Windows에서 안전한 표기 정책이다. 따라서 PowerShell, cmd, Git Bash 중 하나를 추론하지 않는다.

## Python 런처와 PowerShell 5.x

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

부트스트랩에 표시된 `python=<런처>`가 카드의 실제 런처다. clone-local `local.conf`에서 마지막
`py=` assignment의 값이 non-empty일 때만 그 값을 쓴다. 마지막 assignment가 비었으면 앞선
non-empty 값도 무효화하며, 키가 없거나 파일을 읽을 수 없는 경우와 마찬가지로 Windows 기본
`py -3`을 쓴다. 명령은 `&&`로 연결하지 않고 도구의 workdir 파라미터를 사용하거나 줄별로 분리한다.

## `pm-config` Windows 진입 원문

> **Windows 진입**: `./pm-config.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-config.cmd`**(동일 인자·
> pm_import 가 루트로 복사). PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신
> **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## `$pm-update` Windows 진입 원문

> **Windows 진입**: `./pm-update.sh` 는 bash 용 — PowerShell/cmd 에선 **`.\pm-update.cmd`**(동일 인자·아래 `./pm-config.sh` 참조도 동형 **`.\pm-config.cmd`**).
> 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> PowerShell 5.x 는 `&&` 체이닝 미지원(ParseError) — `cd X && …` 대신 **도구의 workdir 파라미터**나 명령 분리로 실행한다.

## 릴리즈의 두 facade 합성 원문

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> `./pm-update.sh`·`./pm-config.sh` 파사드는 bash 용 — PowerShell/cmd 에선 각각
> **`.\pm-update.cmd`**·**`.\pm-config.cmd`**(동일 인자).
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 인코딩

엔진은 파일 IO에 `encoding="utf-8"`을 쓰고 콘솔 stdout을 재구성하므로 env prefix 없이 호출한다.
구버전 Windows나 서드파티 파이프에서만 PowerShell은 `$env:PYTHONUTF8='1';`, bash는
`PYTHONUTF8=1`을 opt-in으로 붙인다. bash 문법을 Windows 전 환경에 강제하지 않는다.

## 출하 경로

이 canonical 문서와 `environment-posix.md`는 Claude/OpenCode 모델 채널의
`.claude/skills/references/`, Codex 모델 채널의 `.agents/skills/references/`, OpenCode 사람 채널의
`.opencode/references/`에 기계 생성한다. 특히 평면 `.opencode/command/<skill>.md`의
`../references/environment-*.md`는 `.opencode/references/environment-*.md`로 해소된다.
