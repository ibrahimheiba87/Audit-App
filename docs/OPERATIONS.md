# Running the live server

How https://auditapp.heiba.cloud is set up, and how to update it, back it up, restore it and
turn on the AI assistant. It's written for whoever runs commands on the server, as root.

## What runs where

| Part | Where |
| --- | --- |
| Server | Hostinger VPS, AlmaLinux 9, IP 76.13.60.113 |
| DNS | Cloudflare: `auditapp.heiba.cloud` A record to the VPS, proxied. SSL/TLS mode must stay **Full** or **Full (strict)**: with Flexible, Cloudflare talks HTTP to the server, which redirects to HTTPS, forever |
| Web server | OpenLiteSpeed (CyberPanel), vhost `/usr/local/lsws/conf/vhosts/auditapp.heiba.cloud/vhost.conf` (earlier versions are kept beside it as `vhost.conf.bak-*`): HTTPS with a Let's Encrypt certificate, HTTP redirects to HTTPS, proxies to `127.0.0.1:8400` |
| Visitors' IP addresses | Set server-wide in `/usr/local/lsws/conf/httpd_config.conf`: `useIpInProxyHeader 2`, with Cloudflare's IP ranges trusted (a `T` suffix) in the server-level `accessControl` allow list. LiteSpeed then passes the visitor's real address, and the app logs it. The setting affects every site on the server (the earlier file is saved as `httpd_config.conf.bak-<timestamp>`), and the list needs updating if Cloudflare adds ranges (cloudflare.com/ips-v4 and /ips-v6) |
| App code | `/srv/apps/audit-app`, a git clone of `main` |
| Python | 3.12 in `/srv/apps/audit-app/.venv` (the system's 3.9 is left alone) |
| Process | PM2 process `audit-app`, started by `/srv/apps/audit-app-start.sh`, which loads the settings and runs `uvicorn app.main:app --host 127.0.0.1 --port 8400 --proxy-headers --forwarded-allow-ips 127.0.0.1` with a single worker. It's in PM2's saved list, so it starts again after a reboot |
| Settings | `/etc/audit-app/env` (root only, mode 600) |
| Database | The server's PostgreSQL 13: database `auditapp`, owned by role `auditapp`, password login over 127.0.0.1 |
| Backups | `/usr/local/bin/audit-app-backup.sh`, run by `/etc/cron.d/audit-app-backup` daily at 02:30: `pg_dump -Fc` into `/srv/backups/audit-app`, kept 14 days |

Uploaded files (ledgers, evidence, client documents) are stored in the database, so the
database backup is a complete backup of the app's data.

## Settings

`/etc/audit-app/env` holds one `NAME=value` per line:

| Setting | Value |
| --- | --- |
| `AUDIT_DB_URL` | `postgresql+psycopg://auditapp:PASSWORD@127.0.0.1:5432/auditapp` |
| `AUDIT_SECURE_COOKIES` | `1`. Needed behind the proxy so sign-in cookies are HTTPS-only |
| `AUDIT_SETUP_CODE` | Optional. While no account exists, the first-run form asks for this code |
| `ANTHROPIC_API_KEY` | Optional. Turns on the AI assistant (below) |
| `AUDIT_AI_MODEL` | Optional. The Claude model the assistant uses; leave it out for the default |

After changing the file, restart: `pm2 restart audit-app`.

Never paste the database password or the API key into a chat, a ticket or the repository.

## Check that it's up

```bash
curl -s http://127.0.0.1:8400/api/health      # {"status":"ok"}
pm2 status audit-app
pm2 logs audit-app --lines 100
```

## Update to the latest version

```bash
cd /srv/apps/audit-app
git pull --ff-only
.venv/bin/pip install -r requirements.txt
pm2 restart audit-app
curl -s http://127.0.0.1:8400/api/health
```

The app updates its own tables when it starts, so there's no separate migration step. Take a
backup first (next section) if the release notes mention database changes.

## Backups

Run one now:

```bash
/usr/local/bin/audit-app-backup.sh
ls -lh /srv/backups/audit-app
```

The backups are on the same server as the app, so they don't survive the server being lost.
Copy them somewhere else regularly, such as another machine or cloud storage, with a tool like
`rclone` or `scp`.

## Restore a backup

This replaces everything in the app with the backup's contents. The dumps are readable by
root only, so root feeds them to `pg_restore` on standard input.

```bash
pm2 stop audit-app
sudo -u postgres pg_restore --clean --if-exists --no-owner --role=auditapp \
  -d auditapp < /srv/backups/audit-app/FILE.dump
pm2 start audit-app
curl -s http://127.0.0.1:8400/api/health
```

To check a backup without touching the live data, restore it into a scratch database instead:

```bash
sudo -u postgres createdb -O auditapp auditapp_check
sudo -u postgres pg_restore --no-owner --role=auditapp -d auditapp_check < /srv/backups/audit-app/FILE.dump
sudo -u postgres psql -d auditapp_check -c "select count(*) from users; select count(*) from engagements;"
sudo -u postgres dropdb auditapp_check
```

## Turn on the AI assistant

The assistant explains findings and drafts write-ups. It's off until the server has an
Anthropic API key. When someone uses it, the finding (with its detail), the engagement's
framework, period end and materiality, and up to 50 ledger lines of the same entry are sent
to the Claude API. The client record's name isn't sent, but ledger descriptions, account
names, user names and finding notes go as they are and can name the client, customers or
staff. Turn it on only if the firm allows sending that data to Anthropic.

1. Create a key in the Anthropic Console (console.anthropic.com, under API keys) and set a
   monthly spend limit there.
2. On the server, add it to the settings file without echoing it into the shell history:

   ```bash
   read -rs -p "API key: " KEY && echo "ANTHROPIC_API_KEY=$KEY" >> /etc/audit-app/env && unset KEY
   pm2 restart audit-app
   ```

3. Open a finding in the app: the **Explain this entry** and **Draft a write-up** buttons now show. Each use
   is recorded in the activity log with the model and outcome.

To turn it off again, delete the `ANTHROPIC_API_KEY` line and restart.

## First admin

On a fresh database, the first person to open the site creates the admin account. Until that
account exists, either keep the site unreachable (for example limited to your own IP in the
vhost, as was done for the first setup) or set `AUDIT_SETUP_CODE`, so nobody else can get there
first. After that, the admin adds
everyone else under **Users**.
