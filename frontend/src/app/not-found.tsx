"use client";

import { CompassIcon } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/states";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="py-10">
      <h1 className="sr-only">Page not found</h1>
      <EmptyState icon={CompassIcon} title="Page not found">
        <p>This page doesn&apos;t exist.</p>
        <Button asChild variant="outline" size="sm" className="mt-3">
          <Link href="/">Back to the dashboard</Link>
        </Button>
      </EmptyState>
    </div>
  );
}
