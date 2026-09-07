import * as RadixTabs from "@radix-ui/react-tabs";
import type { Entry } from "../App";
import ConfigTab from "../tabs/ConfigTab";
import SettingsTab from "../tabs/SettingsTab";
import StatusTab from "../tabs/StatusTab";
import UpdateTab from "../tabs/UpdateTab";

const TRIGGER =
  "px-3 py-1 border-b-2 border-transparent data-[state=active]:border-slate-800";

/** 가운데 칸 탭 넷 — 이 티켓은 현황 탭의 읽기 부분만 채운다. */
export default function Tabs({ light, heavy, onOpenUpdate }:
  { light: Entry | undefined; heavy: Entry | undefined; onOpenUpdate: () => void }) {
  return (
    <RadixTabs.Root defaultValue="status" className="flex min-h-0 flex-1 flex-col"
                    onValueChange={(value) => { if (value === "update") onOpenUpdate(); }}>
      <RadixTabs.List className="flex border-b border-slate-200 px-2">
        <RadixTabs.Trigger value="status" className={TRIGGER}>현황</RadixTabs.Trigger>
        <RadixTabs.Trigger value="settings" className={TRIGGER}>설정</RadixTabs.Trigger>
        <RadixTabs.Trigger value="config" className={TRIGGER}>구성</RadixTabs.Trigger>
        <RadixTabs.Trigger value="update" className={TRIGGER}>갱신</RadixTabs.Trigger>
      </RadixTabs.List>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <RadixTabs.Content value="status"><StatusTab light={light} heavy={heavy} /></RadixTabs.Content>
        <RadixTabs.Content value="settings"><SettingsTab /></RadixTabs.Content>
        <RadixTabs.Content value="config"><ConfigTab /></RadixTabs.Content>
        <RadixTabs.Content value="update"><UpdateTab /></RadixTabs.Content>
      </div>
    </RadixTabs.Root>
  );
}
