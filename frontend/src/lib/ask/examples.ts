/** Example questions for the chat box (PR-041). */

/** The recorded scenario questions (scenarios/S1..S5): what replay/demo mode can investigate. */
export const SCENARIO_QUESTIONS = [
  { q: "Payment API is returning HTTP 500 in production", hint: "S1 · DB pool", scenario: "S1" },
  { q: "Orders are failing intermittently in production", hint: "S2 · OOM", scenario: "S2" },
  { q: "Why are orders timing out in production?", hint: "S3 · slow dependency", scenario: "S3" },
  {
    q: "Login and checkout requests are failing in production",
    hint: "S4 · bad deploy",
    scenario: "S4",
  },
  { q: "Payments are slow in production", hint: "S5 · cache", scenario: "S5" },
] as const;

/** Platform questions: answered from live platform data, no investigation started. */
export const TRY_ASKING = [
  "What agents are running now?",
  "Which agents do you have?",
  "Is the LLM configured?",
] as const;
