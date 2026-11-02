#!/bin/bash
# The image's installer (Dockerfile RUN), as root, at build time. The
# desktop's setup.sh is a two-step user install and the VM's a host install;
# this is the same package set for a container: apt, real Google Chrome, the
# PulseAudio daemon (a container has no PipeWire — lib/xsession.sh starts it
# on demand), uv + the hash-pinned venv at $MEETING_BOT_VENV, yt-dlp and deno,
# and the vendored Thai fonts.
#
#   WITH_LIBREOFFICE=1 / WITH_FIREFOX=1   (build args) add those as well
set -euo pipefail
cd "$(dirname "$0")/.."
export DEBIAN_FRONTEND=noninteractive
VENV="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}"

apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg git unzip tini \
  ffmpeg python3 python3-venv \
  procps psmisc util-linux tzdata locales \
  xvfb x11vnc novnc websockify \
  pulseaudio pulseaudio-utils \
  poppler-utils \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  fonts-thai-tlwg fonts-liberation fonts-noto-core fonts-cmu fonts-jetbrains-mono \
  fontconfig
sed -i 's/^# *\(en_US.UTF-8\|th_TH.UTF-8\)/\1/' /etc/locale.gen
locale-gen >/dev/null

[ "${WITH_LIBREOFFICE:-0}" = "1" ] && apt-get install -y --no-install-recommends libreoffice-impress
[ "${WITH_FIREFOX:-0}" = "1" ] && apt-get install -y --no-install-recommends firefox-esr

# Real Chrome, NOT chromium: Google's sign-in blocks unbranded builds.
wget -q -O /tmp/google.pub https://dl.google.com/linux/linux_signing_key.pub
gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg /tmp/google.pub
rm -f /tmp/google.pub
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list
apt-get update -qq
apt-get install -y --no-install-recommends google-chrome-stable
google-chrome-stable --version

uv venv --seed --python 3.13 "$VENV"
uv pip install --quiet --python "$VENV/bin/python3" \
  -r requirements.txt -r requirements-browser.txt
"$VENV/bin/playwright" install-deps chromium >/dev/null 2>&1 || true

if [ "${WITH_FIREFOX:-0}" = "1" ]; then
  tag="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
         https://github.com/mozilla/geckodriver/releases/latest | sed 's#.*/tag/##')"
  curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/$tag/geckodriver-$tag-linux64.tar.gz" \
    | tar -xz -C /usr/local/bin geckodriver
fi

# yt-dlp from its releases (a stale apt one is the #1 cause of silent
# failures; rebuild the image to refresh it), and deno, its JS runtime.
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
chmod a+rx /usr/local/bin/yt-dlp
curl -fsSL -o /tmp/deno.zip \
  https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
python3 -c 'import zipfile; zipfile.ZipFile("/tmp/deno.zip").extract("deno", "/usr/local/bin")'
chmod a+rx /usr/local/bin/deno
rm -f /tmp/deno.zip
yt-dlp --version && deno --version | head -n 1

install -d /usr/local/share/fonts/meeting-bot
install -m 0644 fonts/*/*.ttf /usr/local/share/fonts/meeting-bot/
fc-cache -f >/dev/null

rm -rf /var/lib/apt/lists/* /root/.cache
