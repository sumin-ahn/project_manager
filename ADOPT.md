# ADOPT.md — 이 프레임워크를 채택하기 (fresh 세션 온보딩 가이드)

> 당신(target-harness LLM 세션·claude 또는 opencode)이 **이 프레임워크(project_manager)를 다른 프로젝트에
> 채택**하도록 안내한다. 사용자는 보통 **이 manager 의 경로(또는 git URL) 하나만** 준다 — 나머지는 이
> 가이드와 `pm-import.sh --help` 를 읽고 당신이 자율 수행한다.

## 한 줄

```
<manager>/pm-import.sh --new <대상경로> --harness <자기-하니스> [--upstream <url|path>]
```

`<manager>` = 이 repo(project_manager)의 루트(또는 그 clone). `pm-import.sh` 가 deep 경로·인터프리터를
자기 위치로 해석하므로 cwd 무관하게 동작한다(Windows 는 `pm-import.cmd`).

## 자율 판단 3가지

1. **harness = 자기 세션** — 당신이 Claude Code 세션이면 `--harness claude`, opencode 세션이면
   `--harness opencode`. (자명하므로 사용자에게 묻지 않는다.)
2. **`--new` vs `--into`** — 맥락으로 고른다:
   - **`--new <dest>`** : 코드 없는 **PM 홈**을 새로 만든다(빈 디렉토리 + `git init`). 표준 채택(ADR-0026).
     "프로젝트 PM 을 새로 시작" / "PM 홈 만들어" 류.
   - **`--into <dest>`** : **기존 프로젝트에 임베드**(비파괴·기존 파일 백업). "이 프로젝트에 프레임워크 얹어" 류.
3. **upstream = future 갱신 소스** (`--upstream`, 생략 가능):
   - **권장 기본 = git URL** — 릴리스 추적(공개 GitHub 등). 로컬 clone 관리 불요.
   - manager 가 **로컬 clone** 이면 생략해도 `git remote get-url origin` 으로 URL 자동도출된다.
   - **엔진 공동개발**(프레임워크 자체를 같이 고치는 경우)이면 로컬 **경로**를 명시(`--upstream /path/to/checkout`).
   - 값은 self-describing(`https://`·`ssh://`·`file://`→URL · 그 외→경로)·안전 검증을 통과해야 한다 —
     scheme allowlist(https/ssh/file *만*·평문 비인증 `git://`·`http://` 는 거부) · credential-in-URL 거부 ·
     leading-dash/transport-helper(`ext::`) 거부. 거부되면 `--help`/엔진 에러가 사유를 명시한다.

## import 후 다음 단계

채택자 트리에 PM 스킬/command 이 함께 도착한다. 순서대로:

1. **`/pm-bootstrap`** — 보드·git·회귀·log 마지막 entry 를 한 번에 측정(세션 시작 상태점검).
2. **`/pm-env`** — 환경 관리(multi-PM repo/worktree 슬롯·upstream show/switch). 솔로면 보통 불필요.
3. 이후 엔진 갱신이 필요할 때 **`/pm-update`** — upstream(위 값)에서 엔진 변경을 흡수(URL→cache clone·경로→pull).

## 기존 채택자가 *새* PM command 를 받으려면 (opencode)

opencode 어댑터 command(`.opencode/command/`)는 `@target-owned` 라 `pm-update` 전파에서 제외된다(채택자 소유).
프레임워크가 새 command(예 `pm-update`·`pm-env`)를 추가했고 기존 opencode 채택자가 그걸 받으려면 — 같은
`pm-import.sh ... --into <기존-dest>` 로 **재-import**(비파괴·기존 customization 백업)한다. claude 스킬
(`.claude/skills/`)은 `pm-update` 가 정상 전파하므로 재-import 불요. (엔진 `board.py` 등은 양쪽 모두 전파된다.)

## 참고

- `pm-import.sh --help` — 전체 플래그(`--weight`·`--fill`·`--name`·`--opencode-model` 등).
- 설계 근거: ADR-0026(빈 홈 표준)·ADR-0027(import 의미·두 git)·ADR-0032(upstream 하이브리드·운영 스킬화).
