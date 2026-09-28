"use client";

import { MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  return (
    <Tooltip content={label}>
      <Button
        variant="ghost"
        size="icon"
        aria-label={label}
        data-testid="theme-toggle"
        onClick={() => setTheme(dark ? "light" : "dark")}
      >
        <SunIcon
          aria-hidden
          className="scale-100 rotate-0 transition-transform dark:scale-0 dark:-rotate-90"
        />
        <MoonIcon
          aria-hidden
          className="absolute scale-0 rotate-90 transition-transform dark:scale-100 dark:rotate-0"
        />
      </Button>
    </Tooltip>
  );
}
