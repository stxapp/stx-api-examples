#!/bin/sh
# The shell each recorded tmux pane runs. Repo venv on PATH, bare prompt, no
# user dotfiles, so the GIF shows the commands and not someone's theme.
cd "$(dirname "$0")/.." || exit 1
PATH="$PWD/python/.venv/bin:$PATH"
PS1='$ '
export PATH PS1
exec bash --norc --noprofile
