/**
 * Deep-link templates as the backend renders them from `capabilities.<cap>.settings`
 * (profiles/local*). The demo mirrors the local stack's URLs.
 */
import { iso } from "./rng";

const REPO = "https://github.com/vigneshAJ1503/AI_Incident_Agents/blob/main";

export const kibana = (index: string, query: string, start: number, end: number): string =>
  `http://localhost:5601/app/discover#/?_g=(time:(from:'${iso(start)}',to:'${iso(end)}'))` +
  `&_a=(index:'${index}',query:(language:kuery,query:'${encodeURIComponent(query)}'))`;

export const grafana = (service: string, panel: string, start: number, end: number): string =>
  `http://localhost:3000/d/service-overview/service-overview?var-service=${service}` +
  `&from=${start}&to=${end}&viewPanel=${encodeURIComponent(panel)}`;

export const grafanaK8s = (deployment: string, start: number, end: number): string =>
  `http://localhost:3000/d/k8s-workloads/k8s-workloads?var-deployment=${deployment}&from=${start}&to=${end}`;

export const alertmanager = (alertname: string): string =>
  `http://localhost:9093/#/alerts?filter=${encodeURIComponent(`{alertname="${alertname}"}`)}`;

export const ticket = (key: string): string => `http://localhost:8109/browse/${key}`;

export const runbook = (file: string, anchor?: string): string =>
  `${REPO}/knowledge-base/runbooks/${file}${anchor ? `#${anchor}` : ""}`;

export const serviceDoc = (file: string): string => `${REPO}/knowledge-base/services/${file}`;
