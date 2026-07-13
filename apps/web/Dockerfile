# SOA web artifact image (REL-001).
#
# A Node builder produces the static Vite bundle; the runtime stage serves
# it from the UNPRIVILEGED nginx base (runs as uid 101, listens on 8080).
# No Node, no build tools, and no source survive into the final image —
# only the compiled static assets and the nginx config. Build from the
# REPO ROOT (the pnpm workspace root):
#   docker build -f apps/web/Dockerfile -t soa-web .

FROM node:22-bookworm-slim AS builder

RUN corepack enable

WORKDIR /app
COPY . .
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @soa/web run build


FROM nginxinc/nginx-unprivileged:1.27-bookworm AS runtime

# Apply Debian security updates: the pinned base tag lags the security
# repo (openssl/perl patches), so pull current fixes rather than suppress
# fixable CVEs. Root only for the apt step; drop straight back to the
# unprivileged nginx user (REL-002 digest-pinning refreshes the base).
USER root
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Replace the stock server with our security-header + SPA-fallback config.
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html

# Back to the unprivileged user (uid 101) — the runtime never runs as root.
USER nginx
EXPOSE 8080
