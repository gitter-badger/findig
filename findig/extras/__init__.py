from warnings import warn
from traceback import print_exc

try:
    from .redis import *
except ImportError:
    print_exc()
    warn("Redis support is not available. Run `pip install redis` to enable.")