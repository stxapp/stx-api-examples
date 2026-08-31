# Records the two-terminal walkthrough in GETTING_STARTED.md section 6:
# the watcher in one pane, a place/cancel round trip in the other.
#
#   vhs docs/watch-roundtrip.tape
#
# This places a REAL order on the integration exchange, so it needs a working
# [default] profile. It never touches production.
#
# The market is resolved at record time rather than pinned, so the tape keeps
# working after today's markets settle.
#
# The committed GIF is a fixed illustration, not a live view. It will show an
# expired market and stale prices, which is fine: it exists to show the shape of
# the two-terminal loop. Re-record only if the scripts' output format changes
# enough that the recording misleads.

Output docs/watch-roundtrip.gif

Require tmux
Require python3

Set Shell "bash"
Set FontSize 12
Set Width 1700
Set Height 580
Set Padding 14
Set TypingSpeed 40ms

# Setup, hidden: repo, venv, a clean prompt, and the market roundtrip will pick.
Hide
Type "cd /Users/kannan/projects/sportsx/stx-api-examples" Enter
Type "source python/.venv/bin/activate" Enter
Type "export PS1='$ '" Enter
Type "tmux kill-server 2>/dev/null; true" Enter
Type "export SYM=$(python docs/pick-market.py)" Enter
Type "clear" Enter
Show

# One session, two panes, each labelled by its pane title.
Type "tmux -f docs/demo.tmux.conf new -s demo" Enter
Sleep 1.5s
Ctrl+b
Type "%"
Sleep 1s

Hide
Type "tmux select-pane -t demo.0 -T ' terminal 1  the watcher '" Enter
Type "tmux select-pane -t demo.1 -T ' terminal 2  place and cancel '" Enter
Type "clear" Enter
Ctrl+b
Left
Type "clear" Enter
Show
Sleep 500ms

# Left pane: the watcher.
Type "python python/websockets/watch.py --market $SYM" Enter
Sleep 9s

# Right pane: place and cancel.
Ctrl+b
Right
Sleep 800ms
Type "python python/rest/quickstart.py roundtrip" Enter
Sleep 12s

# Let the last book update land.
Sleep 3s
