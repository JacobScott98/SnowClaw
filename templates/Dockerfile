ARG OPENCLAW_VERSION=latest
FROM ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}

# Stage default files into a defaults directory so the entrypoint can
# seed them into the volume-mounted OPENCLAW_HOME on first run.
# openclaw.json is NOT baked in — it's uploaded to the stage by deploy/push.
COPY --chown=1000:1000 skills/ /opt/snowclaw/defaults/skills/
COPY --chown=1000:1000 workspace/ /opt/snowclaw/defaults/workspace/

# Copy Snowflake connection config
COPY --chown=1000:1000 config/connections.toml /home/node/.snowflake/connections.toml

# Install cortex code
RUN SKIP_PODMAN=1 NON_INTERACTIVE=1 \
 sh -c 'curl -LsS https://ai.snowflake.com/static/cc-scripts/install.sh | sh'

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