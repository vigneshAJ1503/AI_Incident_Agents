/**
 * Same-origin proxy to the aiops API (PR-039).
 *
 * The container build sets NEXT_PUBLIC_API_URL=/api, so the browser only ever talks to the Web
 * UI's own origin (http://localhost:3100): no CORS, no second port to expose, and one URL to
 * open. This handler forwards /api/* to AIOPS_API_INTERNAL_URL (read at *runtime*, e.g.
 * http://aiops-api:8000 inside the compose network) and streams the response body through
 * untouched, which keeps Server-Sent Events live. `make ui-dev` still calls the API directly
 * (NEXT_PUBLIC_API_URL defaults to http://localhost:8000/api).
 */
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const UPSTREAM = (process.env.AIOPS_API_INTERNAL_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

/** Request headers worth forwarding (no cookies, no hop-by-hop headers). */
const FORWARD_REQUEST = ["accept", "content-type", "last-event-id", "x-api-key", "x-request-id"];
/** Response headers worth returning (the body is re-streamed, so no length/encoding). */
const FORWARD_RESPONSE = ["content-type", "cache-control", "x-request-id", "content-disposition"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = `${UPSTREAM}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const name of FORWARD_REQUEST) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      signal: req.signal, // the browser closing an SSE stream closes the upstream one
      cache: "no-store",
      redirect: "manual",
    });
  } catch (err) {
    return Response.json(
      {
        error: {
          code: "api_unreachable",
          message: `The Web UI cannot reach the API at ${UPSTREAM} (${(err as Error).message})`,
        },
      },
      { status: 502 },
    );
  }
  const out = new Headers();
  for (const name of FORWARD_RESPONSE) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (out.get("content-type")?.startsWith("text/event-stream")) {
    // no-transform: Next's gzip must not buffer the stream; X-Accel-Buffering for any proxy.
    out.set("cache-control", "no-cache, no-transform");
    out.set("x-accel-buffering", "no");
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
