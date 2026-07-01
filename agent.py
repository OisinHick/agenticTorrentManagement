import os
import time
import json
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import httpx
import qbittorrentapi
from fastapi import FastAPI, HTTPException, UploadFile, File
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
                    "username": "Auto Torrent",
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

# Cached qBittorrent client and connection rate-limiting/backoff state
_qbt_client = None
_qbt_last_failed_settings = None
_qbt_consecutive_failures = 0
_qbt_cooldown_until = 0.0

def normalize_host(host: str) -> str:
    if host and not (host.startswith("http://") or host.startswith("https://")):
        return f"http://{host}"
    return host

# Connect to qBittorrent
def get_qbt_client():
    global _qbt_client, _qbt_last_failed_settings, _qbt_consecutive_failures, _qbt_cooldown_until

    current_settings = (
        config.get("qbittorrentHost"),
        config.get("qbittorrentPort"),
        config.get("qbittorrentUsername"),
        config.get("qbittorrentPassword")
    )

    # If credentials/host settings changed, bypass the cooldown immediately
    if _qbt_last_failed_settings is not None and current_settings != _qbt_last_failed_settings:
        logger.info("qBittorrent connection settings changed. Resetting connection backoff cooldown.")
        _qbt_last_failed_settings = None
        _qbt_consecutive_failures = 0
        _qbt_cooldown_until = 0.0

    if _qbt_client is not None:
        # If connection parameters in config have changed, invalidate cached client
        expected_host = normalize_host(config["qbittorrentHost"])
        if (
            _qbt_client.host != expected_host or
            _qbt_client.port != config["qbittorrentPort"] or
            _qbt_client.username != config["qbittorrentUsername"] or
            getattr(_qbt_client, "_password", None) != config["qbittorrentPassword"]
        ):
            logger.info("qBittorrent connection configuration changed. Resetting cached client.")
            try:
                _qbt_client.auth_log_out()
            except Exception:
                pass
            _qbt_client = None
            # Also reset cooldown when parameters explicitly change
            _qbt_consecutive_failures = 0
            _qbt_cooldown_until = 0.0

    if _qbt_client is None:
        now_time = time.time()
        if now_time < _qbt_cooldown_until:
            # Under cool-down; return None immediately to prevent IP blocking
            return None

        try:
            host = normalize_host(config["qbittorrentHost"])
            client = qbittorrentapi.Client(
                host=host,
                port=config["qbittorrentPort"],
                username=config["qbittorrentUsername"],
                password=config["qbittorrentPassword"],
                FORCE_SCHEME_FROM_HOST=True,
                REQUESTS_ARGS={"timeout": 3.5},
                HTTPADAPTER_ARGS={"max_retries": 0}
            )
            client.auth_log_in()
            _qbt_client = client
            logger.info("Successfully connected and authenticated with qBittorrent.")
            # Reset failure count and cooldown on successful connection
            _qbt_consecutive_failures = 0
            _qbt_cooldown_until = 0.0
            _qbt_last_failed_settings = None
        except Exception as e:
            logger.error(f"Failed to connect to qBittorrent: {e}")
            _qbt_client = None
            _qbt_last_failed_settings = current_settings
            _qbt_consecutive_failures += 1
            # Exponential backoff starting at 5s, doubling, up to a max of 300s (5 minutes)
            backoff = min(5 * (2 ** (_qbt_consecutive_failures - 1)), 300)
            _qbt_cooldown_until = time.time() + backoff
            logger.warning(
                f"qBittorrent connection failed. Cool-down active. "
                f"Will retry in {backoff} seconds (failure #{_qbt_consecutive_failures})."
            )
    return _qbt_client

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

