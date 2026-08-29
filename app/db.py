"""Shared Mongo and Redis clients.

Mongo holds durable state: profiles, jobs, accounts, users, config. Redis holds
everything ephemeral: the job queue, sessions, rate-limit windows, account
scheduling and job progress events.
"""

from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.config import settings

mongo = AsyncIOMotorClient(settings.mongo_url)
db = mongo[settings.mongo_db]
redis = Redis.from_url(settings.redis_url, decode_responses=True)

profiles = db.profiles
jobs = db.jobs
