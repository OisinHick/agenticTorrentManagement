import os
import json
import logging
import asyncio
from datetime import datetime, timezone
import httpx
import qbittorrentapi
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from croniter import croniter

# Setup Memory Logging Handler
class LogMemoryHandler(logging.Handler):
    def __init__(self, capacity=500):
        super().__init__()
        self.capacity = capacity
        self.buffer = []

    def emit(self, record):
        log_entry = self.format(record)
        self.buffer.insert(0, {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "message": record.getMessage()
        })
        if len(self.buffer) > self.capacity:
            self.buffer.pop()

# Setup logging
log_handler = LogMemoryHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger = logging.getLogger("agent-manager")
logger.addHandler(log_handler)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# Make other third-party libraries less verbose
logging.getLogger("qbittorrentapi").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Configuration File Path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(CURRENT_DIR, "agent-data"))
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

# Default settings
default_config = {
    "qbittorrentHost": "localhost",
    "qbittorrentPort": 8080,
    "qbittorrentUsername": "admin",
    "qbittorrentPassword": "adminadmin",
    
    "stuckLimitMinutes": 15.0,
    "checkIntervalSeconds": 900,
    "cronExpression": "",
    
    "downloadsDirPath": "/downloads",
    "enableOrphanedCleaner": False,
    "orphanedCleanerDryRun": True,
    
    "autoReannounce": True,
    "injectPublicTrackers": True,
    
    "webhookUrl": "",
    "webhookType": "discord", # 'discord', 'gotify', 'generic'
    
    "excludeTags": [],
    "excludeCategories": [],
    
    "stats": {
        "runsCount": 0,
        "pausedCount": 0,
        "reannouncedCount": 0,
        "injectedCount": 0,
        "cleanedOrphanedCount": 0,
        "cleanedOrphanedBytes": 0
    }
}

config = { **default_config }

def load_config():
    global config
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                loaded = json.load(f)
                config = { **default_config, **loaded }
                config["stats"] = { **default_config["stats"], **loaded.get("stats", {}) }
                logger.info("Configuration loaded successfully.")
        else:
            save_config()
            logger.info("Default configuration file initialized.")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")

def save_config():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save configuration: {e}")

# Webhook Alert notifications
async def send_notification(title, message):
    if not config.get("webhookUrl"):
        return
    
    url = config["webhookUrl"]
    w_type = config["webhookType"]
    
    try:
        async with httpx.AsyncClient() as client:
            payload = {}
            if w_type == "discord":
                payload = {
                    "username": "AgenticTorrent",
                    "embeds": [{
                        "title": title,
                        "description": message,
                        "color": 3447003, # blue accent
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }]
                }
            elif w_type == "gotify":
                payload = {
                    "title": title,
                    "message": message,
                    "priority": 5
                }
            else: # generic
                payload = {
                    "title": title,
                    "message": message,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            
            await client.post(url, json=payload, timeout=5.0)
    except Exception as e:
        logger.warning(f"Notification dispatch failed: {e}")

# Connect to qBittorrent
def get_qbt_client():
    try:
        client = qbittorrentapi.Client(
            host=config["qbittorrentHost"],
            port=config["qbittorrentPort"],
            username=config["qbittorrentUsername"],
            password=config["qbittorrentPassword"]
        )
        client.auth_log_in()
        return client
    except Exception as e:
        logger.error(f"Failed to connect to qBittorrent: {e}")
        return None

# Fetch public trackers
async def fetch_public_trackers():
    url = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5.0)
            if res.status_code == 200:
                trackers = [line.strip() for line in res.text.split("\n") if line.strip()]
                return trackers
    except Exception as e:
        logger.warning(f"Could not download online public trackers list: {e}")
    
    # Fallback basic trackers
    return [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.coppersurfer.tk:6969/announce"
    ]

