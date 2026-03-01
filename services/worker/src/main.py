import redis
import logging
import time
import json
from pathlib import Path
import sys

# Support both direct-script and module execution.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from platform_shared.config import load_service_config

CONFIG = load_service_config(caller_file=__file__)

# Set up formatting
logging.basicConfig(
    level=CONFIG.log_level,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("Worker")

logger.info(f"Worker will connect to Redis at {CONFIG.redis_host}:{CONFIG.redis_port}")

def run_worker():
    with redis.Redis(
        host=CONFIG.redis_host,
        port=CONFIG.redis_port,
        db=CONFIG.redis_db,
        password=CONFIG.redis_password,
    ) as r:
        logger.info("Worker online. Waiting for frames...")
        
        try:
            while True:
                # 1. Wait for data
                # brpop returns a tuple: (queue_name, data)
                result = r.brpop(CONFIG.queue_name)
                
                if result:
                    _, raw_data = result
                    packet = json.loads(raw_data)
                    
                    # 2. Performance Tracking
                    latency = time.time() - packet['timestamp']
                    logger.info(f"Received frame {packet['id']} | Latency: {latency:.4f}s")
                    
                    # ML Logic will go here
                
        except KeyboardInterrupt:
            logger.info("Worker shutting down...")
    
    logger.info("Worker connection closed.")

if __name__ == "__main__":
    run_worker()
