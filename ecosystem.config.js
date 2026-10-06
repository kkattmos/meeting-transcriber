// pm2 process file for the meeting bot's long-running pieces.
//
//   ./webui.sh on       start both (pm2 start ecosystem.config.js)
//   ./webui.sh off      stop and remove both from pm2
//
// Deliberately NOT registered with `pm2 startup` / `pm2 save`: the operator
// chose "off at boot", so nothing here runs until it is switched on. Don't add
// either to setup.sh.
const path = require("path");
const root = __dirname;

module.exports = {
  apps: [
    {
      // The web UI + /trigger endpoint (trigger_server.py via web/serve.sh,
      // which loads .env). Restarted if it crashes.
      name: "meeting-bot-web",
      script: path.join(root, "web", "serve.sh"),
      interpreter: "bash",
      cwd: root,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
    },
    {
      // The backstop for a summarize stage paused on the Claude usage window:
      // `pipeline.sh --resume-all` every 15 minutes (it skips a run whose
      // window has not reset yet, and one whose lock owner is alive). One
      // shot per tick, so no autorestart — cron_restart is the schedule.
      // web/resume.sh detaches the resume and exits: cron_restart kills a
      // still-running app, which killed every resume longer than 15 minutes.
      name: "meeting-bot-resume",
      script: path.join(root, "web", "resume.sh"),
      interpreter: "bash",
      cwd: root,
      autorestart: false,
      cron_restart: "*/15 * * * *",
    },
  ],
};
