# SnowClaw — OpenClaw on Snowflake Container Services
# Layers Snowflake-specific config, plugins, and deployment on top of upstream OpenClaw.

ARG OPENCLAW_VERSION=latest
FROM ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}

# Stage config and plugins into a defaults directory so the entrypoint can
# sync them into the volume-mounted OPENCLAW_HOME at runtime.
COPY --chown=1000:1000 config/openclaw.json /opt/snowclaw/defaults/openclaw.json
COPY --chown=1000:1000 plugins/cortex-tools /opt/snowclaw/defaults/plugins/cortex-tools
COPY --chown=1000:1000 plugins/cortex-code /opt/snowclaw/defaults/plugins/cortex-code

# Copy Snowflake connection config
COPY --chown=1000:1000 config/connections.toml /home/node/.snowflake/connections.toml

# TODO: Install Cortex Code once the installer is available
RUN curl -LsS https://ai.snowflake.com/static/cc-scripts/install.sh | sh

# Entrypoint wrapper syncs defaults into the volume-mounted home dir
COPY --chown=1000:1000 scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:18789/ || exit 1
