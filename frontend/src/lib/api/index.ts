import { API_URL, DEMO_MODE, DEMO_SPEED } from "@/lib/config";

import type { ApiClient } from "./client";
import { DemoClient } from "./demo-client";
import { HttpClient } from "./http-client";

let client: ApiClient | null = null;

/** The process-wide API client: DemoClient when NEXT_PUBLIC_DEMO=1, HttpClient otherwise. */
export function getClient(): ApiClient {
  client ??= DEMO_MODE
    ? new DemoClient({ speed: DEMO_SPEED })
    : new HttpClient({ baseUrl: API_URL });
  return client;
}

export * from "./client";
