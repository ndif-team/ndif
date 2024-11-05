import os
import pickle
from nnsight.schema.request import RequestModel

REQUEST_DIR = '/tmp/requests/'

def _save_request(request: RequestModel):
    # Ensure the directory exists
    os.makedirs(REQUEST_DIR, exist_ok=True)
    
    # Construct the file path using the request's ID
    file_path = os.path.join(REQUEST_DIR, f'{request.id}.pkl')
    
    # Open the file in binary write mode and pickle the request object
    with open(file_path, 'wb') as f:
        pickle.dump(request, f)