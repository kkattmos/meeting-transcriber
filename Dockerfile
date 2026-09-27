# The meeting bot, as one image: Chrome + Xvfb + PulseAudio for recording,
# ffmpeg, the Python venv, yt-dlp and the `claude` CLI.
#
#   docker compose up -d --build        (see docker-compose.yml and README)
#
# ONE container on purpose, not a service per stage. run.lock, the slot queue
# and kill_meeting.sh all identify their owner by PID, and a PID means nothing
# across PID namespaces: a second container would see every lock as stale and
# take it over, and two pipelines would write the same run. Everything —
# the web UI, the resume loop, `docker compose exec` sessions — shares this
# one namespace.
#
# Per-run isolation (display number, audio sink) is still allocated by
# lib/xsession.sh inside the container, because several recordings may run in
# it at once. See CLAUDE.md.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    MEETING_BOT_IN_DOCKER=1 \
    MEETING_BOT_ROOT=/data/bot \
    MEETING_BOT_VENV=/opt/meeting-bot-venv \
    CHROME_PROFILE_DIR=/data/bot/chrome-profile \
    RESOURCE_CACHE_DIR=/data/bot/resources \
    RECORDINGS_DIR=/data/out/recordings \
    TRANSCRIPTS_DIR=/data/out/transcripts \
    FRAMES_DIR=/data/bot/frames \
    SUMMARIES_DIR=/data/out/summaries \
    PDF_DIR=/data/out/pdf \
    CLAUDE_CONFIG_DIR=/root/.claude \
    CLAUDE_CLI_BIN=/root/.local/bin/claude \
    DISABLE_AUTOUPDATER=1 \
    PATH=/opt/meeting-bot-venv/bin:/root/.local/bin:$PATH

# The venv first on PATH, so a bare `python3` in `docker compose exec` is the
# one with the pinned dependencies. (It doesn't exist yet while setup.sh runs,
# which therefore creates it with the system python3, as it should.)
#
# uv makes the pinned install seconds instead of minutes; setup.sh uses it
# when it is on PATH and verifies the same hashes either way.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# The installer and the lockfiles only, so editing the code does not throw
# away the apt/Chrome/venv layer.
COPY setup.sh source_env.sh requirements.txt requirements-browser.txt ./
ARG WITH_LIBREOFFICE=0
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends ca-certificates wget tini \
 && if [ "$WITH_LIBREOFFICE" = "1" ]; then ./setup.sh --with-libreoffice; else ./setup.sh; fi \
 && rm -rf /var/lib/apt/lists/* /root/.cache

# The Claude Code CLI, for the summarizer's subscription billing. Its login
# lives in the claude-config volume (CLAUDE_CONFIG_DIR), not in the image, so
# rebuilding never signs you out; the auto-updater is off because the binary
# would update into a layer that the next `up` throws away — rebuild instead.
RUN wget -qO- https://claude.ai/install.sh | bash \
 && claude --version

COPY . .
RUN chmod +x docker/entrypoint.sh

# 8765: the web UI / trigger. 6080: noVNC, only while first_time_login.sh runs.
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
