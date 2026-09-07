import * as RadixTabs from "@radix-ui/react-tabs";
import { useEffect, useState } from "react";
import type { Entry } from "../App";
import type { Bridge } from "../api";
import ConfigTab from "../tabs/ConfigTab";
import SettingsTab from "../tabs/SettingsTab";
import StatusTab from "../tabs/StatusTab";
import UpdateTab from "../tabs/UpdateTab";

const TRIGGER =
  "px-3 py-1 border-b-2 border-transparent data-[state=active]:border-slate-800";

/** 갓 적용한 프로젝트가 열리는 자리 — 질문형 설정이 여기서 시작한다. */
const QUESTION_TAB = "settings";

/** 가운데 칸 탭 넷 — 현황 탭의 읽기 부분, 설정 탭, 갱신 탭. */
export default function Tabs({ bridge, path, light, heavy, latestTag, onOpenUpdate, onApplied,
                              startAsking }:
  { bridge: Bridge; path: string | null; light: Entry | undefined;
    heavy: Entry | undefined; latestTag: string | null; onOpenUpdate: () => void;
    onApplied: () => void; startAsking: boolean }) {
  const [tab, setTab] = useState("status");

  // 새 프로젝트를 적용하면 그 프로젝트의 설정 탭이 질문형으로 열린다.
  useEffect(() => { if (startAsking) setTab(QUESTION_TAB); }, [startAsking]);

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
        <RadixTabs.Content value="status"><StatusTab light={light} heavy={heavy} /></RadixTabs.Content>
        <RadixTabs.Content value="settings">
          <SettingsTab bridge={bridge} path={path} startAsking={startAsking} />
        </RadixTabs.Content>
        <RadixTabs.Content value="config"><ConfigTab /></RadixTabs.Content>
        <RadixTabs.Content value="update">
          <UpdateTab bridge={bridge} path={path} light={light} heavy={heavy}
                     latestTag={latestTag} onApplied={onApplied} />
        </RadixTabs.Content>
      </div>
    </RadixTabs.Root>
  );
}
