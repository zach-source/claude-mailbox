# syntax=docker/dockerfile:1.7
#
# Low-CVE build: multi-stage so build tooling (pip, apt cache, curl) never
# reaches the final image; the runtime stage installs only the two things bd
# hard-requires beyond Python — git and ca-certificates — via
# --no-install-recommends on top of the official `python:*-slim` base, and
# runs as a non-root user. `bd init`/`bd` shell out to `git` themselves (git
# repo bookkeeping for .beads/), so a fully shell-less distroless base isn't
# an option here; this is the leanest base that still satisfies that.
#
# Known residual CVE clusters (tracked, not silently ignored — see the CI
# scan job, which uploads full results to the repo's Security tab):
#   - Debian's `git` package hard-Depends on perl + liberror-perl (used by
#     git-am/git-send-email etc., not by the plain init/add/commit calls bd
#     makes) and on libcurl/libexpat for smart-HTTP transport bd never uses
#     here. There's no perl-free `git` package on Debian; a from-source
#     `NO_PERL=1` build was evaluated and rejected as disproportionate
#     hand-rolling of a security-critical dependency for this project's size.
#     Alpine's `git` avoids perl, but bd's release binary is glibc-linked and
#     only runs on musl via the `gcompat` shim — an untested-under-load
#     compatibility layer for a stateful embedded-database binary, judged a
#     worse risk than the extra Debian CVEs.
#   - `bd`'s own release binary bundles somewhat dated golang.org/x/crypto,
#     golang.org/x/net, and grpc-go — fixable only by upstream (gastownhall/
#     beads) shipping a rebuild; bumping BD_VERSION picks that up.
# The scheduled weekly rebuild (see .github/workflows/docker-publish.yml)
# re-pulls both base images so Debian-side security patches land without a
# code change as soon as upstream publishes them.

ARG PYTHON_VERSION=3.11
ARG DEBIAN_CODENAME=bookworm
ARG BD_VERSION=1.1.0

# ── bd (beads) CLI ───────────────────────────────────────────────────────────
# Fetched as a pinned, checksum-verified release binary (not built from
# source) — beads' embedded-Dolt storage needs cgo, which a from-source
# `go install` would require a full C toolchain for; the release binary
# already has it built in.
FROM debian:${DEBIAN_CODENAME}-slim AS bd-fetch
ARG BD_VERSION
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/bd
RUN set -eu; \
    asset="beads_${BD_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL -o "$asset" \
      "https://github.com/gastownhall/beads/releases/download/v${BD_VERSION}/${asset}"; \
    curl -fsSL -o checksums.txt \
      "https://github.com/gastownhall/beads/releases/download/v${BD_VERSION}/checksums.txt"; \
    grep -q " $asset\$" checksums.txt; \
    sha256sum --ignore-missing -c checksums.txt; \
    tar xzf "$asset" bd; \
    install -m 0755 bd /usr/local/bin/bd

# ── Python deps ──────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_CODENAME} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --target=/deps .

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_CODENAME} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /data --uid 65532 --user-group mailbox

COPY --from=bd-fetch /usr/local/bin/bd /usr/local/bin/bd
COPY --from=builder /deps /deps

ENV PYTHONPATH=/deps \
    HOME=/data \
    MAILBOX_TRANSPORT=http \
    MAILBOX_HTTP_HOST=0.0.0.0 \
    MAILBOX_HTTP_PORT=8000 \
    MAILBOX_GLOBAL=0 \
    MAILBOX_WORKSPACE=/data \
    GIT_AUTHOR_NAME=claude-mailbox \
    GIT_AUTHOR_EMAIL=mailbox@localhost \
    GIT_COMMITTER_NAME=claude-mailbox \
    GIT_COMMITTER_EMAIL=mailbox@localhost

# /data holds the local bd database (MAILBOX_WORKSPACE) — mount a volume
# there for persistence across container restarts. Run `bd init` against it
# once before first start (see README "Docker" section).
USER mailbox
WORKDIR /data
EXPOSE 8000
ENTRYPOINT ["python3", "-m", "claude_mailbox.server"]
