# The Web UI (PR-039): Next.js `output: "standalone"` in a small, non-root image.
# Build context = frontend/ (see .dockerignore), e.g. via deploy/compose/docker-compose.app.yml or:
#   docker build -t aiops/web:0.1.0 frontend
#
# The browser only talks to this container (http://localhost:3100): the build bakes
# NEXT_PUBLIC_API_URL=/api and src/app/api/[...path]/route.ts proxies /api/* (REST + SSE) to
# AIOPS_API_INTERNAL_URL, which is read at runtime (compose: http://aiops-api:8000).
ARG NODE_IMAGE=node:24.19.0-alpine3.23

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_OUTPUT=standalone \
    NEXT_PUBLIC_API_URL=/api
# NEXT_PUBLIC_DEMO=1 builds the static demo instead (no API needed).
ARG NEXT_PUBLIC_DEMO=0
ENV NEXT_PUBLIC_DEMO=${NEXT_PUBLIC_DEMO}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM ${NODE_IMAGE}
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3100 \
    AIOPS_API_INTERNAL_URL=http://aiops-api:8000
# Numeric UID/GID (Kubernetes runAsNonRoot needs a number); files stay root-owned, read-only.
# Security (PR-042, Trivy): apply Alpine security fixes, and drop the package managers the
# standalone server never uses (npm/yarn/corepack bundle their own vulnerable dependencies).
RUN apk upgrade --no-cache \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
       /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn-* \
       /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && addgroup -g 10001 aiops && adduser -D -u 10001 -G aiops aiops
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER 10001:10001
EXPOSE 3100
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=10 \
  CMD node -e "fetch('http://127.0.0.1:3100/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
