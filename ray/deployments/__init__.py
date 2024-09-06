from ray.serve._private.config import ReplicaConfig
 
# Otherwise we cant use concurrency groups in ray serve deployments. No one can stop me
ReplicaConfig._validate_ray_actor_options = lambda self: None