async def get_injectable_trackers():
    trackers = []
    trackers_path = os.path.join(STATE_DIR, "custom_trackers.txt")
    if os.path.exists(trackers_path):
        try:
            with open(trackers_path, "r") as f:
                trackers = [l.strip() for l in f if l.strip()]
        except Exception as e:
            logger.error(f"Error reading custom_trackers.txt: {e}")
            
    # If config allows public trackers or if we have no custom trackers, fetch public trackers
    if config.get("injectPublicTrackers") or not trackers:
        public_trackers = await fetch_public_trackers()
        trackers = list(set(trackers + public_trackers))
        
    return trackers

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
        if isinstance(e, (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error, qbittorrentapi.LoginFailed)):
            global _qbt_client
            _qbt_client = None
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

                    # 2. 75% mark: inject trackers list
                    if duration >= (limit * 0.75) and state[h]["staged_stage"] == "reannounced":
                        logger.info(f"Injecting trackers list for stuck torrent '{name}'...")
                        trackers = await get_injectable_trackers()
                        if trackers:
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
            await asyncio.to_thread(run_orphaned_cleanup)
            
    except Exception as e:
        logger.error(f"Error executing agent turn: {e}", exc_info=True)
        if isinstance(e, (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error, qbittorrentapi.LoginFailed)):
            global _qbt_client
            _qbt_client = None

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: trigger background scheduler daemon
    asyncio.create_task(background_scheduler())
    yield

# FastAPI endpoints
api = FastAPI(lifespan=lifespan)
load_config()
load_state()

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
    global config, _qbt_client, _qbt_consecutive_failures, _qbt_cooldown_until, _qbt_last_failed_settings
    
    if payload.cronExpression.strip():
        if not croniter.is_valid(payload.cronExpression):
            raise HTTPException(status_code=400, detail="Invalid Cron Expression formatting.")
            
    # Reset cached client if credentials/connection parameters change
    if (
        config.get("qbittorrentHost") != payload.qbittorrentHost or
        config.get("qbittorrentPort") != payload.qbittorrentPort or
        config.get("qbittorrentUsername") != payload.qbittorrentUsername or
        config.get("qbittorrentPassword") != payload.qbittorrentPassword
    ):
        logger.info("qBittorrent credentials updated in configuration endpoint. Resetting client and backoff cooldown.")
        if _qbt_client is not None:
            try:
                _qbt_client.auth_log_out()
            except Exception:
                pass
            _qbt_client = None
        # Reset cooldown statistics when credentials change
        _qbt_consecutive_failures = 0
        _qbt_cooldown_until = 0.0
        _qbt_last_failed_settings = None

    config.update(payload.model_dump())
    save_config()
    logger.info("Configuration updated via web control panel.")
    return {"message": "Configuration saved successfully", "config": config}

@api.get("/api/trackers")
def get_trackers_info():
    trackers_path = os.path.join(STATE_DIR, "custom_trackers.txt")
    total = 0
    if os.path.exists(trackers_path):
        try:
            with open(trackers_path, "r") as f:
                total = sum(1 for line in f if line.strip())
        except Exception as e:
            logger.error(f"Error reading trackers size: {e}")
    return {"total": total}

@api.post("/api/trackers/upload")
async def upload_trackers(file: UploadFile = File(...)):
    try:
        content = await file.read()
        text = content.decode("utf-8")
        new_trackers = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if any(line.startswith(proto) for proto in ["udp://", "http://", "https://", "wss://"]):
                    new_trackers.append(line)
        
        if not new_trackers:
            raise HTTPException(status_code=400, detail="No valid tracker URLs found. URLs must start with udp://, http://, https://, or wss://")
        
        trackers_path = os.path.join(STATE_DIR, "custom_trackers.txt")
        existing_trackers = set()
        if os.path.exists(trackers_path):
            with open(trackers_path, "r") as f:
                for l in f:
                    l = l.strip()
                    if l:
                        existing_trackers.add(l)
        
        added_count = 0
        for tracker in new_trackers:
            if tracker not in existing_trackers:
                existing_trackers.add(tracker)
                added_count += 1
                
        with open(trackers_path, "w") as f:
            for tracker in sorted(existing_trackers):
                f.write(tracker + "\n")
                
        logger.info(f"Custom Trackers Uploaded: Added {added_count} new trackers. Total database: {len(existing_trackers)} trackers.")
        return {"success": True, "added": added_count, "total": len(existing_trackers)}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error uploading trackers: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api.post("/api/torrent/{torrent_hash}/inject-trackers")
