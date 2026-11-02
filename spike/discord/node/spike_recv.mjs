// Spike: can discord.js + @discordjs/voice (the official library, which
// decrypts DAVE on receive since 0.19) record a Discord voice channel?
// Throwaway: see README.md, "Discord voice bot". Run through ./run.sh.
//
// Same commands, same output layout and the same spool format as the Python
// spike (lib/discord_spool.py), so the two can be compared file for file:
//   /spike_join   record YOUR voice channel
//   /spike_stop   stop, mix (with the repo's own lib/discord_spool.py), post
//                 the result into the voice channel's chat AND your DMs
// Output: $MEETING_BOT_ROOT/discord/spike/node_<stamp>/
//
// One structural difference, and the thing to listen for: this library hands
// over bare Opus payloads — no RTP timestamp, no jitter buffer, no loss
// concealment — so the spool places audio by arrival time alone.
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

import { Client, GatewayIntentBits, AttachmentBuilder, MessageFlags } from 'discord.js';
import {
  joinVoiceChannel, entersState, VoiceConnectionStatus, EndBehaviorType,
} from '@discordjs/voice';
import OpusScript from 'opusscript';

import { SpoolWriter } from './spool_writer.mjs';

const run = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../..');
const PY = process.env.MEETING_BOT_VENV
  ? path.join(process.env.MEETING_BOT_VENV, 'bin/python3')
  : path.join(REPO, '.venv/bin/python3');
const TOKEN = (process.env.DISCORD_BOT_TOKEN || '').trim();
const GUILD_ID = (process.env.DISCORD_SPIKE_GUILD_ID || '').trim();
if (!TOKEN || !/^\d+$/.test(GUILD_ID)) {
  console.error('spike: DISCORD_BOT_TOKEN and DISCORD_SPIKE_GUILD_ID must be set in .env');
  process.exit(1);
}
const ROOT = process.env.MEETING_BOT_ROOT
  || path.join(os.homedir(), '.local/share/meeting-bot');

// --- The bot ------------------------------------------------------------------
const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
const sessions = new Map();   // guild id -> session

function stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

async function join(interaction) {
  const member = interaction.member;
  const channel = member?.voice?.channel;
  if (!channel) {
    await interaction.reply({ content: 'Join a voice channel first.', flags: MessageFlags.Ephemeral });
    return;
  }
  if (sessions.has(interaction.guildId)) {
    await interaction.reply({ content: 'Already recording here — /spike_stop first.', flags: MessageFlags.Ephemeral });
    return;
  }
  await interaction.deferReply();
  const out = path.join(ROOT, 'discord', 'spike', `node_${stamp()}`);
  const writer = new SpoolWriter(path.join(out, 'spool'), {
    library: '@discordjs/voice', guild_id: interaction.guildId, guild_name: interaction.guild.name,
    channel_id: channel.id, channel_name: channel.name, requester_id: member.id });
  const conn = joinVoiceChannel({
    channelId: channel.id, guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator, selfDeaf: false, selfMute: true,
    debug: true });
  const debug = [];
  conn.on('debug', (m) => { if (/dave|decrypt|transition|epoch/i.test(m)) debug.push(`${new Date().toISOString()} ${m}`); });
  await entersState(conn, VoiceConnectionStatus.Ready, 30_000);

  const stats = { packets: {}, decode_errors: 0 };
  const decoders = new Map();
  conn.receiver.speaking.on('start', (userId) => {
    if (decoders.has(userId)) return;
    const dec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
    decoders.set(userId, dec);
    const m = channel.guild.members.cache.get(userId);
    writer.setName(userId, m?.displayName || userId);
    const stream = conn.receiver.subscribe(userId, { end: { behavior: EndBehaviorType.Manual } });
    stream.on('data', (opus) => {
      const arrival = writer.nowMs();
      stats.packets[userId] = (stats.packets[userId] || 0) + 1;
      try {
        writer.write(userId, dec.decode(opus), arrival);
      } catch {
        stats.decode_errors += 1;
      }
    });
    stream.on('error', (e) => debug.push(`stream ${userId} error: ${e}`));
  });
  sessions.set(interaction.guildId, { conn, writer, out, channel, requester: interaction.user, stats, debug, decoders });
  await interaction.editReply(`🔴 Recording \`${channel.name}\` (spike, discord.js). Talk for a minute or two, `
    + `taking turns and overlapping once, then /spike_stop.\nSpool: \`${out}\``);
}

async function stop(interaction) {
  const s = sessions.get(interaction.guildId);
  if (!s) {
    await interaction.reply({ content: 'Not recording.', flags: MessageFlags.Ephemeral });
    return;
  }
  sessions.delete(interaction.guildId);
  await interaction.deferReply();
  s.conn.destroy();
  s.writer.close();
  for (const d of s.decoders.values()) d.delete?.();

  const report = { library: 'node', ...s.stats, dave_debug: s.debug.slice(-200) };
  try {
    const mix = await run(PY, [path.join(REPO, 'lib/discord_spool.py'), 'mix',
      '--spool', path.join(s.out, 'spool'), '--out', path.join(s.out, 'mixed.m4a'),
      '--speakers-dir', path.join(s.out, 'speakers')]);
    report.mix = JSON.parse(mix.stdout);
    const info = await run(PY, [path.join(REPO, 'lib/discord_spool.py'), 'info', '--spool', path.join(s.out, 'spool')]);
    report.spool = JSON.parse(info.stdout);
  } catch (e) {
    report.mix_error = String(e.stderr || e);
  }
  fs.writeFileSync(path.join(s.out, 'report.json'), JSON.stringify(report, null, 1));
  const text = `Spike result (discord.js): ${Object.keys(s.stats.packets).length} speaker(s), `
    + `mix ${report.mix ? 'OK' : 'FAILED'}.\n\`${s.out}\``;
  await interaction.editReply(text);

  // The real bot's two destinations: the voice chat and a DM.
  const files = [new AttachmentBuilder(path.join(s.out, 'report.json'))];
  if (fs.existsSync(path.join(s.out, 'mixed.m4a'))) files.push(new AttachmentBuilder(path.join(s.out, 'mixed.m4a')));
  for (const [name, dest] of [['voice chat', s.channel], ['DM', s.requester]]) {
    try {
      await dest.send({ content: text, files });
      console.log(`delivered to ${name}`);
    } catch (e) {
      console.warn(`delivery to ${name} failed: ${e}`);
      if (name === 'DM') await s.channel.send(`Could not DM <@${s.requester.id}>: ${e.message}`);
    }
  }
}

client.once('clientReady', async () => {
  await client.application.commands.set([
    { name: 'spike_join', description: 'Record your voice channel (spike)' },
    { name: 'spike_stop', description: 'Stop, mix and post the result (spike)' },
  ], GUILD_ID);
  console.log(`ready as ${client.user.tag}; commands registered on guild ${GUILD_ID}`);
});

client.on('interactionCreate', async (interaction) => {
  if (!interaction.isChatInputCommand()) return;
  try {
    if (interaction.commandName === 'spike_join') await join(interaction);
    else if (interaction.commandName === 'spike_stop') await stop(interaction);
  } catch (e) {
    console.error(e);
    const msg = { content: `spike error: ${e.message}` };
    if (interaction.deferred || interaction.replied) await interaction.editReply(msg).catch(() => {});
    else await interaction.reply({ ...msg, flags: MessageFlags.Ephemeral }).catch(() => {});
  }
});

client.login(TOKEN);
