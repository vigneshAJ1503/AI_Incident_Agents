import {
  BoxesIcon,
  FlaskConicalIcon,
  ServerIcon,
  ShieldCheckIcon,
  BotIcon,
  LayoutDashboardIcon,
  ListChecksIcon,
  MessageSquarePlusIcon,
  PlugIcon,
  type LucideIcon,
} from "lucide-react";
import type { Route } from "next";

export interface NavItem {
  href: Route;
  label: string;
  icon: LucideIcon;
  /** `g <key>` keyboard shortcut */
  shortcut?: string;
  match: (path: string) => boolean;
}

export const NAV: NavItem[] = [
  {
    href: "/investigations/new",
    label: "New investigation",
    icon: MessageSquarePlusIcon,
    match: (p) => p === "/investigations/new",
  },
  {
    href: "/",
    label: "Dashboard",
    icon: LayoutDashboardIcon,
    shortcut: "d",
    match: (p) => p === "/",
  },
  {
    href: "/investigations",
    label: "Investigations",
    icon: ListChecksIcon,
    shortcut: "i",
    match: (p) => p.startsWith("/investigations") && p !== "/investigations/new",
  },
  {
    href: "/approvals",
    label: "Approvals",
    icon: ShieldCheckIcon,
    shortcut: "a",
    match: (p) => p.startsWith("/approvals"),
  },
  {
    href: "/services",
    label: "Services",
    icon: ServerIcon,
    shortcut: "s",
    match: (p) => p.startsWith("/services"),
  },
  {
    href: "/agents",
    label: "Agents",
    icon: BoxesIcon,
    match: (p) => p.startsWith("/agents"),
  },
  {
    href: "/scenarios",
    label: "Scenarios",
    icon: FlaskConicalIcon,
    match: (p) => p.startsWith("/scenarios"),
  },
  {
    href: "/settings/integrations",
    label: "Integrations",
    icon: PlugIcon,
    match: (p) => p.startsWith("/settings"),
  },
];

export const BRAND = { name: "AI Ops", tagline: "Incident agents", icon: BotIcon };
