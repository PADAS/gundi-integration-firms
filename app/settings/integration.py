# Add your integration-specific settings here
from environs import Env

env = Env()

# The NASA Earthdata app key shared by every FIRMS integration that does not bring its own.
# In a deployment it is a mounted secret file, read at the start of every run, so that a replaced key is picked up
# without restarting the service. The variable holding the key itself is for running locally.
FIRMS_APP_KEY_FILE = env.str("FIRMS_APP_KEY_FILE", None)
FIRMS_APP_KEY = env.str("FIRMS_APP_KEY", None)
