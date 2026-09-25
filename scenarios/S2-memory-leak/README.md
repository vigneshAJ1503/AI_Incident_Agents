# S2 — Memory leak / OOM restarts

order-service leaks memory. From T-20m the logs show `GC overhead` warnings, then `OutOfMemoryError`, and the service restarts every ~4 minutes.
