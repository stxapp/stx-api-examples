#!/usr/bin/env bash
# The order round trip with the socket watcher looking on, as GETTING_STARTED
# step 6 runs them in two terminals.
#
#   .github/scripts/watched-roundtrip.sh <py|js> <name>
#
# The watcher joins the market `roundtrip` will pick; once it has joined, the
# round trip places its order and cancels it. Then both outputs are checked:
# the order was placed and cancelled over REST, and the watcher saw the join
# snapshots, the order open and cancelled on orders:<user_id>, and a book
# update. The placed order id is appended to $PLACED.
#
# Public topics send nothing on join, so the book update is only guaranteed
# because the round trip moves the book on the watched market.
set -eo pipefail
lang=$1; name=$2
if [ "$lang" = py ]; then
  watch=("$PY" -u python/websockets/watch.py); roundtrip=("$PY" python/rest/quickstart.py roundtrip); joined='JOIN      ORDER ok'
else
  watch=(node javascript/websockets/watch.mjs); roundtrip=(node javascript/rest/quickstart.mjs roundtrip); joined='^joined orders:'
fi
# The watcher on the market roundtrip will pick, as GETTING_STARTED step 6 does.
market=$("$PY" docs/pick-market.py)
: > "watch-$lang.log"
# The round trip runs in the background once the watcher has joined; the
# watcher runs in the foreground so the SIGINT from timeout reaches it.
(
  for _ in $(seq 1 30); do grep -q "$joined" "watch-$lang.log" && break; sleep 0.5; done
  "${roundtrip[@]}" > "roundtrip-$lang.log"
) &
roundtrip_pid=$!
timeout --preserve-status -k 10 -s INT 25 "${watch[@]}" --market "$market" | tee "watch-$lang.log"
wait "$roundtrip_pid" || { cat "roundtrip-$lang.log"; echo "::error::roundtrip failed on $name"; exit 1; }
cat "roundtrip-$lang.log"
id=$(awk '/^placed /{print $2}' "roundtrip-$lang.log")
[ -n "$id" ] && echo "$id" >> "$PLACED"
fail() { echo "::error::$1 on $name"; exit 1; }
grep -q '^placed .* status=accepted' "roundtrip-$lang.log" || fail "roundtrip placed no order"
grep -q '^cancelled  status=cancelled' "roundtrip-$lang.log" || fail "roundtrip did not cancel its order"
grep -q 'ORDER     all_orders: ' "watch-$lang.log" || fail "the watcher received no orders snapshot"
grep -q 'WALLET    balances' "watch-$lang.log" || fail "the watcher received no balances"
grep -q "ORDER .*id=${id:0:8}.*status=open" "watch-$lang.log" || fail "the watcher did not see the order open"
grep -q "ORDER .*id=${id:0:8}.*status=cancelled" "watch-$lang.log" || fail "the watcher did not see the order cancelled"
grep -qE '^[0-9:]{8}  BOOK ' "watch-$lang.log" || fail "the watcher received no book update"
grep -q '^stopped' "watch-$lang.log" || fail "the watcher did not stop cleanly"
