import * as RadixTabs from "@radix-ui/react-tabs";
import { useEffect, useMemo, useState } from "react";
import type { Entry, SettingsEntry } from "../App";
import type { Bridge, ConfSection, CtxCapacity } from "../api";
import { ProjectRunContext } from "../lib/run";
import ConfigTab from "../tabs/ConfigTab";
import SettingsTab from "../tabs/SettingsTab";
import StatusTab from "../tabs/StatusTab";
import UpdateTab from "../tabs/UpdateTab";

const TRIGGER =
  "px-3 py-1 border-b-2 border-transparent data-[state=active]:border-slate-800";

/** 갓 적용한 프로젝트가 열리는 자리 — 질문형 설정이 여기서 시작한다. */
const QUESTION_TAB = "settings";

/** 가운데 칸 탭 넷 — 현황 탭의 읽기 부분, 설정 탭, 구성 탭, 갱신 탭. */
export default function Tabs({ bridge, path, light, heavy, remote, settings, latestTag,
                              onOpenUpdate, onApplied, onChanged, onReloadSettings, startAsking, onSettingsEntry, capacities }:
  { bridge: Bridge; path: string | null; light: Entry | undefined;
    heavy: Entry | undefined; remote: Entry | undefined;
    settings: SettingsEntry | undefined; latestTag: string | null;
    onOpenUpdate: () => void; onApplied: () => void; onChanged: () => void;
    onReloadSettings: () => void; startAsking: boolean;
    onSettingsEntry?: (path: string) => void; capacities?: Record<string, CtxCapacity> }) {
  const [tab, setTab] = useState("status");
  // 설정 탭의 [불러오기] 가 받아 온 판정 — 설정 완전성 다섯 목록 중 "갱신하면 필요" 가 실린
  // 유일한 조회라, 그 값을 쓰는 두 탭(설정·갱신)이 여기서 같은 것을 본다. 프로젝트를 바꾸면
  // 지운다(다른 프로젝트의 판정을 보여 주지 않는다).
  const [adapterConf, setAdapterConf] = useState<ConfSection | null>(null);
  useEffect(() => { setAdapterConf(null); }, [path]);
  // 사본을 부르는 자리 하나 — 구성 탭과 현황 탭 행 동작이 같은 값을 쓴다(현황 탭의 읽기
  // 구조를 고치지 않고 행 동작에 바깥을 주는 유일한 배선이다).
  const run = useMemo(
    () => (path === null ? null : { bridge, path, status: light?.status, onChanged }),
    [bridge, path, light, onChanged]);

  // 새 프로젝트를 적용하면 그 프로젝트의 설정 탭이 질문형으로 열린다.
  useEffect(() => { if (startAsking) setTab(QUESTION_TAB); }, [startAsking]);

  useEffect(() => {
    if (tab === "settings" && path !== null) onSettingsEntry?.(path);
  }, [tab, path, onSettingsEntry]);

  return (
    <RadixTabs.Root value={tab} className="flex min-h-0 flex-1 flex-col"
                    onValueChange={(value) => {
                      setTab(value);
                      if (value === "update") onOpenUpdate();
                    }}>
      <RadixTabs.List className="flex border-b border-slate-200 px-2">
        <RadixTabs.Trigger value="status" className={TRIGGER}>현황</RadixTabs.Trigger>
        <RadixTabs.Trigger value="settings" className={TRIGGER}>설정</RadixTabs.Trigger>
        <RadixTabs.Trigger value="config" className={TRIGGER}>구성</RadixTabs.Trigger>
        <RadixTabs.Trigger value="update" className={TRIGGER}>갱신</RadixTabs.Trigger>
      </RadixTabs.List>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <ProjectRunContext.Provider value={run}>
          <RadixTabs.Content value="status"><StatusTab light={light} heavy={heavy} remote={remote} /></RadixTabs.Content>
          <RadixTabs.Content value="settings">
            <SettingsTab capacities={capacities} bridge={bridge} path={path} light={light} settings={settings}
                         onReload={onReloadSettings} startAsking={startAsking}
                         adapterConf={adapterConf} onAdapterConf={setAdapterConf} />
          </RadixTabs.Content>
          <RadixTabs.Content value="config"><ConfigTab remote={remote} /></RadixTabs.Content>
          <RadixTabs.Content value="update">
            <UpdateTab bridge={bridge} path={path} light={light} adapterConf={adapterConf}
                       latestTag={latestTag} onApplied={onApplied} />
          </RadixTabs.Content>
        </ProjectRunContext.Provider>
      </div>
    </RadixTabs.Root>
  );
}
