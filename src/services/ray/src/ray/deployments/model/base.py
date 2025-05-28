import torch


def cpu():
    
    tensor = torch.randn((1000,1000,10), device="cpu")
    
    torch.save(tensor, "tensor.pt")
    
def gpu():
    tensor = torch.randn((1000,1000,10), device="cuda")
    
    torch.save(tensor, "tensor.pt")
    


import time

cpu()

start = time.time()

for i in range(100):
    cpu()
    
end = time.time()

print((end - start) / 100)

start = time.time()

for i in range(100):
    gpu()
    
end = time.time()

print((end - start) / 100)