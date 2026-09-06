"""Fishie API composition; implementation lives in web_api."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from web_api import (
    accounts,
    auth,
    guilds,
    health,
    history,
    media,
    public,
    state,
    webhooks,
)
from web_api.state import init as init

app = FastAPI(title="Fishie API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(state.WEB_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.middleware("http")(health.protect_cookie_requests)
for router in (
    health.router,
    media.router,
    webhooks.router,
    history.router,
    guilds.router,
    auth.router,
    accounts.router,
    public.router,
):
    app.include_router(router)
