
def check_valid_email(user_id : str) -> bool:
    '''Helper function which verifies that the `user_id` field contains a "valid" email.'''
    if user_id != '' and user_id is not None and '@' in user_id:
            return True
    return False