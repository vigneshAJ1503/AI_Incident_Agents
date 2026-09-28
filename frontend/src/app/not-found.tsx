"use client";

import { CompassIcon } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/states";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="py-10">
      <h1 className="sr-only">Page not found</h1>
      <EmptyState
        icon={CompassIcon}
        title="Page not found"
        action={
          <Button asChild variant="outline" size="sm">
            <Link href="/">Back to the dashboard</Link>
          </Button>
        }
      >
        This page doesn&apos;t exist.
      </EmptyState>
    </div>
  );
}
