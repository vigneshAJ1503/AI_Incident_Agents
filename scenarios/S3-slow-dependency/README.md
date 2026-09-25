# S3 — Slow downstream dependency

inventory-service `stock_levels` queries take 3–5 s. order-service calls to inventory time out after 3 s (HTTP 504). Tests cross-service reasoning: the symptom is in order-service, the cause is in inventory-service.
