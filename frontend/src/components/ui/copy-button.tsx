"use client";

import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Copies `text` to the clipboard with a toast; the icon flips to a check for a moment. */
export function CopyButton({
  text,
  label = "Copy",
  what = "Copied",
  className,
}: {
  text: string;
  /** accessible name, e.g. "Copy query" */
  label?: string;
  /** toast title, e.g. "Query copied" */
  what?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success(what);
    } catch {
      toast.error("Could not copy", { description: "The browser blocked clipboard access." });
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={label}
      data-testid="copy"
      onClick={() => void copy()}
      className={cn("size-7 text-muted-foreground hover:text-foreground", className)}
    >
      {copied ? <CheckIcon aria-hidden className="text-ok" /> : <CopyIcon aria-hidden />}
    </Button>
  );
}
