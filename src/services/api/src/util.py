import os
import sys
from packaging.version import Version
from nnsight import __version__

MIN_NNSIGHT_VERSION = os.getenv('MIN_NNSIGHT_VERSION', __version__)
# TODO: At some point, we might want to have the supported python versions also be configurable

def check_valid_email(user_id : str) -> bool:
    '''Helper function which verifies that the `user_id` field contains a "valid" email.'''
    if user_id != '' and user_id is not None and '@' in user_id:
            return True
    return False

def verify_python_version(python_version: str):
    '''Helper function which verifies that the `python_version` field contains a valid python version. Raises an exception if the version is not valid.'''
    
    server_python_version = '.'.join(sys.version.split('.')[0:2]) # e.g. 3.12
    user_python_version = '.'.join(python_version.split('.')[0:2])

    if user_python_version == '':
        raise Exception("Client python version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again.")

    elif user_python_version != server_python_version:
        raise Exception(f"Client python version {user_python_version} does not match server version: {server_python_version}\nPlease update your python version and try again.")

def verify_nnsight_version(nnsight_version: str):
        '''Helper function which verifies that the `nnsight_version` field contains a valid nnsight version. Raises an exception if the version is not valid.'''

        if nnsight_version == '':
            raise Exception("Client nnsight version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again.")

        min_nnsight_version = Version(MIN_NNSIGHT_VERSION)
        user_nnsight_version = Version(nnsight_version)

        if user_nnsight_version < min_nnsight_version:
                raise Exception(f"Client nnsight version {user_nnsight_version} is incompatible with the server nnsight version. The minimum supported version is {min_nnsight_version}\nPlease update nnsight to the latest version: `pip install --upgrade nnsight`")
