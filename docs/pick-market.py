"""Print the symbol quickstart.py roundtrip will choose.

Used by docs/watch-roundtrip.tape so the recording points the watcher at the
same market the round trip acts on, without pinning a symbol that settles.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
import stx  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "quickstart", os.path.join(ROOT, "python", "rest", "quickstart.py")
)
quickstart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quickstart)

config = stx.load_profile(None)
private_key = stx.load_private_key(config)
markets = quickstart.tradeable_markets(quickstart.list_markets(config, private_key))
print(next(m for m in markets if m.get("bids"))["symbol"])
