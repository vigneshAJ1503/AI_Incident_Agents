import { redirect } from "next/navigation";

/** Settings has one page so far: Integrations (PR-046). */
export default function SettingsPage() {
  redirect("/settings/integrations");
}
