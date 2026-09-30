FROM node:22.22.0-bookworm-slim AS node_runtime
FROM python:3.12-slim-bookworm
ARG CLAUDE_VERSION=2.1.269
ARG CODEX_VERSION=0.154.0
ARG OPENCODE_VERSION=1.18.30
ARG CURSOR_VERSION=2026.09.10-fd3934a
ARG GEMINI_VERSION=0.60.0
ARG KILOCODE_VERSION=7.8.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git ripgrep sqlite3 libatomic1 procps squashfs-tools \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/ /usr/local/
RUN curl --fail --silent --show-error --location https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && bash /tmp/install-claude.sh "${CLAUDE_VERSION}" \
    && install -m 0755 /root/.local/bin/claude /usr/local/bin/claude \
    && /usr/local/bin/claude --version
# Slimmed build: only claude, opencode, and kilocode are installed for now.
# The other agents are commented out temporarily; uncomment to restore them.
# RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
#     && codex --version
RUN npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version
# RUN npm install --global "@google/gemini-cli@${GEMINI_VERSION}" \
#     && gemini --version
RUN npm install --global "@kilocode/cli@${KILOCODE_VERSION}" \
    && kilo --version
# RUN detected_arch="${TARGETARCH:-$(uname -m)}" \
#     && case "${detected_arch}" in \
#       amd64|x86_64) cursor_arch=x64 ;; \
#       arm64|aarch64) cursor_arch=arm64 ;; \
#       *) echo "Unsupported Cursor architecture: ${detected_arch}" >&2; exit 1 ;; \
#     esac \
#     && mkdir -p "/opt/cursor-agent/${CURSOR_VERSION}" \
#     && curl --fail --silent --show-error --location \
#       "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/${cursor_arch}/agent-cli-package.tar.gz" \
#       | tar --strip-components=1 -xzf - -C "/opt/cursor-agent/${CURSOR_VERSION}" \
#     && ln -s "/opt/cursor-agent/${CURSOR_VERSION}/cursor-agent" /usr/local/bin/cursor-agent \
#     && cursor-agent --version
# COPY trace_lab/install_extended.py /tmp/install_extended.py
# RUN python3 /tmp/install_extended.py && rm /tmp/install_extended.py
# COPY trace_lab/install_zcode.py /tmp/install_zcode.py
# RUN python3 /tmp/install_zcode.py && rm /tmp/install_zcode.py
# COPY trace_lab/install_kimi.py /tmp/install_kimi.py
# RUN python3 /tmp/install_kimi.py && rm /tmp/install_kimi.py
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir /workspace /relay
COPY trace_lab /opt/trace-lab/trace_lab
ENV PYTHONPATH=/opt/trace-lab PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /workspace
USER agent
CMD ["python3", "-m", "trace_lab.relay"]
