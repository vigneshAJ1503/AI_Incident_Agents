"use client";

import { Rows3Icon, Rows4Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";
import { useDensity } from "@/lib/density";

export function DensityToggle() {
  const [density, setDensity] = useDensity();
  const compact = density === "compact";
  return (
    <Tooltip content={compact ? "Density: compact" : "Density: comfortable"}>
      <Button
        variant="ghost"
        size="icon"
        aria-label="Compact density"
        aria-pressed={compact}
        data-testid="density-toggle"
        onClick={() => setDensity(compact ? "comfortable" : "compact")}
      >
        {compact ? <Rows4Icon aria-hidden /> : <Rows3Icon aria-hidden />}
      </Button>
    </Tooltip>
  );
}
