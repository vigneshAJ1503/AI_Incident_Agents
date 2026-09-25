---
title: Bad deployment and rollback
type: runbook
services: [payment-service, order-service, user-service, inventory-service]
tags: [deployment, rollback, release, imagepullbackoff, replicas, kubernetes, capacity]
alerts: [KubeDeploymentReplicasMismatch, KubePodNotReady, HighErrorRate, HighLatency]
owner: platform-sre
last_reviewed: 2026-09-01
---
# Bad deployment and rollback

## Summary
Most incidents start with a change. When symptoms begin within minutes of a rollout, **roll back first and investigate later**. Rolling back is fast, safe and reversible. Debugging in production isn't.

Common ways a deployment goes bad:
- the new image can't start: **ImagePullBackOff** (wrong tag, missing image, registry auth) or **CrashLoopBackOff** (bad config, missing secret);
- the new version starts but misbehaves: new errors, bad config values (pool sizes, timeouts), memory leaks;
- the rollout stalls half-way, leaving **fewer ready replicas** than desired, so the remaining pods are overloaded.

## Symptoms
- A new `version` appears in the logs right before the errors start.
- Reduced capacity: `Request queue depth high: 57 pending (ready replicas 1/3)`.
- `kubectl get deploy` shows READY `1/3`, and new pods are stuck in `ImagePullBackOff`, `ErrImagePull` or `CrashLoopBackOff`.
- Callers see HTTP 503 or elevated latency because the remaining replicas are overloaded.
- Alerts: `KubeDeploymentReplicasMismatch`, `KubePodNotReady`.

## Known issues
- **Nonexistent image tag (INC-2026-058).** user-service was deployed with a tag that was never pushed. New pods stayed in `ImagePullBackOff`, only 1 of 3 replicas was ready, and login and checkout requests queued up and failed. Fix: roll back, then add a CI check that the image exists before the manifest is updated.
- **Config regression in a release.** See [database-connection-pool](database-connection-pool.md): payment-service `v1.8.2` lowered `DB_POOL_SIZE`.

## Diagnosis
### 1. Correlate with the rollout
- `kubectl rollout history deployment/<service> -n prod` and `kubectl rollout status deployment/<service> -n prod`.
- Compare the first error or first warning with the rollout start time. Within ±5 minutes counts as a strong correlation.

### 2. Why is the new version unhealthy?
- `kubectl get pods -n prod -l app=<service>`, then `kubectl describe pod <new-pod>` and read the Events:
  - `Failed to pull image ... not found`: a bad tag. Check the registry.
  - `Back-off restarting failed container`: read `kubectl logs <pod> --previous`.
- Diff the manifests and ConfigMaps between the old and new ReplicaSets.

### 3. Assess the impact
- Ready replicas compared with desired, and the traffic per ready pod.
- Error rate and latency on the service and on its callers.

## Mitigation
1. **Roll back:** `kubectl rollout undo deployment/<service> -n prod` (or `--to-revision=<n>`).
2. If the rollback is blocked (for example the old image was deleted), scale up the old ReplicaSet directly, or re-deploy the last known-good tag.
3. Pause further rollouts: `kubectl rollout pause deployment/<service> -n prod`. Freeze the pipeline for this service.
4. Tell the callers' owners that capacity is reduced; consider shedding load.

## Rollback
- `kubectl rollout undo deployment/<service> -n prod`
- `kubectl rollout status deployment/<service> -n prod --timeout=5m`
- Check that READY equals the desired replicas, that the error rate is back to baseline, and that the new pattern is gone.
- Revert the commit or manifest change in git so GitOps doesn't re-apply the bad version.

## Escalation
- The owning team of the service (see its service doc).
- If the registry or cluster is at fault (image pulls fail for every service): `#platform-oncall`.

## Prevention
- Progressive delivery: canary or `maxUnavailable: 0` with readiness gates.
- CI verifies that the image tag exists before the manifest is changed.
- Automatic rollback when the error rate SLO burns during a rollout.
