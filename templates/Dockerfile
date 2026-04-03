ARG OPENCLAW_VERSION=latest
FROM --platform=linux/amd64 ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}

# Stage default files into a defaults directory so the entrypoint can
# seed them into the volume-mounted OPENCLAW_HOME on first run.
# openclaw.json is NOT baked in — it's uploaded to the stage by deploy/push.
COPY --chown=1000:1000 skills/ /opt/snowclaw/defaults/skills/
COPY --chown=1000:1000 workspace/ /opt/snowclaw/defaults/workspace/

# Install Cortex Code manually — the official install.sh runs `cortex --help`
# to initialize the binary, which fails under QEMU (exit 132 / SIGILL) when
# cross-building amd64 images on Apple Silicon.  We replicate the install
# steps without that final exec so the image builds cleanly on any host.
RUN set -e \
    && S3_BUCKET="sfc-repo.snowflakecomputing.com" \
    && S3_PREFIX="cortex-code-cli/a4643c4278" \
    && VERSION=$(curl -fsSL "https://${S3_BUCKET}/${S3_PREFIX}/stable_version.txt" | tr -d '[:space:]') \
    && ENCODED_VERSION=$(echo "$VERSION" | sed 's/+/%2B/g') \
    && MANIFEST_URL="https://${S3_BUCKET}/${S3_PREFIX}/${ENCODED_VERSION}/manifest.json" \
    && MANIFEST=$(curl -fsSL "$MANIFEST_URL") \
    && TAR_NAME=$(echo "$MANIFEST" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*linux-amd64\.tar\.gz"' | head -1 | grep -o '"[^"]*\.tar\.gz"' | tr -d '"') \
    && ENCODED_TAR=$(echo "$TAR_NAME" | sed 's/+/%2B/g') \
    && DOWNLOAD_URL="https://${S3_BUCKET}/${S3_PREFIX}/${ENCODED_VERSION}/${ENCODED_TAR}" \
    && INSTALL_DIR="$HOME/.local/share/cortex/${VERSION}" \
    && BIN_DIR="$HOME/.local/bin" \
    && mkdir -p "$BIN_DIR" "$INSTALL_DIR" /tmp/cc-install \
    && curl -fsSL "$DOWNLOAD_URL" -o /tmp/cc-install/cortex.tar.gz \
    && tar xzf /tmp/cc-install/cortex.tar.gz -C /tmp/cc-install \
    && TAR_BASENAME=$(echo "$TAR_NAME" | sed 's/\.tar\.gz$//') \
    && cp -a /tmp/cc-install/"$TAR_BASENAME"/. "$INSTALL_DIR/" \
    && if [ -f "$INSTALL_DIR/dist/index.js" ]; then \
         printf '#!/bin/sh\nexec node "%s/dist/index.js" "$@"\n' "$INSTALL_DIR" > "$BIN_DIR/cortex"; \
       else \
         chmod +x "$INSTALL_DIR/cortex"; \
         ln -sf "$INSTALL_DIR/cortex" "$BIN_DIR/cortex"; \
       fi \
    && chmod +x "$BIN_DIR/cortex" \
    && rm -rf /tmp/cc-install

# Switch to root for system-level installs
USER root

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Ensure the openclaw home dir exists with correct ownership so volume mounts work
RUN mkdir -p /home/node/.openclaw && chown 1000:1000 /home/node/.openclaw

# Entrypoint runs as root to enforce file permissions, then drops to node.
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["node", "openclaw.mjs", "gateway", "--allow-unconfigured"]

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:18789/ || exit 1