# Orphaned Files Cleaner
def run_orphaned_cleanup():
    downloads_dir = config.get("downloadsDirPath", "/downloads")
    if not config.get("enableOrphanedCleaner") or not os.path.exists(downloads_dir):
        return {"cleaned_files": [], "total_bytes": 0}

    client = get_qbt_client()
    if not client:
        return {"error": "qBittorrent offline"}

    try:
        torrents = client.torrents_info(status_filter="all")
        active_paths = set()
        
        # Build set of all active file paths
        for t in torrents:
            content_path = os.path.abspath(t.content_path)
            active_paths.add(content_path)
            
            # If it's a directory, add child files
            if os.path.isdir(content_path):
                for root, _, files in os.walk(content_path):
                    for file in files:
                        active_paths.add(os.path.abspath(os.path.join(root, file)))

        cleaned_files = []
        total_bytes = 0
        dry_run = config.get("orphanedCleanerDryRun", True)

        for root, _, files in os.walk(downloads_dir):
            for file in files:
                file_path = os.path.abspath(os.path.join(root, file))
                
                # Check if file path is not registered in active torrent paths
                if file_path not in active_paths:
                    # Exclude hidden files
                    if not file.startswith('.'):
                        try:
                            file_size = os.path.getsize(file_path)
                            total_bytes += file_size
                            cleaned_files.append(file_path)
                            
                            if not dry_run:
                                os.remove(file_path)
                        except Exception as delete_err:
                            logger.error(f"Error accessing orphaned file {file_path}: {delete_err}")

        # Update stats
        if not dry_run and len(cleaned_files) > 0:
            config["stats"]["cleanedOrphanedCount"] += len(cleaned_files)
            config["stats"]["cleanedOrphanedBytes"] += total_bytes
            save_config()
            logger.info(f"Orphaned Cleaner: Deleted {len(cleaned_files)} files ({total_bytes} bytes).")
        elif dry_run and len(cleaned_files) > 0:
            logger.info(f"Orphaned Cleaner [DRY RUN]: Found {len(cleaned_files)} orphaned files ({total_bytes} bytes).")

        return {
            "cleaned_files": cleaned_files,
            "total_bytes": total_bytes,
            "dry_run": dry_run
        }
    except Exception as e:
        logger.error(f"Error running orphaned cleaner: {e}")
        return {"error": str(e)}

# Staged actions tracker
state = {}
def load_state():
    global state
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r") as f:
                state = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

def save_state():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

