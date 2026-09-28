import { z } from "zod";

import { ContractError } from "./client";

/** Parse `data` with `schema`, turning a mismatch into a readable ContractError. */
export function parseOrThrow<S extends z.ZodType>(
  schema: S,
  data: unknown,
  endpoint: string,
): z.infer<S> {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new ContractError(endpoint, z.prettifyError(result.error));
  }
  return result.data;
}
