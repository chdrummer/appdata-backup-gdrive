import collections, datetime, hmac, json, os, re, shutil, subprocess, tempfile, threading
from flask import Flask, Response, jsonify, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SOURCE = os.environ.get("SOURCE_DIR", "/source")
SELF_NAME = os.environ.get("SELF_NAME", "appdata-backup-gdrive")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
SETTINGS_FILE = f"{CONFIG_DIR}/settings.json"
RCLONE_CONF = f"{CONFIG_DIR}/rclone.conf"
STAGING = os.environ.get("STAGING_DIR", "/staging")
REMOTE = "gdrive"
COMPLETE_MARKER = ".complete"
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")

DEFAULTS = {
    "enabled": True,
    "cron": "0 3 * * *",
    "folder": "unraid-backups",
    "keep": 7,
    "limit_mbps": 0,
    "stage_locally": False,
    "staging_dir": STAGING,
    "stop_containers": True,
    "skip_apps": SELF_NAME,
    "excludes": "*/cache/*\n*/Cache/*\n*/logs/*\n*.log",
}

app = Flask(__name__)
sched = BackgroundScheduler(timezone=os.environ.get("TZ", "UTC"))
lock = threading.Lock()
logbuf = collections.deque(maxlen=400)
state = {"running": False, "last": None}


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S}  {msg}"
    print(line, flush=True)
    logbuf.append(line)


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return dict(DEFAULTS)


def save_settings(s):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)


def rclone(*args, timeout=None, input=None):
    return subprocess.run(["rclone", "--config", RCLONE_CONF, *args],
                          capture_output=True, text=True, timeout=timeout, input=input)


def staging_status(path):
    """Check a staging folder is on a mapped volume (not Docker's own disk image) and writable."""
    path = os.path.normpath(path or "")
    if not path.startswith("/") or path == "/":
        return {"ok": False, "error": "Enter a folder path like /staging."}
    p = path
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    if p == "/":
        return {"ok": False, "error": "That folder isn't on a mapped volume. Add a path in the container "
                "settings (for example /staging) and choose it or a folder inside it."}
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        return {"ok": True, "free": shutil.disk_usage(path).free}
    except OSError as e:
        return {"ok": False, "error": f"Can't write to that folder: {e.strerror or e}"}


def drive_status():
    if not os.path.exists(RCLONE_CONF):
        return {"connected": False}
    try:
        r = rclone("about", f"{REMOTE}:", "--json", timeout=30)
        if r.returncode != 0:
            return {"connected": False, "error": r.stderr.strip()[-300:]}
        d = json.loads(r.stdout)
        return {"connected": True, "used": d.get("used"), "total": d.get("total")}
    except Exception as e:
        return {"connected": False, "error": str(e)}


# ---------- backup ----------

TAR_SCRIPT = r'''
args=(); while IFS= read -r p; do [ -n "$p" ] && args+=(--exclude="$p"); done <<< "$EXCLUDES"
tar -C "$SOURCE" "${args[@]}" -cf - "$APP" | zstd -T0 -3 | rclone --config "$RCLONE_CONF" ${BWLIMIT:+--bwlimit "$BWLIMIT"} rcat "$DEST"
st=("${PIPESTATUS[@]}")
# tar exit 1 = "file changed while reading": fine for live appdata
[ "${st[0]}" -le 1 ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ]
'''


# Pack to a local file first so the container is only stopped for the fast local step
STAGE_SCRIPT = r'''
args=(); while IFS= read -r p; do [ -n "$p" ] && args+=(--exclude="$p"); done <<< "$EXCLUDES"
tar -C "$SOURCE" "${args[@]}" -cf - "$APP" | zstd -T0 -3 -q -f -o "$STAGED"
st=("${PIPESTATUS[@]}")
[ "${st[0]}" -le 1 ] && [ "${st[1]}" -eq 0 ]
'''


def running_containers():
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    return {n.lower(): n for n in r.stdout.split()} if r.returncode == 0 else {}


def docker(action, name):
    args = ["docker", action] + (["-t", "60"] if action == "stop" else []) + [name]
    return subprocess.run(args, capture_output=True, text=True).returncode == 0