# Automated check turn execution
async def run_agent_turn():
    logger.info("Executing torrent check turn...")
    config["stats"]["runsCount"] += 1
    save_config()
    
    client = get_qbt_client()
    if not client:
        return
        
    try:
        # Load exclusion setups
        exclude_tags = set(config.get("excludeTags", []))
        exclude_categories = set(config.get("excludeCategories", []))

        torrents = client.torrents_info(status_filter="all")
        current_hashes = set()
        now = datetime.now(timezone.utc)
        
        load_state()

        stuck_hashes = []
        for t in torrents:
            h = t.hash
            name = t.name
            qbt_state = t.state
            category = t.category
            tags = [x.strip() for x in t.tags.split(",") if x.strip()] if t.tags else []
            current_hashes.add(h)

            # Check exclusions
            if category in exclude_categories or any(tag in exclude_tags for tag in tags):
                # Clean tracking state if excluded midway
                if h in state:
                    del state[h]
                continue

            # Flag if stalled downloading or stuck fetching metadata
            if qbt_state in ("stalledDL", "metaDL"):
                if h not in state:
                    state[h] = {
                        "hash": h,
                        "name": name,
                        "state": qbt_state,
                        "first_seen": now.isoformat(),
                        "staged_stage": "none", # 'none', 'reannounced', 'injected'
                        "stuck": False,
                        "duration_minutes": 0.0
                    }
                    logger.info(f"Torrent '{name}' flagged as stalled/metaDL.")
                else:
                    first_seen = datetime.fromisoformat(state[h]["first_seen"])
                    duration = (now - first_seen).total_seconds() / 60.0
                    state[h]["duration_minutes"] = duration
                    state[h]["state"] = qbt_state

                    # Determine stuck threshold
                    limit = config["stuckLimitMinutes"]
                    
                    # Core Staged Recovery System:
                    # 1. Halfway mark: run re-announce
                    if duration >= (limit / 2.0) and state[h]["staged_stage"] == "none":
                        if config.get("autoReannounce"):
                            logger.info(f"Triggering auto-reannounce for stuck torrent '{name}'...")
                            client.torrents_reannounce(torrent_hashes=h)
                            state[h]["staged_stage"] = "reannounced"
                            config["stats"]["reannouncedCount"] += 1
                            save_config()

                    # 2. 75% mark: inject public trackers
                    if duration >= (limit * 0.75) and state[h]["staged_stage"] == "reannounced":
                        if config.get("injectPublicTrackers"):
                            logger.info(f"Injecting public trackers list for stuck torrent '{name}'...")
                            trackers = await fetch_public_trackers()
                            client.torrents_add_trackers(torrent_hash=h, urls=trackers)
                            state[h]["staged_stage"] = "injected"
                            config["stats"]["injectedCount"] += 1
                            save_config()

                    # 3. Exceeded Stuck limit: mark stuck and add tag
                    if duration >= limit:
                        if not state[h]["stuck"]:
                            state[h]["stuck"] = True
                            logger.warning(f"Torrent '{name}' reached stuck threshold ({duration:.1f} mins).")
                            # Add stuck tag in qBittorrent
                            client.torrents_add_tags(tags="stuck", torrent_hashes=h)
                            await send_notification(
                                "Torrent Stuck",
                                f"Torrent '{name}' has been stalled for {duration:.1f} minutes and is marked as STUCK."
                            )
                        stuck_hashes.append(h)
            else:
                # Active or complete
                if h in state:
                    logger.info(f"Torrent '{name}' recovered (state: {qbt_state}). Removing stuck tracking.")
                    client.torrents_remove_tags(tags="stuck", torrent_hashes=h)
                    del state[h]

        # Clean old tracking entries
        for h in list(state.keys()):
            if h not in current_hashes:
                del state[h]
                
        save_state()

        # Handle stuck torrent actions (pausing)
        if stuck_hashes:
            hashes_str = "|".join(stuck_hashes)
            logger.info(f"Pausing stuck torrents: {hashes_str}")
            client.torrents_pause(torrent_hashes=hashes_str)
            config["stats"]["pausedCount"] += len(stuck_hashes)
            save_config()
            
            # Send webhook notification
            await send_notification(
                "Stuck Torrents Paused",
                f"Automatically paused {len(stuck_hashes)} stuck torrents."
            )

        # Run Orphaned files cleaner automatically if enabled
        if config.get("enableOrphanedCleaner"):
            run_orphaned_cleanup()
            
    except Exception as e:
        logger.error(f"Error executing agent turn: {e}", exc_info=True)

# Asynchronous Background Daemon Scheduler
async def background_scheduler():
    logger.info("Initializing background scheduler...")
    while True:
        # Check current delay configurations
        cron_expr = config.get("cronExpression", "")
        delay = config.get("checkIntervalSeconds", 900)

        # Sleep interval calculation
        sleep_seconds = delay
        if cron_expr and cron_expr.strip():
            try:
                if croniter.is_valid(cron_expr):
                    now = datetime.now()
                    iter_cron = croniter(cron_expr, now)
                    next_run = iter_cron.get_next(datetime)
                    sleep_seconds = int((next_run - now).total_seconds())
                    # Ensure positive sleep timer
                    if sleep_seconds <= 0:
                        sleep_seconds = 1
                else:
                    logger.warning(f"Cron expression '{cron_expr}' is invalid. Using interval default.")
            except Exception as e:
                logger.error(f"Error calculating next cron ticks: {e}")

        logger.info(f"Next queue check scheduled in {sleep_seconds} seconds.")
        await asyncio.sleep(sleep_seconds)
        
        try:
            await run_agent_turn()
        except Exception as e:
            logger.error(f"Scheduler check cycle failed: {e}")

# FastAPI endpoints
api = FastAPI()
load_config()

# Startup background task creation
@api.on_event("startup")
async def startup_event():
    asyncio.create_task(background_scheduler())

