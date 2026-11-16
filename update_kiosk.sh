#!/usr/bin/env bash
# =====================================================================
# update_kiosk.sh - get the newest TESTED code from GitHub (branch kiosk-stable).
#
# kiosk_autostart.sh runs this at every boot, BEFORE the kiosk starts.
# You can also run it by hand:   bash ~/Desktop/MediKiosk_hardware/update_kiosk.sh
#
# Golden rule: ANY problem -> keep the current version and let the kiosk start.
# A repo is only updated when:
#   - the Pi has kiosk-stable checked out (testing another branch? then it is left alone)
#   - the update is a plain "move forward" (git merge --ff-only: no merging, no conflicts)
#   - it doesn't overwrite a file that was edited on the Pi (git itself refuses that)
#
# Workflow: push to your normal branch -> test it on the Pi
#   (git fetch && git checkout <your branch> && git pull) -> when it works, merge it into
#   kiosk-stable on GitHub -> on the Pi:  git checkout kiosk-stable
# =====================================================================
# Everything runs inside main() so bash reads the whole file first: this script may
# update ITSELF (it's in the hardware repo) while it runs.

STABLE_BRANCH="kiosk-stable"
HW="$HOME/Desktop/MediKiosk_hardware"
FE="$HOME/Desktop/mediKiosk_Frontend"
export GIT_TERMINAL_PROMPT=0   # never wait for a GitHub password - nobody is there to type it

say() { echo "[update] $*"; }

update_repo() {   # update_repo <folder> <name for the log>
  local dir="$1" name="$2" branch here there changed edited
  if [ ! -d "$dir/.git" ]; then
    say "$name: $dir is not a git folder - skipped"
    return
  fi
  cd "$dir" || return

  # 1. only follow kiosk-stable (another branch = you are testing something)
  branch=$(git rev-parse --abbrev-ref HEAD)
  if [ "$branch" != "$STABLE_BRANCH" ]; then
    say "$name: on branch '$branch' (testing?) - not auto-updating. Back to normal: cd $dir && git checkout $STABLE_BRANCH"
    return
  fi

  # 2. ask GitHub what's new (give up after 30 s - e.g. no internet at the health center)
  if ! timeout 30 git fetch --quiet origin "$STABLE_BRANCH"; then
    say "$name: can't reach GitHub - keeping the current version"
    return
  fi
  here=$(git rev-parse HEAD)
  there=$(git rev-parse "origin/$STABLE_BRANCH")
  if [ "$here" = "$there" ]; then
    say "$name: up to date ($(git rev-parse --short HEAD))"
    return
  fi

  # files edited on the Pi (e.g. calibration.json) are fine as long as the update doesn't touch them
  edited=$(git status --porcelain --untracked-files=no)
  [ -n "$edited" ] && say "$name: files edited on the Pi (kept as they are): $(echo "$edited" | awk '{print $2}' | tr '\n' ' ')"

  # 3. move forward - git refuses (and changes nothing) if that would overwrite a Pi edit
  if ! git merge --ff-only --quiet "origin/$STABLE_BRANCH"; then
    say "$name: can't update safely (see the git message above) - keeping the current version"
    return
  fi
  say "$name: UPDATED $(git rev-parse --short "$here") -> $(git rev-parse --short HEAD). New commits:"
  git log --oneline "$here..HEAD" | sed 's/^/[update]     /'
  say "$name: old version was $(git rev-parse --short "$here") (to go back: git reset --hard $(git rev-parse --short "$here"))"

  # 4. new packages?
  changed=$(git diff --name-only "$here" HEAD)
  if echo "$changed" | grep -qxE 'package(-lock)?\.json'; then
    say "$name: website packages changed - running npm install"
    npm install --no-audit --no-fund || say "$name: npm install FAILED - run it by hand: cd $dir && npm install"
  fi
  if echo "$changed" | grep -qx 'requirements.txt'; then
    say "$name: requirements.txt changed - install new Python packages BY HAND (not automatic)"
  fi
}

main() {
  update_repo "$HW" "hardware"
  update_repo "$FE" "website"
}

main "$@"; exit
