from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from app.config import settings

mongo = AsyncIOMotorClient(settings.mongo_url)
db = mongo[settings.mongo_db]
redis = Redis.from_url(settings.redis_url, decode_responses=True)

profiles = db.profiles
jobs = db.jobs
