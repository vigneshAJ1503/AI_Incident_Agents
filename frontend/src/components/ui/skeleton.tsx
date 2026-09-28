import type * as React from "react";

import { cn } from "@/lib/utils";

/** Glass placeholder with a light sweep (a transform-only animation; static under reduced motion). */
export function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      aria-hidden
      data-slot="skeleton"
      className={cn(
        "relative overflow-hidden rounded-md border border-glass-border bg-glass",
        "after:absolute after:inset-0 after:-translate-x-full after:animate-[glass-shimmer_1.6s_ease-in-out_infinite] after:bg-linear-to-r after:from-transparent after:via-foreground/6 after:to-transparent motion-reduce:after:hidden",
        className,
      )}
      {...props}
    />
  );
}
