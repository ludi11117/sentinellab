import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://agent:agent@localhost:5432/agent_runtime",
)

PROJECT_DATA_DIR = Path(os.getenv("SENTINEL_PROJECT_DATA", str(Path(__file__).resolve().parents[2] / "data" / "projects")))
ADMIN_TOKEN = os.getenv("SENTINEL_ADMIN_TOKEN", "")
ALLOW_LOCAL = os.getenv("SENTINEL_ALLOW_LOCAL", "1") == "1"
