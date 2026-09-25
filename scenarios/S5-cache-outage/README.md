# S5 — Cache outage

Redis is down. Every service logs `ECONNREFUSED` and falls back to the database, so latency rises everywhere.
