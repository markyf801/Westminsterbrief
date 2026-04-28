import os


def feature_enabled(flag_name: str, current_user=None) -> bool:
    """
    Check if a feature flag is enabled for the current user.

    Returns True if:
    - Flag is 'live' (everyone sees the feature)
    - Flag is 'beta' AND current_user is authenticated AND email is in BETA_USERS

    Returns False if:
    - Flag is 'hidden' (default — feature does not exist)
    - Flag is 'beta' but user is not authenticated or not in BETA_USERS
    - Flag value is unrecognised (fail closed)

    Flags are read from environment variables at call time.
    Default state is 'hidden' — fail closed if var is unset.
    """
    state = os.environ.get(flag_name, 'hidden').lower().strip()

    if state == 'live':
        return True

    if state == 'beta':
        is_auth = getattr(current_user, 'is_authenticated', False)
        if not is_auth:
            return False
        email = getattr(current_user, 'email', '').lower().strip()
        beta_users = {
            e.strip().lower()
            for e in os.environ.get('BETA_USERS', '').split(',')
            if e.strip()
        }
        return email in beta_users

    # 'hidden' or anything unrecognised
    return False
