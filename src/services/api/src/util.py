import re
import sys
from nnsight import __version__

def check_valid_email(user_id : str) -> bool:
    '''Helper function which verifies that the `user_id` field contains a "valid" email.'''
    if user_id != '' and user_id is not None and '@' in user_id:
            return True
    return False

def verify_python_version(python_version: str):
    '''Helper function which verifies that the `python_version` field contains a valid python version. Raises an exception if the version is not valid.'''
    
    server_python_version = '.'.join(sys.version.split('.')[0:2]) # e.g. 3.12
    user_python_version = '.'.join(python_version.split('.')[0:2])
    if user_python_version != server_python_version:
        raise Exception(f"Client python version {user_python_version} does not match server version: {server_python_version}\nPlease update your python version and try again.")

def verify_nnsight_version(nnsight_version: str):
        '''Helper function which verifies that the `nnsight_version` field contains a valid nnsight version. Raises an exception if the version is not valid.'''

        # Extract just the base version number (e.g. 0.4.7 from 0.4.7.dev10+gbcb756d)
        server_nnsight_version = re.match(r'^(\d+\.\d+\.\d+)', __version__).group(1)
        user_base_version = re.match(r'^(\d+\.\d+\.\d+)', nnsight_version).group(1)
        if user_base_version != server_nnsight_version:
                raise Exception(f"Client version {user_base_version} does not match server version {server_nnsight_version}\nPlease update your nnsight version `pip install --upgrade nnsight`")