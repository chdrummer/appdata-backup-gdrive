# Appdata Backup to Google Drive (Unraid)

Backs up every folder in your Unraid `appdata` share to Google Drive on a schedule. One `.tar.zst` per app, streamed straight to Drive (no temp space), pruned to the last N runs.

## Build and publish
1. Create a GitHub repo, push this folder to `main`. The included workflow publishes `ghcr.io/<you>/appdata-backup-gdrive:latest`.
2. Make the package public (GitHub → Packages → settings).
3. Edit `YOUR_GITHUB_USER` in `unraid-template.xml`.

## Install on Unraid (about two minutes)
1. Open the Unraid web terminal and paste this one line (replace `YOUR_GITHUB_USER`):
   ```
   wget -O /boot/config/plugins/dockerMan/templates-user/my-appdata-backup-gdrive.xml https://raw.githubusercontent.com/YOUR_GITHUB_USER/appdata-backup-gdrive/main/unraid-template.xml
   ```
2. Docker tab → **Add Container** → choose **appdata-backup-gdrive** from the Template list.
3. Check the time zone, click **Apply**, then open the WebUI.

The defaults suit most servers. Optional settings (staging folder, password) are under *Show more settings*.

To get it into Community Apps for one-click installs, submit the repo through the Unraid forum's "Community Applications" thread.

## First run
Open the web UI, follow the three steps under *Connect Google Drive*, then click *Back up now*.

## Notes
- Mount `/source` read-only; the container never writes to appdata.
- "Stop containers" needs the Docker socket and matches containers to folders by name.
- A backup folder only counts toward "Backups to keep" if every app succeeded (it gets a `.complete` file). Partial folders are deleted once they are older than the oldest backup you're keeping.
- Backups land in `<Drive folder>/YYYY-MM-DD_HH-MM/<app>.tar.zst`.
- "Package locally first" uses the staging folder you choose in the UI (default `/staging`). It must sit inside a path mapped on the container, with free space for your largest app, and only its `appdata-backup-staging` subfolder is ever cleaned. Containers are stopped only while the archive is written there, and uploads are retried.
- Set `UI_PASSWORD` if the port is reachable beyond your LAN. Don't expose it to the internet.
