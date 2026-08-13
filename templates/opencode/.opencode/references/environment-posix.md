# Linux/macOS 환경 명령 문법

부트스트랩이 `현재 환경: linux` 또는 `현재 환경: macos`를 표시할 때 이 문서를 따른다. 이 판정은
실제 셸 감지가 아니라 POSIX 계열 OS에서 쓰는 안전한 표기 정책이다.

## Python 런처

카드의 기본 런처는 `python3`이다. clone-local `local.conf`에서 마지막 `py=` assignment의 값이
non-empty일 때만 그 검증된 런처를 우선한다. 마지막 assignment가 비었으면 앞선 non-empty 값도
무효화하며, 키가 없거나 파일을 읽을 수 없는 경우와 마찬가지로 `python3`으로 돌아간다.
`python3`/`python`이 WindowsApps 가짜 shim일 수 있다는 Windows 경계는
`environment-windows.md`에서 다룬다.

## 명령 연결과 facade

Linux/macOS 표기 정책은 and-if(`&&`) 연결을 허용한다. 현재 부트스트랩 카드의 실행 행은 각기 단일
명령이라 연결을 새로 합성하지 않는다. `./pm-config.sh`와 `./pm-update.sh`는 bash facade이며,
Windows PowerShell/cmd의 `.\pm-config.cmd`와 `.\pm-update.cmd` 대응은 Windows 안내를 따른다.

## 인코딩

엔진은 파일 IO에 `encoding="utf-8"`을 쓰고 콘솔 stdout을 재구성하므로 env prefix 없이 호출한다.
구버전 Windows나 서드파티 파이프에서만 bash `PYTHONUTF8=1` 또는 PowerShell
`$env:PYTHONUTF8='1';`를 opt-in으로 붙인다. 평상시 Linux/macOS 명령에 env prefix를 요구하지 않는다.

## 출하 경로

파생 목적지와 OpenCode 평면 command의 상대 링크 계약은 `environment-windows.md`의
"출하 경로" 절을 단일 진실로 따른다.
