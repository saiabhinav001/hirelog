from __future__ import annotations

import logging
from contextlib import asynccontextmanager

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("placement-archive")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ──────────────────────────────────────────────────────────────
    try:
        db.collection("metadata").document("bootstrap").set(
            {"bootstrapped_at": firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        logger.info("Firestore connection verified")
    except Exception:
        logger.exception("Firestore bootstrap write failed — continuing")

    logger.info("FAISS index loaded with %d vectors", faiss_store.index.ntotal)

    try:
        seed_report = ensure_seeded()
        logger.info("Seed data status: %s", seed_report)
    except Exception:
        logger.exception("Seed data check failed — continuing")

    try:
        update_dashboard_stats_async()
        logger.info("Dashboard stats cache refresh triggered")
    except Exception:
        logger.exception("Dashboard stats refresh failed — continuing")

    try:
        repaired = repair_all_practice_list_stats()
        logger.info("Practice list stats reconciled: %d list(s)", repaired)
    except Exception:
        logger.exception("Practice list stats repair failed — continuing")

    logger.info("Startup complete — ENV=%s", settings.ENV)
    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down gracefully")


app = FastAPI(title=settings.API_TITLE, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "placement-archive", "env": settings.ENV}


@app.get("/health")
def health() -> dict:
    return {
        "status": "healthy",
        "faiss_vectors": faiss_store.index.ntotal,
        "env": settings.ENV,
    }


app.include_router(users.router)
app.include_router(experiences.router)
app.include_router(search.router)
app.include_router(dashboard.router)
app.include_router(practice.router)
