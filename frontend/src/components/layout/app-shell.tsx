"use client";

import { MenuIcon, SearchIcon } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import type * as React from "react";

import { ModeBanner } from "@/components/system-status";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  SheetContent,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { useApprovals } from "@/lib/queries";
import { createSequencer } from "@/lib/shortcuts";

import { DensityToggle } from "./density-toggle";
import { NAV } from "./nav";
import { SidebarNav } from "./sidebar";
import { ThemeToggle } from "./theme-toggle";

const NEW_ROUTE = NAV.find((n) => n.href === ("/investigations/new" as Route))?.href ?? null;

// cmdk + the recent-investigations query load on first use (⌘K or the search box), not on every page
const CommandPalette = dynamic(() => import("./command-palette").then((m) => m.CommandPalette), {
  ssr: false,
});

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [paletteOpen, setPaletteOpenState] = useState(false);
  const [paletteUsed, setPaletteUsed] = useState(false);
  const setPaletteOpen = useCallback((o: boolean | ((prev: boolean) => boolean)) => {
    setPaletteUsed(true);
    setPaletteOpenState(o);
  }, []);
  const [helpOpen, setHelpOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const approvals = useApprovals("pending");
  const pending = approvals.data?.length ?? 0;

  const shortcuts = useMemo(() => {
    const list = NAV.filter((n) => n.shortcut).map((n) => ({
      keys: `g ${n.shortcut}`,
      description: `Go to ${n.label}`,
      href: n.href,
    }));
    return [
      ...list,
      ...(NEW_ROUTE ? [{ keys: "n", description: "New investigation", href: NEW_ROUTE }] : []),
      { keys: "?", description: "Keyboard shortcuts", href: null },
    ];
  }, []);

  useEffect(() => {
    const seq = createSequencer(shortcuts.map((s) => s.keys));
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (paletteOpen || helpOpen) return;
      const hit = seq(e);
      if (!hit) return;
      e.preventDefault();
      if (hit === "?") setHelpOpen(true);
      else {
        const s = shortcuts.find((x) => x.keys === hit);
        if (s?.href) router.push(s.href);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shortcuts, router, paletteOpen, helpOpen, setPaletteOpen]);

  const badges = { "/approvals": pending };
  return (
    <div className="flex min-h-dvh">
      <div aria-hidden className="aurora" />
      <a
        href="#main"
        className="sr-only z-50 rounded bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <aside className="hidden w-60 shrink-0 border-r border-glass-border bg-sidebar md:block">
        <div className="sticky top-0 h-dvh">
          <SidebarNav badges={badges} />
        </div>
      </aside>
      <Dialog open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-64 p-0 sm:max-w-64" aria-describedby={undefined}>
          <DialogTitle className="sr-only">Navigation</DialogTitle>
          <SidebarNav badges={badges} onNavigate={() => setMobileOpen(false)} />
        </SheetContent>
      </Dialog>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-glass-border glass-chrome px-4 md:px-6">
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label="Open navigation"
            onClick={() => setMobileOpen(true)}
          >
            <MenuIcon />
          </Button>
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            data-testid="open-palette"
            className="flex h-9 max-w-md min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-lg border border-glass-border bg-card/70 px-3 text-sm text-muted-foreground shadow-elev-1 transition-[background-color,box-shadow] hover:bg-accent hover:shadow-elev-2"
          >
            <SearchIcon aria-hidden className="size-4" />
            <span className="flex-1 truncate text-left">
              {NEW_ROUTE ? "Ask a question or search…" : "Search…"}
            </span>
            <Kbd>⌘K</Kbd>
          </button>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            <DensityToggle />
            <ThemeToggle />
          </div>
        </header>
        <ModeBanner />
        <main id="main" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 md:px-6 md:py-8">
          {children}
        </main>
      </div>
      {paletteUsed && (
        <CommandPalette
          open={paletteOpen}
          onOpenChange={setPaletteOpen}
          canInvestigate={NEW_ROUTE !== null}
        />
      )}
      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Keyboard shortcuts</DialogTitle>
            <DialogDescription>Work without the mouse.</DialogDescription>
          </DialogHeader>
          <ul className="divide-y text-sm">
            <li className="flex items-center justify-between py-2">
              Command palette <Kbd>⌘K</Kbd>
            </li>
            {shortcuts.map((s) => (
              <li key={s.keys} className="flex items-center justify-between py-2">
                {s.description}
                <span className="flex gap-1">
                  {s.keys.split(" ").map((k) => (
                    <Kbd key={k}>{k}</Kbd>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </DialogContent>
      </Dialog>
    </div>
  );
}
