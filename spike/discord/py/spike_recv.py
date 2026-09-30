#!/usr/bin/env python3
"""
Spike: can discord.py (zacker150's fork) + discord-ext-voice-recv record a
DAVE-encrypted Discord voice channel? Throwaway: see README.md, "Discord
voice bot". Run it through ./run.sh, not directly.

Slash commands (registered on DISCORD_SPIKE_GUILD_ID only, so they appear at
once instead of after Discord's global-command delay):

  /spike_join   the bot joins YOUR voice channel and records every speaker
                into a spool (lib/discord_spool.py's format)
  /spike_stop   stops, mixes the spool into mixed.m4a (+ per-speaker tracks),
                writes report.json, and posts the result into the voice
                channel's chat AND your DMs — the two places the real bot
                will deliver the PDF

Everything lands in $MEETING_BOT_ROOT/discord/spike/py_<stamp>/. The spool is
kept (no --remove-spool) so a bad mix can be looked at.

What to check afterwards (the same list for the Node spike):
  * report.json: every speaker has speech_s close to how long they talked,
    and dave.decryption_stats shows successes, not failures
  * mixed.m4a: every voice is clear (not noise, not robotic), and nobody's
    words are shifted against the others'
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, voice_recv

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "lib"))
import discord_spool  # noqa: E402

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = os.environ.get("DISCORD_SPIKE_GUILD_ID", "").strip()
if not TOKEN or not GUILD_ID.isdigit():
    sys.exit("spike: DISCORD_BOT_TOKEN and DISCORD_SPIKE_GUILD_ID must be set in .env")
GUILD = discord.Object(id=int(GUILD_ID))
ROOT = Path(os.environ.get("MEETING_BOT_ROOT")
            or Path.home() / ".local/share/meeting-bot").expanduser()

discord.opus._load_default()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spike")

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


class SpoolSink(voice_recv.AudioSink):
    """Every decoded packet, with its RTP timestamp, into the spool."""

    def __init__(self, writer):
        super().__init__()
        self.writer = writer
        self.packets = {}
        self.empty = 0

    def wants_opus(self):
        return False

    def write(self, user, data):
        if user is not None:
            key = str(user.id)
            if key not in self.packets:
                self.writer.set_name(key, getattr(user, "display_name", str(user)))
        else:
            key = f"ssrc_{data.packet.ssrc}"
        self.packets[key] = self.packets.get(key, 0) + 1
        if not data.pcm:
            self.empty += 1
            return
        self.writer.write(key, data.pcm, rtp_ts=data.packet.timestamp)

    def cleanup(self):
        pass


SESSIONS = {}   # guild id -> dict


class Spike(commands.Cog):
    @app_commands.command(name="spike_join", description="Record your voice channel (spike)")
    @app_commands.guilds(GUILD)
    async def spike_join(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member) \
                or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True)
            return
        if guild.id in SESSIONS:
            await interaction.response.send_message(
                "Already recording here — /spike_stop first.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        channel = member.voice.channel
        out = ROOT / "discord" / "spike" / f"py_{time.strftime('%Y%m%d_%H%M%S')}"
        writer = discord_spool.SpoolWriter(out / "spool", meta={
            "library": "discord.py fork + discord-ext-voice-recv",
            "guild_id": str(guild.id), "guild_name": guild.name,
            "channel_id": str(channel.id), "channel_name": channel.name,
            "requester_id": str(member.id)})
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        sink = SpoolSink(writer)
        vc.listen(sink)
        SESSIONS[guild.id] = dict(vc=vc, sink=sink, writer=writer, out=out,
                                  channel=channel, requester=member)
        await interaction.followup.send(
            f"🔴 Recording `{channel.name}` (spike, discord.py). "
            f"Talk for a minute or two, taking turns and overlapping once, "
            f"then /spike_stop.\nSpool: `{out}`")

    @app_commands.command(name="spike_stop", description="Stop, mix and post the result (spike)")
    @app_commands.guilds(GUILD)
    async def spike_stop(self, interaction: discord.Interaction):
        s = SESSIONS.pop(interaction.guild_id, None)
        if s is None:
            await interaction.response.send_message("Not recording.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        vc = s["vc"]
        diag = {}
        try:
            diag = vc.get_recv_diagnostics()
        except Exception as exc:          # a spike: report, don't die
            diag = {"error": repr(exc)}
        vc.stop_listening()
        await vc.disconnect()
        s["writer"].close()

        out = s["out"]
        report = {"library": "py", "packets": s["sink"].packets,
                  "empty_pcm_packets": s["sink"].empty,
                  "dave": {k: diag.get(k) for k in (
                      "dave_session", "decryption_stats", "dave_inner_decrypt_ok",
                      "dave_inner_decrypt_err", "dave_plaintext_rejected",
                      "opus_decode_ok", "opus_decode_err", "opus_plc_ok",
                      "opus_fec_ok", "jitter_resync")}}
        try:
            report["spool"] = discord_spool.info(out / "spool")
            dur = await asyncio.to_thread(discord_spool.mix, out / "spool",
                                          out / "mixed.m4a")
            await asyncio.to_thread(discord_spool.export_speakers, out / "spool",
                                    out / "speakers")
            report["mixed_duration_s"] = round(dur, 2)
        except discord_spool.SpoolError as exc:
            report["mix_error"] = str(exc)
        (out / "report.json").write_text(
            json.dumps(report, indent=1, ensure_ascii=False, default=str))

        text = (f"Spike result (discord.py): {len(report['packets'])} speaker(s), "
                f"mix {'OK' if 'mixed_duration_s' in report else 'FAILED'}.\n"
                f"`{out}`")
        await interaction.followup.send(text)
        await self._deliver(s["channel"], s["requester"], out, text)

    async def _deliver(self, channel, requester, out, text):
        """The real bot's two destinations: the voice chat and a DM."""
        for name, dest in (("voice chat", channel), ("DM", requester)):
            try:
                files = [discord.File(out / "report.json")]
                if (out / "mixed.m4a").exists():
                    files.append(discord.File(out / "mixed.m4a"))
                await dest.send(text, files=files)
                log.info("delivered to %s", name)
            except discord.HTTPException as exc:
                log.warning("delivery to %s failed: %s", name, exc)
                if name == "DM":
                    await channel.send(f"Could not DM {requester.mention}: {exc}")


@bot.event
async def setup_hook():
    await bot.add_cog(Spike())
    synced = await bot.tree.sync(guild=GUILD)
    log.info("synced %d command(s) to guild %s", len(synced), GUILD_ID)


@bot.event
async def on_ready():
    log.info("ready as %s (%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    bot.run(TOKEN)
