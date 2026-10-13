import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import SessionLocal
from ..models import AccessKey, Project
from ..projects import project_engine
from ..security import administrator, key_digest

router = APIRouter(prefix="/api/admin", tags=["projects"], dependencies=[Depends(administrator)])


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class NewKey(BaseModel):
    role: Literal["reader", "writer"] = "writer"


def issue_key(db, project_id, role):
    token = "sl_" + secrets.token_urlsafe(32)
    key = AccessKey(project_id=project_id, digest=key_digest(token), role=role)
    db.add(key)
    db.flush()
    return {"id": key.id, "role": role, "token": token}


@router.post("/projects", status_code=201)
def create_project(payload: NewProject):
    with SessionLocal() as db:
        project = Project(name=payload.name)
        db.add(project)
        db.flush()
        key = issue_key(db, project.id, "writer")
        project_engine(project.id)
        db.commit()
        return {"id": project.id, "name": project.name, "key": key}


@router.get("/projects")
def list_projects():
    with SessionLocal() as db:
        return [{"id": p.id, "name": p.name} for p in db.scalars(select(Project).order_by(Project.created_at).limit(1000))]


@router.post("/projects/{project_id}/keys", status_code=201)
def create_key(project_id: str, payload: NewKey):
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(404, "project not found")
        key = issue_key(db, project_id, payload.role)
        db.commit()
        return key


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str):
    with SessionLocal() as db:
        key = db.get(AccessKey, key_id)
        if key is None:
            raise HTTPException(404, "key not found")
        key.revoked = 1
        db.commit()
        return {"revoked": True}
