import torch


def get_total_cudamemory_MBs():

    cudamemory = 0

    for device in range(torch.cuda.device_count()):
        cudamemory += torch.cuda.mem_get_info(device)[1] * 1e-6

    return cudamemory
