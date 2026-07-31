#!/bin/bash
# Creates a virtual PulseAudio sink named "meeting_sink".
# The browser plays meeting audio INTO this sink (set as default output),
# and ffmpeg records FROM its .monitor source.
# Safe to run every time before joining a meeting - it skips if already loaded.
set -e

# Start pulseaudio in the background if not already running (headless mode).
# Works whether the OS has plain PulseAudio (Debian minimal, Ubuntu Server
# minimal - neither installs an audio stack by default) or PipeWire's
# pulse-compatible socket already running (some Ubuntu images) - pactl talks
# to either transparently, so we only launch our own daemon if nothing
# is answering yet.
if ! pactl info > /dev/null 2>&1; then
  echo "==> Starting PulseAudio"
  pulseaudio -D --exit-idle-time=-1
  sleep 1
fi

# Create the virtual sink once
if ! pactl list short sinks | grep -q meeting_sink; then
  echo "==> Creating virtual sink 'meeting_sink'"
  pactl load-module module-null-sink sink_name=meeting_sink sink_properties=device.description=MeetingSink
else
  echo "==> 'meeting_sink' already exists"
fi

# Make it the default output so Chromium/Zoom plays audio here
pactl set-default-sink meeting_sink

echo "==> Audio ready. Recording source: meeting_sink.monitor"
