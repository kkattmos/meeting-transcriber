# The meeting bot, as one image: Chrome + Xvfb + PulseAudio for recording,
# ffmpeg, the Python venv, yt-dlp + deno and the `claude` CLI.
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
    MEETING_BROWSER=chrome \
    CHROME_PROFILE_DIR=/data/bot/chrome-profile \
    FIREFOX_PROFILE_DIR=/data/bot/firefox-profile \
    RESOURCE_CACHE_DIR=/data/bot/resources \
    RECORDINGS_DIR=/data/out/recordings \
    TRANSCRIPTS_DIR=/data/out/transcripts \
    FRAMES_DIR=/data/bot/frames \
    SUMMARIES_DIR=/data/out/summaries \
    PDF_DIR=/data/out/pdf \
    CLAUDE_CONFIG_DIR=/root/.claude \
    CLAUDE_CLI_BIN=/root/.local/bin/claude \
    DISABLE_AUTOUPDATER=1 \
    GDRIVE_LOGIN_BIND=0.0.0.0 \
    GDRIVE_LOGIN_PORT=8766 \
    PATH=/opt/meeting-bot-venv/bin:/root/.local/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# The installer, the lockfiles and the fonts only, so editing the code does
# not throw away the apt/Chrome/venv layer.
COPY docker/install.sh docker/install.sh
COPY requirements.txt requirements-browser.txt ./
COPY fonts fonts
ARG WITH_LIBREOFFICE=0
ARG WITH_FIREFOX=0
RUN apt-get update -qq && apt-get install -y --no-install-recommends python3 \
 && WITH_LIBREOFFICE=$WITH_LIBREOFFICE WITH_FIREFOX=$WITH_FIREFOX ./docker/install.sh

# The Claude Code CLI, for the summarizer's subscription billing. Its login
# lives in the claude-config volume (CLAUDE_CONFIG_DIR), not in the image, so
# rebuilding never signs you out; the auto-updater is off because the binary
# would update into a layer that the next `up` throws away — rebuild instead.
RUN wget -qO- https://claude.ai/install.sh | bash \
 && claude --version

COPY . .
RUN chmod +x docker/entrypoint.sh

# 8765: the web UI / trigger. 6080: noVNC, only while first_time_login.sh runs.
# 8766: the Google Drive sign-in's redirect, only while `gdrive.py login` runs.
EXPOSE 8765 6080 8766
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
