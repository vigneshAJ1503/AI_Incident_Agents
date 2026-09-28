import type { Metadata } from "next";

import { InvestigationView } from "@/components/investigation/investigation-view";

export const metadata: Metadata = { title: "Investigation" };

export default async function InvestigationPage({ params }: PageProps<"/investigations/[id]">) {
  const { id } = await params;
  return <InvestigationView id={id} />;
}
