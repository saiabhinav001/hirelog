from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import firestore

from app.api.routes import dashboard, experiences, practice, search, users
from app.core.config import settings
from app.core.firebase import db
from app.services.faiss_store import faiss_store
from app.services.seed_data import ensure_seeded
from app.api.routes.practice import repair_all_practice_list_stats
from app.api.routes.dashboard import update_dashboard_stats_async

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("placement-archive")

app = FastAPI(title=settings.API_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def bootstrap() -> None:
    db.collection("metadata").document("bootstrap").set(
        {"bootstrapped_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    logger.info("FAISS index loaded with %s vectors", faiss_store.index.ntotal)
    seed_report = ensure_seeded()
    logger.info("Seed data status: %s", seed_report)
    update_dashboard_stats_async()
    logger.info("Dashboard stats cache refreshed")
    repaired = repair_all_practice_list_stats()
    logger.info("Practice list stats reconciled: %d list(s)", repaired)


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "placement-archive"}


app.include_router(users.router)
app.include_router(experiences.router)
app.include_router(search.router)
app.include_router(dashboard.router)
app.include_router(practice.router)