def run_backup():
    if not lock.acquire(blocking=False):
        return False
    state["running"] = True
    logbuf.clear()
    started = datetime.datetime.now()
    ok = fail = 0
    try:
        s = load_settings()
        if not os.path.exists(RCLONE_CONF):
            raise RuntimeError("Google Drive is not connected yet.")
        base = f"{REMOTE}:{s['folder'].strip('/')}"
        stamp = started.strftime("%Y-%m-%d_%H-%M")
        skip = {x.strip().lower() for x in re.split(r"[,\n]", s["skip_apps"]) if x.strip()}
        apps = sorted(d for d in os.listdir(SOURCE)
                      if os.path.isdir(os.path.join(SOURCE, d)) and d.lower() not in skip)
        containers = running_containers() if s["stop_containers"] else {}
        mbps = float(s.get("limit_mbps") or 0)
        bwlimit = str(max(1, int(mbps * 122))) if mbps > 0 else ""  # Mbps -> KiB/s for rclone
        log(f"Backing up {len(apps)} apps to {base}/{stamp}"
            + (f" (upload limited to {mbps:g} Mbps)" if bwlimit else ""))
        stage = bool(s.get("stage_locally"))
        if stage:
            chk = staging_status(s["staging_dir"])
            if not chk["ok"]:
                raise RuntimeError("Staging folder problem: " + chk["error"])
            # our own subfolder, so cleanup never touches anything else in the chosen folder
            work = os.path.join(os.path.normpath(s["staging_dir"]), "appdata-backup-staging")
            os.makedirs(work, exist_ok=True)
            for f in os.listdir(work):  # leftovers from an interrupted run
                if f.endswith(".tar.zst"):
                    os.remove(os.path.join(work, f))
        bw = ["--bwlimit", bwlimit] if bwlimit else []
        for a in apps:
            name = containers.get(a.lower())
            if name and name.lower() != SELF_NAME.lower():
                log(f"[{a}] stopping container")
                if not docker("stop", name):
                    log(f"[{a}] could not stop container, backing up while running")
                    name = None
            else:
                name = None
            dest = f"{base}/{stamp}/{a}.tar.zst"
            staged = os.path.join(work, f"{a}.tar.zst") if stage else ""
            err = None
            try:
                env = {**os.environ, "APP": a, "SOURCE": SOURCE, "EXCLUDES": s["excludes"],
                       "RCLONE_CONF": RCLONE_CONF, "BWLIMIT": bwlimit, "DEST": dest, "STAGED": staged}
                if stage:
                    log(f"[{a}] packing locally")
                r = subprocess.run(["bash", "-c", STAGE_SCRIPT if stage else TAR_SCRIPT],
                                   env=env, capture_output=True, text=True)
                if r.returncode != 0:
                    err = r.stderr.strip() or "unknown error"
            finally:
                if name:
                    docker("start", name)
                    log(f"[{a}] container started")
            if stage and err is None:
                try:
                    mb = os.path.getsize(staged) / 1e6
                    log(f"[{a}] uploading {mb:,.0f} MB")
                    u = rclone("copyto", staged, dest, *bw)
                    if u.returncode != 0:
                        err = u.stderr.strip() or "upload failed"
                finally:
                    if os.path.exists(staged):
                        os.remove(staged)
            elif stage and os.path.exists(staged):
                os.remove(staged)
            if err is None:
                ok += 1
                log(f"[{a}] done")
            else:
                fail += 1
                log(f"[{a}] FAILED: {err[-300:]}")
        if fail == 0:
            m = rclone("rcat", f"{base}/{stamp}/{COMPLETE_MARKER}", input=f"complete {stamp}\n")
            if m.returncode == 0:
                prune(base, int(s["keep"]))
            else:
                log("Could not mark this backup as complete, so old backups were left alone.")
        else:
            log("Some apps failed. This backup won't count toward your kept backups, and old ones were left alone.")
        result = "ok" if fail == 0 else "errors"
    except Exception as e:
        log(f"ERROR: {e}")
        result = "failed"
    finally:
        log(f"Finished: {ok} backed up, {fail} failed.")
        state["last"] = {"when": started.isoformat(timespec="seconds"), "result": result,
                         "ok": ok, "failed": fail}
        state["running"] = False
        lock.release()
    return True


