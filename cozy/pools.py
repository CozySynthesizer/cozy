"""Definitions for expression pools.

During synthesis, expressions belong to one of two pools: the runtime pool for
expressions executed when the method is called, and the state pool for
expressions that are part of the abstraction relation.

This module declares constants for the two pools and a `pool_name` function to
print them.
"""

# TODO: `import enum` and make Pool a proper enum type
Pool = int
STATE_POOL   = 0
RUNTIME_POOL = 1
ALL_POOLS = (STATE_POOL, RUNTIME_POOL)

_POOL_NAMES = ("state", "runtime")
def pool_name(pool):
    return _POOL_NAMES[pool]