# Serve static web frontend
@api.get("/", response_class=HTMLResponse)
def read_root():
    static_html_path = os.path.join(CURRENT_DIR, "static", "index.html")
    # Resolve relative path fallback
    if not os.path.exists(static_html_path):
        static_html_path = os.path.join(STATE_DIR, "..", "static", "index.html")
        
    try:
        with open(static_html_path, "r") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>ArrAutomatic frontend static files are building... Refresh shortly.</h1>", status_code=200)

# Configuration API schema
class ConfigSchema(BaseModel):
    qbittorrentHost: str
    qbittorrentPort: int
    qbittorrentUsername: str
    qbittorrentPassword: str
    stuckLimitMinutes: float
    checkIntervalSeconds: int
    cronExpression: str
    downloadsDirPath: str
    enableOrphanedCleaner: bool
    orphanedCleanerDryRun: bool
    autoReannounce: bool
    injectPublicTrackers: bool
    webhookUrl: str
    webhookType: str
    excludeTags: list
    excludeCategories: list

@api.get("/api/config")
def get_config_endpoint():
    return config

@api.post("/api/config")
def save_config_endpoint(payload: ConfigSchema):
    global config
    
    if payload.cronExpression.strip():
        if not croniter.is_valid(payload.cronExpression):
            raise HTTPException(status_code=400, detail="Invalid Cron Expression formatting.")
            
    config.update(payload.dict())
    save_config()
    logger.info("Configuration updated via web control panel.")
    return {"message": "Configuration saved successfully", "config": config}

@api.post("/api/test-qbt")
def test_qbt_endpoint(payload: dict):
    host = payload.get("host", "localhost")
    port = int(payload.get("port", 8080))
    username = payload.get("username", "admin")
    password = payload.get("password", "adminadmin")
    
    try:
        client = qbittorrentapi.Client(
            host=host,
            port=port,
            username=username,
            password=password
        )
        client.auth_log_in()
        ver = client.app.version
        return {"success": True, "version": ver}
    except Exception as e:
        return {"success": False, "error": str(e)}

@api.get("/api/status")
def get_status_endpoint():
    client = get_qbt_client()
    qbt_connected = client is not None
    
    torrents_list = []
    if qbt_connected:
        try:
            # Load active state
            load_state()
            torrents = client.torrents_info(status_filter="all")
            for t in torrents:
                stuck_info = state.get(t.hash, {})
                torrents_list.append({
                    "name": t.name,
                    "hash": t.hash,
                    "state": t.state,
                    "progress": round(t.progress * 100, 1),
                    "size": t.size,
                    "category": t.category,
                    "tags": t.tags,
                    "duration_stuck": round(stuck_info.get("duration_minutes", 0), 1),
                    "staged_stage": stuck_info.get("staged_stage", "none"),
                    "stuck": stuck_info.get("stuck", False)
                })
        except Exception as e:
            logger.error(f"Error compile status: {e}")
            
    return {
        "qbtConnected": qbt_connected,
        "torrents": torrents_list,
        "stats": config["stats"]
    }

@api.get("/api/logs")
def get_logs_endpoint():
    return log_handler.buffer

@api.post("/api/trigger")
async def trigger_check_endpoint():
    logger.info("Manual check trigger accepted from dashboard.")
    asyncio.create_task(run_agent_turn())
    return {"message": "Torrent check turn initiated."}

@api.post("/api/clean-orphaned")
def clean_orphaned_endpoint(payload: dict = None):
    dry_run = True
    if payload and "dry_run" in payload:
        dry_run = bool(payload["dry_run"])
    else:
        dry_run = config.get("orphanedCleanerDryRun", True)
        
    orig_dry_run = config.get("orphanedCleanerDryRun", True)
    config["orphanedCleanerDryRun"] = dry_run
    res = run_orphaned_cleanup()
    config["orphanedCleanerDryRun"] = orig_dry_run
    return res

# Mount static folder assets
static_assets_path = os.path.join(CURRENT_DIR, "static")
if not os.path.exists(static_assets_path):
    static_assets_path = os.path.join(STATE_DIR, "..", "static")
    
api.mount("/static", StaticFiles(directory=static_assets_path), name="static")

if __name__ == "__main__":
    import uvicorn
    # Start web server
    uvicorn.run(api, host="0.0.0.0", port=4343)
