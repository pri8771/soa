# SOA web artifact image (REL-001).
#
# A Node builder produces the static Vite bundle; the runtime stage serves
# it from the UNPRIVILEGED nginx base (runs as uid 101, listens on 8080).
# No Node, no build tools, and no source survive into the final image —
# only the compiled static assets and the nginx config. Build from the
# REPO ROOT (the pnpm workspace root):
#   docker build -f apps/web/Dockerfile -t soa-web .

FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS builder

RUN corepack enable

WORKDIR /app
COPY . .
# Vite embeds public configuration at build time. Release publication passes
# the environment's HTTPS API origin; the /api fallback remains useful only
# for hosts that deliberately proxy that path to the API.
ARG VITE_API_BASE_URL=/api
ARG VITE_AUTH_MODE
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
ARG VITE_FIREBASE_APP_ID
ARG VITE_FIREBASE_TENANT_ID
ARG VITE_FIREBASE_PROVIDER_ID
ARG VITE_FIREBASE_LOGIN_METHOD=redirect
ARG VITE_OIDC_ISSUER
ARG VITE_OIDC_CLIENT_ID
ARG VITE_OIDC_SCOPE="openid profile email offline_access"
ARG VITE_OIDC_AUDIENCE
ARG VITE_OIDC_BEARER_TOKEN=id_token
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_AUTH_MODE=${VITE_AUTH_MODE}
ENV VITE_FIREBASE_API_KEY=${VITE_FIREBASE_API_KEY}
ENV VITE_FIREBASE_AUTH_DOMAIN=${VITE_FIREBASE_AUTH_DOMAIN}
ENV VITE_FIREBASE_PROJECT_ID=${VITE_FIREBASE_PROJECT_ID}
ENV VITE_FIREBASE_APP_ID=${VITE_FIREBASE_APP_ID}
ENV VITE_FIREBASE_TENANT_ID=${VITE_FIREBASE_TENANT_ID}
ENV VITE_FIREBASE_PROVIDER_ID=${VITE_FIREBASE_PROVIDER_ID}
ENV VITE_FIREBASE_LOGIN_METHOD=${VITE_FIREBASE_LOGIN_METHOD}
ENV VITE_OIDC_ISSUER=${VITE_OIDC_ISSUER}
ENV VITE_OIDC_CLIENT_ID=${VITE_OIDC_CLIENT_ID}
ENV VITE_OIDC_SCOPE=${VITE_OIDC_SCOPE}
ENV VITE_OIDC_AUDIENCE=${VITE_OIDC_AUDIENCE}
ENV VITE_OIDC_BEARER_TOKEN=${VITE_OIDC_BEARER_TOKEN}
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @soa/web run build


FROM nginxinc/nginx-unprivileged:1.27-bookworm@sha256:f9dfa9c20b2b0b7c5cc830374f22f23dee3f750b6c5291ca7e0330b5c88e6403 AS runtime

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
