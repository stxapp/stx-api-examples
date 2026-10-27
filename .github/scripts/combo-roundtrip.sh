#!/usr/bin/env bash
# A combo maker example quoting a live player's request, end to end.
#
#   .github/scripts/combo-roundtrip.sh <py|js> <name>
#
# The maker (combo_maker.py or combo_maker.mjs) runs as the maker account
# ($MAKER_KEY_ID, key in $MAKER_KEY_FILE) while combo-requester.py, as the
# run's own account, asks for a quote on a small combo, takes the best quote and
# cashes the combo out. Other makers quote on the demo exchanges too, so the
# maker quotes generously enough to lead.
#
# Checked: the maker quoted the request, and its quote was the one placed
# (`[won]` for that request: the placed push reaches only the winning maker);
# the player bought, sold and holds nothing at the end. The maker quotes only
# this player's requests (--player). Another maker can still quote higher, so a
# buy placed elsewhere is tried once more with a new request. Winning the cash out too is reported,
# not required.
set -eo pipefail
# Job control, so the maker in the background still takes SIGINT and stops cleanly.
set -m
lang=$1; name=$2
if [ "$lang" = py ]; then
  maker=("$PY" -u python/websockets/combo_maker.py)
else
  maker=(node javascript/websockets/combo_maker.mjs)
fi
log="combo-maker-$lang.log"

# The maker quotes only this run's player, so it never trades with another run.
player=$("$PY" .github/scripts/combo-requester.py --player-id | awk '/^Player:/ { print $2 }')
[ -n "$player" ] || { echo "::error::the requester printed no player id on $name"; exit 1; }
STX_KEY_ID="$MAKER_KEY_ID" STX_KEY_FILE="$MAKER_KEY_FILE" "${maker[@]}" --player "$player" \
  --seconds 300 --win-multiple 12 --cash-out-price 0.40 --quote-seconds 30 > "$log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do grep -q '^Joined combo_quote:' "$log" && break; sleep 0.5; done
grep -q '^Joined combo_quote:' "$log" || { cat "$log"; echo "::error::${maker[*]} did not join combo_quote on $name"; exit 1; }

status=0
for attempt in 1 2; do
  req="combo-requester-$lang-$attempt.log"
  status=0
  "$PY" -u .github/scripts/combo-requester.py | tee "$req" || status=$?
  sleep 2
  buy=$(awk '/^Request:/ { print substr($2, 1, 8) }' "$req")
  if [ "$attempt" = 1 ] && [ "$status" = 0 ] && [ -n "$buy" ] && ! grep -q "^\[won\]      $buy " "$log"; then
    echo "::warning::another maker's quote was placed on $buy on $name; trying once more"
    continue
  fi
  break
done
kill -INT "$pid" 2>/dev/null || true
wait "$pid" || true
echo "--- maker"; cat "$log"

sell=$(awk '/^Cash out:/ { print substr($3, 1, 8) }' "$req")
[ -n "$buy" ] || { echo "::error::the requester opened no request on $name"; exit 1; }
grep -q "^\[quoted\]   $buy " "$log" || { echo "::error::${maker[*]} did not quote request $buy on $name"; exit 1; }
grep -q '^Bought: ' "$req" || { echo "::error::the requester bought nothing on $name"; exit 1; }
grep -q "^\[won\]      $buy " "$log" || { echo "::error::${maker[*]} quoted $buy but another maker's quote was placed on $name"; exit 1; }
grep -q '^Sold: ' "$req" || { echo "::error::the requester did not cash out on $name"; exit 1; }
grep -Eq '^Final:   position 0(\.0+)?  ' "$req" || { echo "::error::the requester left a position on $name"; exit 1; }
grep -q '^Stopped\.' "$log" || { echo "::error::${maker[*]} did not stop cleanly on $name"; exit 1; }
if [ -n "$sell" ] && ! grep -q "^\[won\]      $sell " "$log"; then
  echo "::warning::another maker took the cash out $sell on $name; ${maker[*]} still holds its side of the combo"
fi
exit "$status"
