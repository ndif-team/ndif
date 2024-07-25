import torch
import os


def get_total_cudamemory_MBs(return_ids=False) -> int:

    cudamemory = 0
    
    ids = []

    for device in range(torch.cuda.device_count()):
        try:
            cudamemory += torch.cuda.mem_get_info(device)[1] * 1e-6
            if return_ids:
                ids.append(device)
        except:
            pass

    if return_ids:
        
        return int(cudamemory), ids

    return int(cudamemory)


def set_cuda_env_var(ids = None):
    
    del os.environ["CUDA_VISIBLE_DEVICES"]
    
    if ids == None:
        
        _, ids = get_total_cudamemory_MBs(return_ids=True)

    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join([str(x) for x in ids])
