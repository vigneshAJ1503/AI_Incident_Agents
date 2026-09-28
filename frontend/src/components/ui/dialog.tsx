"use client";

import { XIcon } from "lucide-react";
import { Dialog as D } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

const overlay =
  "fixed inset-0 z-50 bg-black/50 backdrop-blur-[2px] data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0";

export function DialogContent({
  className,
  children,
  hideClose,
  ...props
}: React.ComponentProps<typeof D.Content> & { hideClose?: boolean }) {
  return (
    <D.Portal>
      <D.Overlay className={overlay} />
      <D.Content
        className={cn(
          "fixed top-1/2 left-1/2 z-50 grid max-h-[90vh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-xl border bg-popover p-6 text-popover-foreground shadow-xl",
          "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95",
          className,
        )}
        {...props}
      >
        {children}
        {!hideClose && (
          <D.Close className="absolute top-4 right-4 rounded-sm opacity-70 transition-opacity hover:opacity-100">
            <XIcon className="size-4" />
            <span className="sr-only">Close</span>
          </D.Close>
        )}
      </D.Content>
    </D.Portal>
  );
}

export function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-1.5 pr-6", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("flex flex-col-reverse gap-2 sm:flex-row sm:justify-end", className)}
      {...props}
    />
  );
}

export function DialogTitle({ className, ...props }: React.ComponentProps<typeof D.Title>) {
  return <D.Title className={cn("text-lg leading-none font-semibold", className)} {...props} />;
}

export function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof D.Description>) {
  return <D.Description className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

/** Side drawer built on the same primitive. */
export function SheetContent({
  className,
  children,
  side = "right",
  ...props
}: React.ComponentProps<typeof D.Content> & { side?: "right" | "left" }) {
  return (
    <D.Portal>
      <D.Overlay className={overlay} />
      <D.Content
        className={cn(
          "fixed inset-y-0 z-50 flex w-full flex-col gap-4 overflow-y-auto bg-popover p-6 text-popover-foreground shadow-xl sm:max-w-lg",
          side === "right"
            ? "right-0 border-l data-[state=closed]:animate-out data-[state=closed]:slide-out-to-right data-[state=open]:animate-in data-[state=open]:slide-in-from-right"
            : "left-0 border-r data-[state=closed]:animate-out data-[state=closed]:slide-out-to-left data-[state=open]:animate-in data-[state=open]:slide-in-from-left",
          className,
        )}
        {...props}
      >
        {children}
        <D.Close className="absolute top-4 right-4 rounded-sm opacity-70 transition-opacity hover:opacity-100">
          <XIcon className="size-4" />
          <span className="sr-only">Close</span>
        </D.Close>
      </D.Content>
    </D.Portal>
  );
}