async def inject_trackers_endpoint(torrent_hash: str):
    mock_hashes = {
        "7d9c8e76a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0",
        "f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5",
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6",
        "0f1e2d3c4b5a6f7e8d9c0b1a2f3e4d5c6b7a8f9e",
        "b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7",
        "d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8",
        "e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9",
        "f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0",
        "a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1",
        "b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2",
        "c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3",
        "d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4",
        "e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5",
        "f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6",
        "a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7",
        "b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8",
        "c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9",
        "d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0",
        "e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1",
        "e1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2",
        "f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3",
        "a3b2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4",
        "b4c3d2e1f0a9b8c7d6e5f4a3f2e1d0c9b8a7f6e5",
        "c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6",
        "d6e5f4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7",
        "e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9d8",
        "f8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9",
        "0a9b8c7d6e5f4a3b2c1d0e9d8c7b6a5f4e3d2c1b",
        "1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c",
        "2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f",
        "3d2e1f0a9b8c7d6e5f4a3b2c1d0e9d8c7b6a5f4e",
        "4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d",
        "5f4a3b2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c",
        "6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f"
    }
    if torrent_hash in mock_hashes:
        logger.info(f"Mock manual inject: simulated injection of trackers into mock torrent '{torrent_hash}'.")
        return {"success": True, "message": "Successfully injected 20 trackers."}

    client = get_qbt_client()
    if not client:
        raise HTTPException(status_code=503, detail="qBittorrent offline")
        
    trackers = await get_injectable_trackers()
    if not trackers:
        raise HTTPException(status_code=400, detail="Tracker database is empty and fallback trackers failed to load.")
        
    try:
        client.torrents_add_trackers(torrent_hash=torrent_hash, urls=trackers)
        if torrent_hash in state:
            state[torrent_hash]["staged_stage"] = "injected"
            save_state()
            
        logger.info(f"Manually injected {len(trackers)} trackers into torrent '{torrent_hash}'.")
        return {"success": True, "message": f"Successfully injected {len(trackers)} trackers."}
    except Exception as e:
        logger.error(f"Failed manual inject: {e}")
        if isinstance(e, (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error, qbittorrentapi.LoginFailed)):
            global _qbt_client
            _qbt_client = None
        raise HTTPException(status_code=500, detail=str(e))

