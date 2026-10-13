import re
import threading

from sqlalchemy import create_engine

from .config import PROJECT_DATA_DIR
from .db import Base, configure_sqlite, engine

_engines = {}
_lock = threading.Lock()


def project_engine(project_id):
    if project_id == "local":
        return engine
    if not re.fullmatch(r"[a-f0-9-]{36}", project_id):
        raise ValueError("invalid server project id")
    with _lock:
        if project_id not in _engines:
            PROJECT_DATA_DIR.mkdir(parents=True, exist_ok=True)
            target = (PROJECT_DATA_DIR / (project_id + ".db")).resolve()
            if target.parent != PROJECT_DATA_DIR.resolve():
                raise ValueError("project path escapes data directory")
            project = create_engine("sqlite:///" + target.as_posix(), connect_args={"check_same_thread": False, "timeout": 15})
            configure_sqlite(project)
            Base.metadata.create_all(project)
            _engines[project_id] = project
        return _engines[project_id]
