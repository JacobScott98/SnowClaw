ARG OPENCLAW_VERSION=latest
FROM ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}

# Stage config and plugins into a defaults directory so the entrypoint can
# sync them into the volume-mounted OPENCLAW_HOME at runtime.
COPY --chown=1000:1000 config/openclaw.json /opt/snowclaw/defaults/openclaw.json
COPY --chown=1000:1000 skills/ /opt/snowclaw/defaults/skills/

# Copy Snowflake connection config
COPY --chown=1000:1000 config/connections.toml /home/node/.snowflake/connections.toml

# Install cortex code
RUN SKIP_PODMAN=1 NON_INTERACTIVE=1 \
 sh -c 'curl -LsS https://ai.snowflake.com/static/cc-scripts/install.sh | sh'

# Ensure the openclaw home dir exists with correct ownership so volume mounts work
USER root
RUN mkdir -p /home/node/.openclaw && chown 1000:1000 /home/node/.openclaw
USER node

# Entrypoint wrapper syncs defaults into the volume-mounted home dir
COPY --chown=1000:1000 scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["node", "openclaw.mjs", "gateway", "--allow-unconfigured"]

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:18789/ || exit 1