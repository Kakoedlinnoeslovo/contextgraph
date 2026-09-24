# Records images/demo.gif with vhs (https://github.com/charmbracelet/vhs).
#
# Setup, once:
#   git clone --depth 1 https://github.com/celery/celery /tmp/celery
#   cd /tmp/celery && contextgraph index
#   contextgraph remember --topic revoke-limits \
#     --text "Revoked task ids live in worker_state.revoked, a LimitedSet capped at REVOKES_MAX (50k) that expires entries after REVOKE_EXPIRES (3h). They survive a restart only when the worker runs with --statedb." \
#     --files celery/worker/state.py --symbols merge_revoked
#
# Record, from the root of this repo:
#   CONTEXTGRAPH_DEMO_REPO=/tmp/celery vhs images/demo.tape

Output images/demo.gif

Require contextgraph

Set Shell bash
Set FontSize 15
Set Width 1300
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful
Set TypingSpeed 30ms

Hide
Type `export PS1='\[\e[1;35m\]celery \[\e[0m\]$ ' LESS='-R' && cd "$CONTEXTGRAPH_DEMO_REPO" && clear`
Enter
Sleep 500ms
Show

# The index is incremental: nothing changed, so this takes a moment.
Type "contextgraph index -q"
Sleep 300ms
Enter
Sleep 2.5s

# Ask about a task. The briefing is paged so each section stays readable.
Type `contextgraph context "What happens when a worker receives a revoke request?" --budget 1200 | less`
Sleep 400ms
Enter
Sleep 5s
Space
Sleep 4s
Space
Sleep 4s
Type "q"
Sleep 500ms

# Zoom in on one symbol: callers, callees, tests.
Type "contextgraph expand Request.revoked"
Sleep 300ms
Enter
Sleep 5s
