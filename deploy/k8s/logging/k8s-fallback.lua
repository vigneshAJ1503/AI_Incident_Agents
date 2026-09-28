-- Fallback Kubernetes metadata from the tail tag (PR-P3, docs/setup/logging.md).
--
-- The kubernetes filter enriches a record by asking the API server about its pod. When the
-- pod is already gone (a pod that lived a few seconds during a rollout, or lines read after
-- a Fluent Bit / node restart while the API or DNS wasn't reachable yet), the lookup fails
-- and the record used to reach Elasticsearch with NO `kubernetes` object: a null
-- `kubernetes.namespace_name`, invisible to namespace filters.
--
-- The tag still names the pod: kube.var.log.containers.<pod>_<namespace>_<container>-<id>.log
-- so fill namespace/pod/container from it and mark the record. Labels (and so
-- `kubernetes.labels.app`) can't be recovered for a deleted pod; the app's own JSON
-- `service` field still identifies it.
function k8s_fallback(tag, timestamp, record)
    local k8s = record["kubernetes"]
    if type(k8s) == "table" and k8s["namespace_name"] ~= nil then
        return 0, timestamp, record
    end
    local pod, namespace, container = string.match(
        tag, "containers%.([^_]+)_([^_]+)_(.+)%-%x+%.log$"
    )
    if namespace == nil then
        return 0, timestamp, record
    end
    if type(k8s) ~= "table" then
        k8s = {}
    end
    k8s["namespace_name"] = namespace
    k8s["pod_name"] = pod
    k8s["container_name"] = container
    k8s["metadata_source"] = "tag"
    record["kubernetes"] = k8s
    return 1, timestamp, record
end
