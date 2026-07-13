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

# Replace the stock server with our security-header + SPA-fallback config.
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html

EXPOSE 8080
# The base image's CMD already starts nginx as the unprivileged `nginx`
# user; no USER/root escalation is introduced here.
