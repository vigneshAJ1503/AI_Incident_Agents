"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef, useState } from "react";

import { getClient, type StreamState } from "@/lib/api";
import { qk } from "@/lib/queries";

import { initialLiveState, liveReducer } from "./reducer";

/** Subscribe to an investigation's SSE stream and reduce it into UI state. */
export function useLiveInvestigation(id: string, enabled: boolean) {
  const [state, dispatch] = useReducer(liveReducer, initialLiveState);
  const [stream, setStream] = useState<StreamState>("closed");
  const [streamError, setStreamError] = useState<string | null>(null);
  const qc = useQueryClient();
  const lastSeq = useRef(0);

  useEffect(() => {
    lastSeq.current = state.lastSeq;
  }, [state.lastSeq]);

  useEffect(() => {
    if (!enabled) return;
    dispatch({ type: "connect" });
    const stop = getClient().subscribe(
      id,
      {
        onEvent: (event) => {
          dispatch({ type: "event", event });
          if (event.type === "investigation_finished" || event.type === "report_ready") {
            void qc.invalidateQueries({ queryKey: qk.investigation(id) });
          }
          if (event.type === "investigation_finished") {
            void qc.invalidateQueries({ queryKey: ["investigations"] });
            void qc.invalidateQueries({ queryKey: ["dashboard"] });
          }
          if (event.type === "approval_requested")
            void qc.invalidateQueries({ queryKey: ["approvals"] });
        },
        onState: setStream,
        onError: (err) => setStreamError(err.message),
      },
      lastSeq.current,
    );
    return stop;
  }, [id, enabled, qc]);

  return { state, stream, streamError };
}
