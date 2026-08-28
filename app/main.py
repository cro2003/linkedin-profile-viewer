import logging

from fastapi import FastAPI

from app.config import settings
from app.db import redis, mongo

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="LinkedIn Profile API", version="0.1.0")


@app.get("/health")
async def health():
    deps = {}
    for name, ping in (("mongo", mongo.admin.command("ping")), ("redis", redis.ping())):
        try:
            await ping
            deps[name] = "ok"
        except Exception as e:
            deps[name] = f"error: {type(e).__name__}"

    ok = all(v == "ok" for v in deps.values())
    return {"status": "ok" if ok else "degraded", "deps": deps, "accounts": len(settings.linkedin_accounts)}
