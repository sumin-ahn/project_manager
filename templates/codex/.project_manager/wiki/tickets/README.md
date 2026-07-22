# Tickets — 개발 작업 보드

`tickets/`는 개발 작업 ticket의 단일 진실입니다. ticket 파일이 들어 있는 상태 디렉터리가 현재 상태를 뜻합니다.

```text
tickets/
├── _template.md
├── open/       누구나 claim 가능
├── claimed/    작업 중
├── blocked/    대기 중
└── done/       완료
```

새 ticket은 `python3 .project_manager/tools/board.py new "제목"`으로 만들고, `claim`,
`complete`, `block`, `unblock`으로 상태를 전환합니다. 모든 상태 디렉터리는 스캐폴드의 일부이며
각각 `.gitkeep`으로 출하됩니다.
