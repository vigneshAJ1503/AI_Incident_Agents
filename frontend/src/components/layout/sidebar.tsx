"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Kbd } from "@/components/ui/kbd";
import { DEMO_MODE } from "@/lib/config";
import { cn } from "@/lib/utils";

import { BRAND, NAV } from "./nav";

export function SidebarNav({
  onNavigate,
  badges = {},
}: {
  onNavigate?: () => void;
  badges?: Record<string, number>;
}) {
  const pathname = usePathname();
  const Brand = BRAND.icon;
  return (
    <div className="flex h-full flex-col text-sidebar-foreground">
      <Link href="/" onClick={onNavigate} className="flex items-center gap-2.5 px-5 py-5">
        <span className="grid size-8 place-items-center rounded-lg bg-(image:--brand-gradient) text-white shadow-elev-2">
          <Brand aria-hidden className="size-4.5" />
        </span>
        <span className="leading-tight">
          <span className="block text-sm font-semibold">{BRAND.name}</span>
          <span className="block text-[11px] text-sidebar-muted">{BRAND.tagline}</span>
        </span>
      </Link>
      <nav aria-label="Main" className="flex-1 px-3">
        <ul className="space-y-0.5">
          {NAV.map((item) => {
            const active = item.match(pathname);
            const Icon = item.icon;
            const badge = badges[item.href];
            return (
              <li key={item.href} className="relative">
                {active && (
                  <motion.span
                    layoutId="nav-active"
                    className="absolute inset-0 rounded-md bg-sidebar-accent shadow-[inset_0_0_0_1px_var(--glass-border)]"
                    transition={{ type: "spring", stiffness: 500, damping: 40 }}
                  />
                )}
                <Link
                  href={item.href}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "relative flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                    active
                      ? "font-medium text-sidebar-accent-foreground"
                      : "text-sidebar-muted hover:text-sidebar-foreground",
                  )}
                >
                  <Icon aria-hidden className="size-4" />
                  <span className="flex-1">{item.label}</span>
                  {badge ? (
                    <span className="rounded-full bg-sidebar-primary px-1.5 text-[11px] font-semibold text-white tabular-nums">
                      {badge}
                    </span>
                  ) : item.shortcut ? (
                    <Kbd className="hidden border-sidebar-foreground/15 bg-sidebar-foreground/5 text-sidebar-muted lg:inline-flex">
                      g {item.shortcut}
                    </Kbd>
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="m-3 rounded-lg border border-glass-border bg-glass px-3 py-2.5 text-[11px] text-sidebar-muted">
        {DEMO_MODE ? (
          <>
            <span className="font-medium text-sidebar-foreground">Demo mode</span> · static dataset,
            simulated live runs
          </>
        ) : (
          <>
            Press{" "}
            <Kbd className="border-sidebar-foreground/15 bg-sidebar-foreground/5 text-sidebar-muted">
              ?
            </Kbd>{" "}
            for keyboard shortcuts
          </>
        )}
      </div>
    </div>
  );
}
