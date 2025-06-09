        
import random
from typing import Dict, List, Optional


from ray._private import services
from ray._private.internal_api import (get_memory_info_reply,
                                       get_state_from_address)
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.dashboard.memory_utils import (construct_memory_table,
                                        get_group_by_type, get_sorting_type,
                                        memory_summary, node_stats,
                                        node_stats_to_dict)
from ray.util.state import list_nodes

from ... import MODEL_KEY
from .deployment import DeploymentLevel
from .evaluator import ModelEvaluator
from .node import CandidateLevel, Node, Resources

from .....logging.logger import load_logger

LOGGER = load_logger("Controller")

class Cluster:
    
    def __init__(self):
        
        self.nodes:Dict[str, Node] = {}
        
        self.evaluator = ModelEvaluator()
        
        self._state = None
        
        self.update_nodes()
        
    @property
    def state(self):
        
        if self._state is None:
        
            address = services.canonicalize_bootstrap_address_or_die(None)

            state = GlobalState()
            options = GcsClientOptions.create(
                address, None, allow_cluster_id_nil=True, fetch_cluster_id_if_nil=False
            )
            state._initialize_global_state(options)
            
            self._state = state
        
        return self._state
    
    def update_nodes(self):
        
        LOGGER.info("Updating nodes...")
        
        nodes = list_nodes(detail=True)
        
        available_resources = self.state.total_resources_per_node()
                
        current_nodes = set()
        
        for node in nodes:
            
            id = node.node_id
            name = node.node_name
            
            current_nodes.add(id)
            
            if id not in self.nodes:
            
                total_gpus = node.resources_total["GPU"]
                gpu_type = "TEST"
                # gpu_type = node.resources_total["GPU_TYPE"]
                gpu_memory_bytes = (node.resources_total["cuda_memory_MB"] * 1024 * 1024) / total_gpus
                total_memory_bytes = node.resources_total["object_store_memory"]
                
                self.nodes[id] = Node(id, name,Resources(
                    total_gpus=total_gpus,
                    gpu_type=gpu_type,
                    gpu_memory_bytes=gpu_memory_bytes,
                    total_memory_bytes=total_memory_bytes,))
                
            resources = available_resources[id]
            
            self.nodes[id].resources.available_gpus = resources.get("GPU", 0)
            self.nodes[id].resources.available_memory_bytes = resources.get("object_store_memory", 0)
            
            LOGGER.info(f"=> Node {name} updated with resources: {self.nodes[id].resources}")
            
        for node in self.nodes.keys():
            if node not in current_nodes:
                
                del self.nodes[node]
                
                LOGGER.info(f"=> Node {node} removed from cluster")
                
    def deploy(self, model_keys:List[MODEL_KEY], dedicated:Optional[bool]=False):
        
        LOGGER.info(f"Cluster deploying models: {model_keys}, dedicated: {dedicated}...")
        
        model_sizes_in_bytes = {model_key:self.evaluator(model_key) for model_key in model_keys}
        
        if dedicated:
            
            LOGGER.info("=> Checking to evict deprecated dedicated deployments...")
            
            for node in self.nodes.values():
                
                for model_key, deployment in list(node.deployments.items()):
                    
                    if deployment.deployment_level == DeploymentLevel.DEDICATED and model_key not in model_sizes_in_bytes:
                        
                        LOGGER.info(f"==> Evicting deprecated dedicated deployment {model_key} from {node.id}")
                        
                        node.evict(model_key)
                        
        # Sort models by size in descending order
        sorted_models = sorted(model_sizes_in_bytes.items(), key=lambda x: x[1], reverse=True)
        
        for model_key, size_in_bytes in sorted_models:
            
            LOGGER.info(f"=> Evaluating {model_key} with size {size_in_bytes}...")
            
            candidates = {}
            
            for node in self.nodes.values():
                
                LOGGER.info(f"==> Evaluating {model_key} for node {node.name}...")
                
                candidate = node.evaluate(model_key, size_in_bytes, dedicated=dedicated)
                
                LOGGER.info(f"==> Candidate: {candidate.candidate_level}, evictions: {candidate.evictions}, gpus_required: {candidate.gpus_required}")
                
                if candidate.candidate_level == CandidateLevel.DEPLOYED:
                    
                    break
                
                if len(candidates) == 0:
                    
                    candidates[node.id] = candidate
                    
                else:
                    
                    current_candidate_level = list(candidates.values())[0].candidate_level
                    
                    if candidate.candidate_level == current_candidate_level:
                        
                        candidates[node.id] = candidate
                        
                    elif candidate.candidate_level > current_candidate_level:
                        
                        candidates = {node.id:candidate}
                        
            node_id, candidate = random.choice(list(candidates.items()))
            
            LOGGER.info(f"==> Deploying {model_key} with size {size_in_bytes} on {self.nodes[node_id].name}")
            
            self.nodes[node_id].deploy(model_key, candidate, dedicated=dedicated)
            
                        
            
            
    # def get_object_store_info(self):
            
    #     group_by = "NODE_ADDRESS"
    #     sort_by = "OBJECT_SIZE"
        
    #     state = get_state_from_address()
        
    #     core_worker_stats = []
        
    #     for raylet in state.node_table():
    #         if not raylet["Alive"]:
    #             continue
    #         try:
    #             stats = node_stats_to_dict(
    #                 node_stats(raylet["NodeManagerAddress"], raylet["NodeManagerPort"])
    #             )
    #         except RuntimeError:
    #             continue
    #         core_worker_stats.extend(stats["coreWorkersStats"])
    #         assert type(stats) is dict and "coreWorkersStats" in stats

    #     # Build memory table with "group_by" and "sort_by" parameters
    #     group_by, sort_by = get_group_by_type(group_by), get_sorting_type(sort_by)
    #     memory_table = construct_memory_table(
    #         core_worker_stats, group_by, sort_by
    #     )
        
    #     return memory_table
    


