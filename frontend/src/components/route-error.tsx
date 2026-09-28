"use client";

import { useEffect } from "react";

import { ErrorState } from "@/components/states";

/** Shared body of every route's error.tsx boundary (retry = re-render the segment). */
export function RouteError({
  error,
  reset,
  title,
}: {
  error: Error & { digest?: string };
  reset: () => void;
  title: string;
}) {
  useEffect(() => {
    console.error(`[${title}]`, error);
  }, [error, title]);
  return (
    <div className="py-10">
      <h1 className="sr-only">{title}</h1>
      <ErrorState error={error} onRetry={reset} />
    </div>
  );
}
