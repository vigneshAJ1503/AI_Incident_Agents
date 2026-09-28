/** Build-time configuration (NEXT_PUBLIC_* values are inlined by Next.js). */

export const DEMO_MODE = process.env.NEXT_PUBLIC_DEMO === "1";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api").replace(
  /\/$/,
  "",
);

const speed = Number(process.env.NEXT_PUBLIC_DEMO_SPEED ?? "4");
export const DEMO_SPEED = Number.isFinite(speed) && speed > 0 ? speed : 4;

/** Who approves/denies in the UI until auth lands (PR-035+ reserves X-API-Key). */
export const CURRENT_USER = "you";