def prune(base, keep):
    """Keep the newest `keep` fully successful backups. Folders without the
    completion marker never count; they are deleted only once they are older
    than the oldest backup being kept."""
    r = rclone("lsf", "--dirs-only", base)
    if r.returncode != 0:
        return
    dirs = sorted(d.strip("/") for d in r.stdout.split() if STAMP_RE.match(d.strip("/")))
    complete = []
    for d in dirs:
        c = rclone("lsf", f"{base}/{d}", "--files-only", "--max-depth", "1", "--include", COMPLETE_MARKER)
        if c.returncode == 0 and COMPLETE_MARKER in c.stdout.split():
            complete.append(d)
    if len(complete) < 1:
        return
    kept = complete[-keep:]
    oldest_kept = kept[0]
    log(f"Keeping the newest {len(kept)} complete backup(s)")
    for d in dirs:
        if d in kept or d > oldest_kept:
            continue
        kind = "old backup" if d in complete else "incomplete backup"
        log(f"Deleting {kind} {d}")
        rclone("purge", f"{base}/{d}", "--drive-use-trash=false")


def reschedule():
    s = load_settings()
    if sched.get_job("backup"):
        sched.remove_job("backup")
    if s["enabled"]:
        sched.add_job(run_backup, CronTrigger.from_crontab(s["cron"], timezone=sched.timezone),
                      id="backup", max_instances=1)


# ---------- web ----------

@app.before_request
def auth():
    if not UI_PASSWORD:
        return
    a = request.authorization
    if not (a and hmac.compare_digest(a.password or "", UI_PASSWORD)):
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Backup"'})


@app.get("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/status")
def status():
    job = sched.get_job("backup")
    return jsonify(running=state["running"], last=state["last"], log=list(logbuf),
                   next_run=job.next_run_time.isoformat(timespec="minutes") if job else None,
                   settings=load_settings(),
                   apps=sorted(d for d in os.listdir(SOURCE) if os.path.isdir(os.path.join(SOURCE, d)))
                   if os.path.isdir(SOURCE) else [])


@app.get("/api/drive")
def drive():
    return jsonify(drive_status())


@app.post("/api/token")
def token():
    d = request.get_json(force=True)
    try:
        tok = json.loads(d["token"].strip())
        assert "access_token" in tok or "refresh_token" in tok
    except Exception:
        return jsonify(error="That doesn't look like the token. Paste the full {...} block that rclone printed."), 400
    conf = f"[{REMOTE}]\ntype = drive\nscope = drive\ntoken = {json.dumps(tok)}\n"
    if d.get("client_id") and d.get("client_secret"):
        conf += f"client_id = {d['client_id'].strip()}\nclient_secret = {d['client_secret'].strip()}\n"
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(RCLONE_CONF, "w") as f:
        f.write(conf)
    os.chmod(RCLONE_CONF, 0o600)
    st = drive_status()
    if not st["connected"]:
        os.remove(RCLONE_CONF)
        return jsonify(error="Google rejected that token. " + st.get("error", "")), 400
    return jsonify(st)


@app.post("/api/staging-check")
def staging_check():
    return jsonify(staging_status(request.get_json(force=True).get("path", "")))


@app.post("/api/settings")
def settings():
    d = request.get_json(force=True)
    s = {**load_settings(), **{k: d[k] for k in DEFAULTS if k in d}}
    try:
        CronTrigger.from_crontab(s["cron"])
        s["keep"] = max(1, int(s["keep"]))
    except Exception:
        return jsonify(error="The schedule must be a cron expression like 0 3 * * *"), 400
    try:
        s["limit_mbps"] = max(0.0, float(s["limit_mbps"] or 0))
    except (TypeError, ValueError):
        return jsonify(error="The upload limit must be a number in Mbps, or 0 for no limit."), 400
    s["staging_dir"] = (s["staging_dir"] or STAGING).strip()
    if s["stage_locally"]:
        chk = staging_status(s["staging_dir"])
        if not chk["ok"]:
            return jsonify(error="Staging folder problem: " + chk["error"]), 400
    save_settings(s)
    reschedule()
    return jsonify(ok=True)


@app.post("/api/run")
def run():
    if state["running"]:
        return jsonify(error="A backup is already running."), 409
    threading.Thread(target=run_backup, daemon=True).start()
    return jsonify(ok=True)


if __name__ == "__main__":
    sched.start()
    reschedule()
    app.run(host="0.0.0.0", port=8080, threaded=True)