@api.post("/api/test-qbt")
def test_qbt_endpoint(payload: dict):
    host_raw = payload.get("host", "localhost")
    port_raw = payload.get("port", 8080)
    try:
        port = int(port_raw) if port_raw else 8080
    except ValueError:
        return {"success": False, "error": f"Invalid port value: {port_raw}"}
        
    username = payload.get("username", "admin")
    password = payload.get("password", "adminadmin")
    
    try:
        host = normalize_host(host_raw)
        client = qbittorrentapi.Client(
            host=host,
            port=port,
            username=username,
            password=password,
            FORCE_SCHEME_FROM_HOST=True,
            REQUESTS_ARGS={"timeout": 3.5},
            HTTPADAPTER_ARGS={"max_retries": 0}
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
            if isinstance(e, (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error, qbittorrentapi.LoginFailed)):
                global _qbt_client
                _qbt_client = None
    
    if not qbt_connected:
        qbt_connected = True  # Show active visual states for test view
        logger.info("Serving mock test torrent data for dashboard visual testing.")
        torrents_list = [
            {
                "name": "Big Buck Bunny 1080p (2008)",
                "hash": "7d9c8e76a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0",
                "state": "stalledDL",
                "progress": 42.5,
                "size": 1546188226,
                "category": "movies",
                "tags": "keep-stalled",
                "duration_stuck": 12.5,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Ubuntu Linux 26.04 LTS Desktop (amd64)",
                "hash": "f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5",
                "state": "downloading",
                "progress": 87.2,
                "size": 4724464025,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Arch Linux Installation ISO (2026.06.30)",
                "hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                "state": "stalledDL",
                "progress": 12.0,
                "size": 954204160,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 28.4,
                "staged_stage": "injected",
                "stuck": True
            },
            {
                "name": "Sintel Open Source Movie (4K)",
                "hash": "c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6",
                "state": "pausedDL",
                "progress": 0.0,
                "size": 8589934592,
                "category": "movies",
                "tags": "",
                "duration_stuck": 18.0,
                "staged_stage": "reannounced",
                "stuck": False
            },
            {
                "name": "Debian GNU/Linux 13.0 Netinst (i386)",
                "hash": "0f1e2d3c4b5a6f7e8d9c0b1a2f3e4d5c6b7a8f9e",
                "state": "seeding",
                "progress": 100.0,
                "size": 408944640,
                "category": "None",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Blender 3.6 LTS Source Code (zip)",
                "hash": "b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7",
                "state": "downloading",
                "progress": 94.1,
                "size": 154128000,
                "category": "development",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Tears of Steel 4K UHD (2012)",
                "hash": "d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8",
                "state": "stalledDL",
                "progress": 1.5,
                "size": 12456000000,
                "category": "movies",
                "tags": "recovery-active",
                "duration_stuck": 45.2,
                "staged_stage": "injected",
                "stuck": True
            },
            {
                "name": "FreeBSD 15.0-RELEASE (x86_64)",
                "hash": "e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9",
                "state": "seeding",
                "progress": 100.0,
                "size": 1073741824,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Alpine Linux 3.20.0 Virtual ISO",
                "hash": "f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0",
                "state": "downloading",
                "progress": 5.2,
                "size": 52428800,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Cosmos Laundromat (Animated Short)",
                "hash": "a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1",
                "state": "stalledDL",
                "progress": 68.0,
                "size": 2147483648,
                "category": "movies",
                "tags": "",
                "duration_stuck": 15.0,
                "staged_stage": "reannounced",
                "stuck": False
            },
            {
                "name": "Fedora Workstation 44 Live ISO",
                "hash": "b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2",
                "state": "downloading",
                "progress": 50.0,
                "size": 2147483648,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "RetroArch BIOS Pack & Emulators",
                "hash": "c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3",
                "state": "stalledDL",
                "progress": 0.0,
                "size": 536870912,
                "category": "games",
                "tags": "",
                "duration_stuck": 6.5,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Debian 12 Live Gnome (amd64)",
                "hash": "d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4",
                "state": "downloading",
                "progress": 77.5,
                "size": 3145728000,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Alpine Linux Extended 3.20.0 ISO",
                "hash": "e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5",
                "state": "downloading",
                "progress": 15.0,
                "size": 838860800,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Kali Linux Live Installer (2026.2)",
                "hash": "f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6",
                "state": "stalledDL",
                "progress": 8.9,
                "size": 4194304000,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 35.0,
                "staged_stage": "reannounced",
                "stuck": False
            },
            {
                "name": "Tails OS USB Installer (v6.4)",
                "hash": "a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7",
                "state": "downloading",
                "progress": 60.2,
                "size": 1342177280,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "OpenBSD Installation ISO 7.5",
                "hash": "b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8",
                "state": "seeding",
                "progress": 100.0,
                "size": 629145600,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Linux Mint 22 Cinnamon Edition",
                "hash": "c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9",
                "state": "downloading",
                "progress": 31.4,
                "size": 3040870400,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Arch Linux Bootable ISO (v2026.07)",
                "hash": "d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0",
                "state": "stalledDL",
                "progress": 5.0,
                "size": 943718400,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 112.1,
                "staged_stage": "injected",
                "stuck": True
            },
            {
                "name": "Pop!_OS Intel/NVIDIA LTS ISO",
                "hash": "e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1",
                "state": "downloading",
                "progress": 99.9,
                "size": 3565158400,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Manjaro Linux Gnome Edition",
                "hash": "e1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2",
                "state": "downloading",
                "progress": 45.8,
                "size": 3670016000,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "CentOS Stream 9 Boot ISO",
                "hash": "f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3",
                "state": "stalledDL",
                "progress": 22.4,
                "size": 9233376000,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 88.0,
                "staged_stage": "reannounced",
                "stuck": False
            },
            {
                "name": "Slackware Installation DVD",
                "hash": "a3b2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4",
                "state": "downloading",
                "progress": 5.0,
                "size": 4724464000,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Gentoo Minimal Install CD",
                "hash": "b4c3d2e1f0a9b8c7d6e5f4a3f2e1d0c9b8a7f6e5",
                "state": "seeding",
                "progress": 100.0,
                "size": 471859200,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Alpine Linux Netboot Kernel",
                "hash": "c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6",
                "state": "downloading",
                "progress": 81.3,
                "size": 15412800,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Mageia 9 Live GNOME DVD",
                "hash": "d6e5f4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7",
                "state": "stalledDL",
                "progress": 1.1,
                "size": 4194304000,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 210.0,
                "staged_stage": "injected",
                "stuck": True
            },
            {
                "name": "MX Linux x64 ISO Desktop",
                "hash": "e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9d8",
                "state": "downloading",
                "progress": 62.0,
                "size": 2306867200,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Solus Gnome Desktop Edition",
                "hash": "f8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9",
                "state": "seeding",
                "progress": 100.0,
                "size": 2726297600,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Void Linux XBPS Live ISO",
                "hash": "0a9b8c7d6e5f4a3b2c1d0e9d8c7b6a5f4e3d2c1b",
                "state": "downloading",
                "progress": 9.4,
                "size": 943718400,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "NixOS Minimal Install ISO",
                "hash": "1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c",
                "state": "stalledDL",
                "progress": 55.0,
                "size": 1024000000,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 15.0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Rocky Linux Minimal Boot ISO",
                "hash": "2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f",
                "state": "downloading",
                "progress": 89.0,
                "size": 2831155200,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "openSUSE Tumbleweed Live DVD",
                "hash": "3d2e1f0a9b8c7d6e5f4a3b2c1d0e9d8c7b6a5f4e",
                "state": "downloading",
                "progress": 14.5,
                "size": 4724464000,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "EndeavourOS Galileo Installer",
                "hash": "4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d",
                "state": "stalledDL",
                "progress": 0.0,
                "size": 2097152000,
                "category": "operating-systems",
                "tags": "recovery-active",
                "duration_stuck": 120.5,
                "staged_stage": "injected",
                "stuck": True
            },
            {
                "name": "Lubuntu 24.04 LTS Desktop",
                "hash": "5f4a3b2c1d0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c",
                "state": "seeding",
                "progress": 100.0,
                "size": 3019898880,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            },
            {
                "name": "Puppy Linux Frugal ISO",
                "hash": "6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f",
                "state": "downloading",
                "progress": 98.2,
                "size": 471859200,
                "category": "operating-systems",
                "tags": "",
                "duration_stuck": 0,
                "staged_stage": "none",
                "stuck": False
            }
        ]
            
    return {
        "qbtConnected": qbt_connected,
        "torrents": torrents_list,
        "stats": config["stats"]
    }

@api.get("/api/logs")
def get_logs_endpoint():
    return log_handler.buffer

@api.post("/api/logs/clear")
def clear_logs_endpoint():
    log_handler.buffer.clear()
    return {"message": "Logs cleared successfully"}

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
