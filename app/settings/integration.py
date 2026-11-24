# Add your integration-specific settings here
from environs import Env

env = Env()

# The NASA Earthdata app key shared by every FIRMS integration that does not bring its own.
FIRMS_APP_KEY = env.str("FIRMS_APP_KEY", None)
