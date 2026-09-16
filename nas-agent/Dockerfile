FROM python:3.13.7-slim-bookworm

ARG APP_UID=568
ARG APP_GID=568
RUN groupadd --gid "${APP_GID}" nas-agent \
    && useradd --no-log-init --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /nonexistent --shell /usr/sbin/nologin nas-agent

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 568:568
ENTRYPOINT ["butters-nas-agent"]
CMD ["--config", "/config/agent.toml", "--agent-credentials", "/run/secrets/butters_agent_credentials", "--truenas-read-key", "/run/secrets/truenas_read_api_key"]
