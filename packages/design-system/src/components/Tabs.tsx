import {
  Tab as AriaTab,
  TabList as AriaTabList,
  type TabListProps,
  TabPanel as AriaTabPanel,
  type TabPanelProps,
  type TabProps,
  Tabs as AriaTabs,
  type TabsProps,
} from "react-aria-components";

export function Tabs(props: TabsProps) {
  return <AriaTabs {...props} className={`soa-tabs ${props.className ?? ""}`} />;
}

export function TabList<T extends object>(props: TabListProps<T>) {
  return <AriaTabList {...props} className={`soa-tab-list ${props.className ?? ""}`} />;
}

export function Tab(props: TabProps) {
  return <AriaTab {...props} className={`soa-tab ${props.className ?? ""}`} />;
}

export function TabPanel(props: TabPanelProps) {
  return <AriaTabPanel {...props} className={`soa-tab-panel ${props.className ?? ""}`} />;
}
