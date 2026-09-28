import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Glass card: a tinted translucent fill over the aurora (no backdrop-filter, see globals.css).
 * `interactive` adds the hover lift + glow for cards that navigate or open something.
 */
export function Card({
  className,
  interactive = false,
  ...props
}: React.ComponentProps<"div"> & { interactive?: boolean }) {
  return (
    <div
      data-slot="card"
      className={cn(
        "rounded-xl glass text-card-foreground",
        interactive && "lift cursor-pointer",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div className={cn("flex flex-col gap-1 px-(--pad) pt-(--pad) pb-3", className)} {...props} />
  );
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <h2 className={cn("text-sm leading-none font-semibold tracking-tight", className)} {...props} />
  );
}

export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-xs text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-(--pad) pb-(--pad)", className)} {...props} />;
}
