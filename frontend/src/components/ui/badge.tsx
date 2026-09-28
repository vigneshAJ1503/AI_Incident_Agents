import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "border-transparent bg-neutral-bg text-neutral",
        ok: "border-transparent bg-ok-bg text-ok",
        info: "border-transparent bg-info-bg text-info",
        warn: "border-transparent bg-warn-bg text-warn",
        danger: "border-transparent bg-danger-bg text-danger",
        purple: "border-transparent bg-purple-bg text-purple",
        outline: "bg-transparent text-foreground",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type Tone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export function Badge({
  className,
  tone,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}
