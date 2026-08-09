from app.services.redis_service import RedisService
import uuid
from app.services.redis_service import RedisService
redis = RedisService()

print("1) PING redis...")
print("PING →", redis.ping())

print("\n2) Enviando job a la cola...")
job_id = str(uuid.uuid4())
job = {
    "jobId": job_id,
    "product": "mug",
    "designPath": "data/designs/test.png"
}
redis.push_job(job)
print("JOB enviado!")

print("\n3) Leyendo job desde la cola...")
job_received = redis.pop_job()
print("Job recibido:", job_received)

print("\n4) Guardando estado del job...")
redis.set_status(job_id, {"status": "processing"})
print("Estado guardado.")

print("\n5) Recuperando estado del job...")
status = redis.get_status(job_id)
print("Estado:", status)
