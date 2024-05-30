
import os

broker_url = os.getenv("RMQ_URL", 'amqp://guest:guest@localhost:5672/')
result_backend = os.getenv("MONGO_URL", 'mongodb://user:pass@localhost:5001/celery_result_backend')

broker_connection_retry_on_startup = True
broker_heartbeat = 10

task_serializer = 'pickle'
result_serializer = 'pickle'

accept_content = ['pickle']

worker_pool_restarts = True
