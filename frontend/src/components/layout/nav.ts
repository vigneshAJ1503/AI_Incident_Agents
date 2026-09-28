import {
  BotIcon,
  LayoutDashboardIcon,
  ListChecksIcon,
  MessageSquarePlusIcon,
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
];

export const BRAND = { name: "AI Ops", tagline: "Incident agents", icon: BotIcon };
