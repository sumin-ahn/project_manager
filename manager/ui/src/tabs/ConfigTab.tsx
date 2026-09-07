import { useContext, useState } from "react";
import type { ReactNode } from "react";
import type { RepoRow } from "../api";
import { rowDot } from "../lib/status";
import { ProjectRunContext, useCommand, useProjectRun } from "../lib/run";
import CommandOutput from "../components/CommandOutput";
import { disabledReason } from "../components/StatusRowActions";
import ConfirmDialog from "../components/ConfirmDialog";

/**
 * 구성 탭 — repo 등록 · 보호 브랜치 · 슬롯 · 하네스 · task 정체성.
 *
 * 여기서 누르는 것은 전부 그 사본의 `pm-config` 명령 하나다. 관리자가 areas.md·장부를 여는
 * 자리는 없고, 거부(승인 토큰·dirty·소유검사)는 엔진 문구가 그대로 화면에 온다.
 *
 * 표는 가벼운 절(`status --json`)의 값이고, 동작 뒤에는 그 프로젝트를 다시 읽는다.
 * 이름만 있는 task 동작(`task new`·`task reopen`)이 여기 있는 이유: 종료한 task 는 JSON 의
 * `tasks` 에서 빠져(실측) 현황 탭에 행이 없다.
 */

const BUTTON = "rounded border border-slate-200 px-2 py-0.5 disabled:opacity-40";
const PRIMARY = "rounded border border-slate-800 px-2 py-0.5 disabled:opacity-40";
const FIELD = "rounded border border-slate-200 px-1 font-mono text-[11px]";

/** `repo add` 의 인자 — 이름은 위치 인자이고 나머지는 플래그다(URL 은 `--git`). */
const REPO_ADD_FLAGS = [
  ["--git", "git URL"], ["--test", "테스트 명령"], ["--base", "base 브랜치"],
  ["--protected", "보호 브랜치 (main,develop)"], ["--owner", "owner"], ["--user", "user"],
] as const;

type SlotDraft = { repo: string; readonly: boolean };

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1">
      <h2 className="font-medium">{title}</h2>
      {children}
    </section>
  );
}

