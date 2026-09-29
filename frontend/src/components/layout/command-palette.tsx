"use client";

import { Command } from "cmdk";
import { MoonIcon, SearchIcon, SparklesIcon, SunIcon } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useRef, useState } from "react";
import { flushSync } from "react-dom";

import { StatusBadge } from "@/components/status";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { useInvestigations } from "@/lib/queries";

import { NAV } from "./nav";

const item =
  "flex cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-sm aria-selected:bg-accent aria-selected:text-accent-foreground";
const group =
  "px-1 py-1.5 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground";

export function CommandPalette({
  open,
  onOpenChange,
  canInvestigate,
  takeTypedAhead,
  onFocused,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canInvestigate: boolean;
  /** keys typed after ⌘K while the lazily loaded palette was still on its way (drains the buffer) */
  takeTypedAhead?: () => string;
  /** the input has focus: from now on keys go straight to it */
  onFocused?: () => void;
}) {
  const router = useRouter();
  const { resolvedTheme, setTheme } = useTheme();
  const [search, setSearch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  /**
   * Instead of Radix's autofocus: commit the typed-ahead text first (flushSync, so the DOM input
   * already holds it), then focus; keys typed until then keep landing in the shell's buffer.
   */
  const focusWithTypedAhead = (e: Event) => {
    e.preventDefault();
    setTimeout(() => {
      const typed = takeTypedAhead?.() ?? "";
      if (typed) flushSync(() => setSearch((s) => s + typed));
      inputRef.current?.focus();
      onFocused?.();
    }, 0);
  };
  const recent = useInvestigations({ limit: 8 });

  const go = (href: Route) => {
    onOpenChange(false);
    setSearch("");
    router.push(href);
  };

  /** The chat page asks /ask: incidents open their live view, platform questions get a reply. */
  const askQuestion = () => {
    const question = search.trim();
    if (question.length < 3) return;
    go(`/investigations/new?q=${encodeURIComponent(question)}` as Route);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-xl overflow-hidden p-0"
        hideClose
        onOpenAutoFocus={focusWithTypedAhead}
      >
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <DialogDescription className="sr-only">
          Ask a question, jump to a page or open an investigation.
        </DialogDescription>
        <Command label="Command palette" className="flex flex-col" loop>
          <div className="flex items-center gap-2 border-b px-4">
            <SearchIcon aria-hidden className="size-4 text-muted-foreground" />
            <Command.Input
              ref={inputRef}
              value={search}
              onValueChange={setSearch}
              placeholder={
                canInvestigate ? "Ask a question or search…" : "Search pages and investigations…"
              }
              className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
            <Kbd>esc</Kbd>
          </div>
          <Command.List className="max-h-[60vh] overflow-y-auto p-1">
            <Command.Empty className="py-6 text-center text-sm text-muted-foreground">
              No matches.
            </Command.Empty>
            {canInvestigate && search.trim().length >= 3 && (
              <Command.Group heading="Ask" className={group}>
                <Command.Item value={`ask ${search}`} onSelect={askQuestion} className={item}>
                  <SparklesIcon aria-hidden className="size-4 text-primary" />
                  <span className="truncate">
                    Ask: <span className="font-medium">“{search.trim()}”</span>
                  </span>
                </Command.Item>
              </Command.Group>
            )}
            <Command.Group heading="Go to" className={group}>
              {NAV.map((n) => (
                <Command.Item
                  key={n.href}
                  value={`go ${n.label}`}
                  onSelect={() => go(n.href)}
                  className={item}
                >
                  <n.icon aria-hidden className="size-4" />
                  {n.label}
                  {n.shortcut && <Kbd className="ml-auto">g {n.shortcut}</Kbd>}
                </Command.Item>
              ))}
            </Command.Group>
            {recent.data && recent.data.items.length > 0 && (
              <Command.Group heading="Recent investigations" className={group}>
                {recent.data.items.map((inv) => (
                  <Command.Item
                    key={inv.id}
                    value={`${inv.incident.title} ${inv.id} ${inv.incident.service ?? ""}`}
                    onSelect={() => go(`/investigations/${inv.id}` as Route)}
                    className={item}
                  >
                    <span className="flex-1 truncate">{inv.incident.title}</span>
                    <StatusBadge status={inv.status} />
                  </Command.Item>
                ))}
              </Command.Group>
            )}
            <Command.Group heading="Preferences" className={group}>
              <Command.Item
                value="toggle theme dark light"
                onSelect={() => {
                  setTheme(resolvedTheme === "dark" ? "light" : "dark");
                  onOpenChange(false);
                }}
                className={item}
              >
                {resolvedTheme === "dark" ? (
                  <SunIcon aria-hidden className="size-4" />
                ) : (
                  <MoonIcon aria-hidden className="size-4" />
                )}
                Toggle theme
              </Command.Item>
            </Command.Group>
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
