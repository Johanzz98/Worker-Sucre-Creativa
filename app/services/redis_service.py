import json
import redis
from app.config.settings import settings  # ← CAMBIO AQUÍ

class RedisService:
    def __init__(self):
        self.client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True  # convierte bytes a string
        )

    def ping(self):
        try:
            return self.client.ping()
        except Exception:
            return False

    # -----------------------------
    # QUEUE (cola de jobs)
    # -----------------------------
    def push_job(self, job):
        """Agrega un job a la cola mockup:jobs"""
        self.client.lpush("mockup:jobs", json.dumps(job))

    def pop_job(self, timeout=5):
        """Espera bloqueando hasta que llegue un job (BRPOP)"""
        result = self.client.brpop("mockup:jobs", timeout=timeout)
        if result:
            _, job_str = result
            return json.loads(job_str)
        return None

    # -----------------------------
    # Job status
    # -----------------------------
    def set_status(self, job_id, data):
        """Guarda el estado del job"""
        key = f"job:{job_id}"
        self.client.set(key, json.dumps(data))

    def get_status(self, job_id):
        key = f"job:{job_id}"
        result = self.client.get(key)
        if result:
            return json.loads(result)
        return None