function ConfigTabBody() {
  const { status } = useProjectRun();
  const { outcome, busy, call } = useCommand();
  const [repoName, setRepoName] = useState("");
  const [repoFlags, setRepoFlags] = useState<Record<string, string>>({});
  const [protectedDraft, setProtectedDraft] = useState<Record<string, string>>({});
  const [slotRepo, setSlotRepo] = useState("");
  const [slotDraft, setSlotDraft] = useState<SlotDraft | null>(null);
  const [harness, setHarness] = useState("");
  const [taskName, setTaskName] = useState("");

  const repos = status?.repos ?? [];
  const slots = status?.slots ?? [];
  const harnesses = status?.harnesses ?? [];

  // 보호목록 칸의 현재 값 — 사람이 고치기 전에는 장부 값이고, 빈 값은 [설정] 을 열지 않는다
  // (빈 문자열은 엔진이 거부하고, 칼럼 비움은 `default` 리터럴이다).
  const protectedValue = (repo: RepoRow) =>
    (protectedDraft[repo.repo] ?? repo.protected ?? "").trim();

  const repoAddArgv = () => {
    const argv = ["repo", "add", repoName.trim()];
    for (const [flag] of REPO_ADD_FLAGS) {
      const value = (repoFlags[flag] ?? "").trim();
      if (value !== "") argv.push(flag, value);
    }
    return argv;
  };

  return (
    <section data-testid="ConfigTab" className="space-y-4 px-4 py-3">
      <Section title="repo">
        <table className="w-full">
          <tbody>
            {repos.map((repo) => (
              <tr key={repo.repo} data-testid={`repo-${repo.repo}`} className="align-top">
                <td className="py-0.5 font-mono">{repo.repo}</td>
                <td className="py-0.5 text-slate-500">{repo.base ?? "base 없음"}</td>
                <td className="py-0.5">
                  <input aria-label={`${repo.repo} 보호 브랜치`} value={protectedValue(repo)}
                         onChange={(event) => setProtectedDraft(
                           (current) => ({ ...current, [repo.repo]: event.target.value }))}
                         className={`${FIELD} w-56`} />
                </td>
                <td className="py-0.5 text-right">
                  <span className="inline-flex gap-1">
                    <button type="button" data-testid={`protected-set-${repo.repo}`}
                            disabled={busy || protectedValue(repo) === ""}
                            onClick={() => void call(
                              ["repo", "protected", repo.repo, protectedValue(repo)])}
                            className={BUTTON}>설정</button>
                    <button type="button" data-testid={`protected-show-${repo.repo}`} disabled={busy}
                            onClick={() => void call(["repo", "protected", repo.repo])}
                            className={BUTTON}>조회</button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex flex-wrap items-center gap-1">
          <input aria-label="repo 이름" value={repoName} placeholder="이름"
                 onChange={(event) => setRepoName(event.target.value)} className={`${FIELD} w-28`} />
          {REPO_ADD_FLAGS.map(([flag, placeholder]) => (
            <input key={flag} aria-label={`repo ${flag}`} placeholder={placeholder}
                   value={repoFlags[flag] ?? ""}
                   onChange={(event) => setRepoFlags(
                     (current) => ({ ...current, [flag]: event.target.value }))}
                   className={`${FIELD} w-40`} />
          ))}
          <button type="button" data-testid="repo-add" disabled={busy || repoName.trim() === ""}
                  onClick={() => void call(repoAddArgv())} className={PRIMARY}>repo 추가</button>
          <button type="button" data-testid="repo-list" disabled={busy}
                  onClick={() => void call(["repo", "list"])} className={BUTTON}>목록 보기</button>
        </div>
      </Section>

      <Section title="슬롯">
        <table className="w-full">
          <tbody>
            {slots.map((slot) => {
              const verdict = rowDot(slot);
              return (
                <tr key={slot.slot} data-testid={`config-slot-${slot.slot}`} className="align-top">
                  <td className="py-0.5 font-mono">{slot.slot}</td>
                  <td className="py-0.5 text-slate-500">{verdict.label}</td>
                  <td className="py-0.5 text-right">
                    {verdict.actions === "refresh" ? null : (
                      <button type="button" data-testid={`slot-remove-${slot.slot}`}
                              disabled={busy || verdict.actions !== "change"}
                              title={disabledReason(slot, verdict) ?? ""}
                              onClick={() => void call(["worktree", "remove", slot.slot])}
                              className={BUTTON}>제거</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="flex items-center gap-1">
          <select aria-label="슬롯을 만들 repo" value={slotRepo} className={`${FIELD} w-40`}
                  onChange={(event) => setSlotRepo(event.target.value)}>
            <option value="">repo 선택</option>
            {repos.map((repo) => <option key={repo.repo} value={repo.repo}>{repo.repo}</option>)}
          </select>
          <button type="button" data-testid="slot-add" disabled={busy || slotRepo === ""}
                  onClick={() => setSlotDraft({ repo: slotRepo, readonly: false })}
                  className={PRIMARY}>슬롯 추가</button>
          <button type="button" data-testid="slot-add-readonly" disabled={busy || slotRepo === ""}
                  onClick={() => setSlotDraft({ repo: slotRepo, readonly: true })}
                  className={BUTTON}>readonly 슬롯 추가</button>
          <button type="button" data-testid="slot-prune" disabled={busy}
                  onClick={() => void call(["worktree", "prune-stale"])}
                  className={BUTTON}>정리</button>
        </div>
        {slotDraft !== null && (
          <ConfirmDialog
            title={`repo ${slotDraft.repo} 에 ${slotDraft.readonly ? "readonly " : ""}물리 슬롯을 만든다 — 승인?`}
            note={`work/${slotDraft.repo}_<번호> 를 만든다(번호·브랜치는 엔진이 고른다 · base `
                  + `${repos.find((repo) => repo.repo === slotDraft.repo)?.base ?? "기본 브랜치"}). `
                  + "물리 슬롯 생성은 사용자 승인 행위다."}
            value={slotDraft.repo}
            onConfirm={(ack) => {
              const draft = slotDraft;
              setSlotDraft(null);
              // `ack` 는 대화가 돌려준 값이다 — 이 자리에서 repo 이름을 복사해 붙이지 않는다.
              void call(["worktree", "add", draft.repo,
                         ...(draft.readonly ? ["--readonly"] : []), "--user-ack", ack]);
            }}
            onCancel={() => setSlotDraft(null)}
          />
        )}
      </Section>

      <Section title="하네스">
        <p data-testid="harness-list" className="font-mono text-[11px] text-slate-600">
          {harnesses.join(" · ") || "(없음)"}
        </p>
        <div className="flex items-center gap-1">
          <input aria-label="추가할 하네스" value={harness} placeholder="claude|codex|opencode"
                 onChange={(event) => setHarness(event.target.value)} className={`${FIELD} w-40`} />
          <button type="button" data-testid="harness-add" disabled={busy || harness.trim() === ""}
                  onClick={() => void call(["add-harness", harness.trim()])}
                  className={PRIMARY}>하네스 추가</button>
        </div>
      </Section>

      <Section title="task">
        <div className="flex items-center gap-1">
          <input aria-label="task 이름" value={taskName} placeholder="이름"
                 onChange={(event) => setTaskName(event.target.value)} className={`${FIELD} w-40`} />
          <button type="button" data-testid="task-new" disabled={busy || taskName.trim() === ""}
                  onClick={() => void call(["task", "new", taskName.trim()])}
                  className={PRIMARY}>+ task 만들기</button>
          <button type="button" data-testid="task-reopen" disabled={busy || taskName.trim() === ""}
                  onClick={() => void call(["task", "reopen", taskName.trim()])}
                  className={BUTTON}>되살리기</button>
        </div>
      </Section>

      <CommandOutput outcome={outcome} />
    </section>
  );
}

export default function ConfigTab() {
  // 프로젝트를 고르기 전에는 부를 사본이 없다 — 본체(훅)를 그리지 않고 안내만 낸다.
  const run = useContext(ProjectRunContext);
  if (run === null) {
    return <section data-testid="ConfigTab" className="px-4 py-3 text-slate-500">
      프로젝트를 고르면 구성이 온다
    </section>;
  }
  return <ConfigTabBody />;
}
