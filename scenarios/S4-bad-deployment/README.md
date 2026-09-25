# S4 — Bad deployment

user-service was rolled out with a nonexistent image tag, so only 1 of 3 replicas is ready. The logs show queue pressure and 503s from callers; the definitive cause (ImagePullBackOff) comes from the K8s agent in PR-019.
