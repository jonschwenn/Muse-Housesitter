#!/usr/bin/env bash
#
# install.sh — install, upgrade, or uninstall Housesitter.
#
# The owner clones the repo (main, until releases are tagged), reads this
# script, and runs it from that checkout. The code is installed FROM THIS
# CHECKOUT (the repo root is this script's parent directory), so the script
# and the code always match.
#
# Run as the owner's own admin account, NOT root: the script refuses EUID 0
# and uses sudo only for the specific privileged commands.
#
#   install/install.sh install --run-as USER [--with-dashboard] [--restart-musegadget] [--dry-run]
#   install/install.sh upgrade [--restart-musegadget] [--dry-run]
#   install/install.sh uninstall [--purge --run-as USER] [--restart-musegadget] [--yes] [--dry-run]
#   install/install.sh -h | --help
#
# TEST ONLY: setting HOUSESITTER_TEST_ROOT redirects every system path
# under one directory ($HOUSESITTER_TEST_ROOT/opt/housesitter,
# .../usr/local/bin/housesitter, .../etc/systemd/system/..., ...) so tests
# can run with --dry-run without touching the system. Real paths are the
# defaults below. Never set this variable for a real install.
#
# The script never reads, writes, prompts for, or accepts the UniFi secret:
# the owner creates the secret file by hand (see the next steps it prints).
set -euo pipefail

PREFIX="/opt/housesitter"
BIN_LINK="/usr/local/bin/housesitter"
UNIT_PATH="/etc/systemd/system/housesitter-serve.service"
COMMANDS_DIR="/etc/musegadget/commands.d"
MUSEGADGET_PREFIX="/opt/musegadget"

if [ -n "${HOUSESITTER_TEST_ROOT:-}" ]; then
    case "$HOUSESITTER_TEST_ROOT" in
        /*) ;;
        *) printf 'install.sh: HOUSESITTER_TEST_ROOT must be absolute\n' >&2; exit 1 ;;
    esac
    if [[ "$HOUSESITTER_TEST_ROOT" =~ [[:space:][:cntrl:]] ]]; then
        printf 'install.sh: HOUSESITTER_TEST_ROOT must not hold whitespace\n' >&2
        exit 1
    fi
    PREFIX="$HOUSESITTER_TEST_ROOT/opt/housesitter"
    BIN_LINK="$HOUSESITTER_TEST_ROOT/usr/local/bin/housesitter"
    UNIT_PATH="$HOUSESITTER_TEST_ROOT/etc/systemd/system/housesitter-serve.service"
    COMMANDS_DIR="$HOUSESITTER_TEST_ROOT/etc/musegadget/commands.d"
    MUSEGADGET_PREFIX="$HOUSESITTER_TEST_ROOT/opt/musegadget"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
EXAMPLE_CONFIG="$REPO_ROOT/install/config.example.json"
UNIT_TEMPLATE="$REPO_ROOT/install/housesitter-serve.service.in"

DRY_RUN=0

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# run CMD...: execute it, or with --dry-run print it as "would run:" instead.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'would run:'
        printf ' %s' "$@"
        printf '\n'
    else
        "$@"
    fi
}

usage() {
    cat <<'USAGE'
Usage: install.sh <command> [options]

Commands:
  install --run-as USER [--with-dashboard] [--restart-musegadget] [--dry-run]
      Install Housesitter from this checkout into /opt/housesitter.
  upgrade [--restart-musegadget] [--dry-run]
      Re-install Housesitter from this checkout over the existing install.
  uninstall [--purge --run-as USER] [--restart-musegadget] [--yes] [--dry-run]
      Remove the install. --purge also removes a user's config and saved data.

Options:
  --run-as USER        Owner account that owns the config and runs the service.
                       Required for install, and for uninstall --purge.
  --with-dashboard     (install) Install the housesitter-serve systemd unit.
                       It is not enabled or started; enable it after configuring.
  --restart-musegadget Restart the musegadget service when done. Without it,
                       the restart command is printed as a next step.
  --purge              (uninstall) Also remove ~/.config/housesitter and
                       ~/.local/share/housesitter for --run-as USER.
  --yes                (uninstall --purge) Skip the confirmation prompt.
  --dry-run            Print each privileged command as 'would run:' and change nothing.
  -h, --help           Show this help.
USAGE
}

preflight() {
    if [ "$(uname -s)" != "Linux" ]; then
        die "this installer runs on Linux"
    fi
    if ! python3 -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        die "python3 >= 3.9 with the venv module is required"
    fi
}

# check_sdk: warn (never fail: Housesitter installs without the SDK) when
# the Muse SDK is missing, or too old to load drop-in commands. The SDK has
# no usable version number, so look for drop-in support in its installed
# code (added in SDK commit 693ef61862, 2026-10-29). Reads files only; never
# imports or runs the SDK.
check_sdk() {
    local pkg="" d=""
    for d in "$MUSEGADGET_PREFIX"/venv/lib/python3*/site-packages/musegadget; do
        if [ -d "$d" ]; then
            pkg=$d
            break
        fi
    done
    if [ -z "$pkg" ]; then
        warn "Muse SDK not found in $MUSEGADGET_PREFIX; Muse sees Housesitter's commands only once the SDK is installed"
    elif ! grep -rqs --include='*.py' 'commands\.d' "$pkg"; then
        warn "this Muse SDK predates drop-in commands (SDK commit 693ef61862, 2026-10-29);"
        warn "update the SDK first, or Muse will not see Housesitter's commands"
    fi
}

# check_commands_dir: refuse early, before anything is installed, when the
# drop-in path is a symlink (even a broken one) or not a directory;
# install-commands would refuse it after the code is already installed.
check_commands_dir() {
    if [ -L "$COMMANDS_DIR" ] || { [ -e "$COMMANDS_DIR" ] && [ ! -d "$COMMANDS_DIR" ]; }; then
        die "$COMMANDS_DIR is not a real directory; the Muse SDK refuses symlinks. Fix it, then run the installer again"
    fi
}

# check_user USER: validate the account and print its home directory.
check_user() {
    local user=$1 home="" uid="" groups=""
    if ! [[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        die "bad --run-as value '$user': must match ^[a-z_][a-z0-9_-]{0,31}\$"
    fi
    if [ "$user" = "root" ]; then
        die "refusing --run-as root: run as the owner's admin account"
    fi
    if ! uid=$(id -u "$user" 2>/dev/null); then
        die "account '$user' does not exist"
    fi
    if [ "$uid" -eq 0 ]; then
        die "refusing --run-as '$user': that account is root"
    fi
    home=$(getent passwd "$user" 2>/dev/null | cut -d: -f6)
    if [ -z "$home" ]; then
        die "cannot find a home directory for '$user'"
    fi
    case "$home" in
        /*) ;;
        *) die "home directory for '$user' is not absolute" ;;
    esac
    if [[ "$home" =~ [[:space:][:cntrl:]%] ]]; then
        die "home directory for '$user' holds whitespace, control characters, or %"
    fi
    groups=$(id -nG "$user" 2>/dev/null || true)
    case " $groups " in
        *" sudo "* | *" wheel "* | *" admin "*)
            printf "note: '%s' can use sudo; Housesitter never needs it at run time\n" "$user" >&2
            ;;
    esac
    printf '%s' "$home"
}

# warn_trust USER: root builds and installs from this checkout. If the run-as
# account (the account Muse's commands run as) is the account running this
# script, or can write the checkout, Muse could change what root runs here.
warn_trust() {
    local user=$1
    if [ "$user" = "$(id -un)" ]; then
        warn "'$user' is both the run-as account and the account running this installer:"
        warn "Muse's commands can change this checkout before root installs from it (D11)"
    elif [ "$DRY_RUN" -eq 1 ]; then
        say "would check: sudo -u $user test -w $REPO_ROOT (warn if the run-as account can write it)"
    elif sudo -u "$user" test -w "$REPO_ROOT" 2>/dev/null; then
        warn "'$user' (the run-as account) can write $REPO_ROOT, which root installs from (D11)"
    fi
}

# install_code [PIP ARGS]: pip-install a root-owned copy of this checkout, so
# the build never writes into the owner's checkout.
install_code() {
    local src=""
    if [ "$DRY_RUN" -eq 1 ]; then
        say "would run: sudo mktemp -d /tmp/housesitter-src.XXXXXX"
        say "would run: sudo cp -R $REPO_ROOT/. <that directory>"
        say "would run: sudo $PREFIX/bin/pip install --no-cache-dir $* <that directory>"
        say "would run: sudo rm -rf <that directory>"
        return 0
    fi
    src=$(sudo mktemp -d /tmp/housesitter-src.XXXXXX) || return 1
    if sudo cp -R "$REPO_ROOT/." "$src/" && sudo "$PREFIX/bin/pip" install --no-cache-dir "$@" "$src"; then
        sudo rm -rf "$src"
        return 0
    fi
    sudo rm -rf "$src"
    return 1
}

# render_unit USER HOME: print the dashboard unit with placeholders filled.
render_unit() {
    local user=$1 home=$2
    RUN_AS="$user" RUN_AS_HOME="$home" TEMPLATE="$UNIT_TEMPLATE" python3 - <<'PYEOF'
import os
with open(os.environ["TEMPLATE"], encoding="utf-8") as handle:
    text = handle.read()
text = text.replace("@RUN_AS_HOME@", os.environ["RUN_AS_HOME"])
text = text.replace("@RUN_AS@", os.environ["RUN_AS"])
if "@RUN_AS" in text:
    raise SystemExit("template has an unreplaced placeholder")
print(text, end="" if text.endswith("\n") else "\n")
PYEOF
}

# write_unit USER HOME: install the rendered dashboard unit and reload systemd.
write_unit() {
    local user=$1 home=$2 rendered=""
    if [ ! -f "$UNIT_TEMPLATE" ]; then
        die "unit template missing: $UNIT_TEMPLATE"
    fi
    rendered=$(render_unit "$user" "$home")
    if [ -z "$rendered" ]; then
        die "rendered unit is empty"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'would run: sudo install -o root -g root -m 644 <rendered unit> %s\n' "$UNIT_PATH"
        printf 'would run: sudo systemctl daemon-reload\n'
    else
        # Straight from this process to root: no temporary file to swap.
        printf '%s\n' "$rendered" | sudo install -o root -g root -m 644 /dev/stdin "$UNIT_PATH" \
            || die "could not install $UNIT_PATH"
        run sudo systemctl daemon-reload
    fi
}

systemctl_is_active() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return 1
    fi
    systemctl is-active --quiet "$1" 2>/dev/null
}

# musegadget_step: restart the musegadget service, or print the next step.
musegadget_step() {
    if [ "$RESTART_MUSEGADGET" -eq 1 ]; then
        say "warning: restarting musegadget stops any command Muse is running right now"
        run sudo systemctl restart musegadget
    elif [ "$RESTART_PRINTED" -eq 0 ]; then
        say "next step: sudo systemctl restart musegadget"
        say "warning: restarting musegadget stops any command Muse is running right now"
    fi
}

# install_commands: install the drop-ins (install-commands prints the
# musegadget restart step and its warning itself).
install_commands() {
    # In --dry-run the installed binary does not exist yet, so the inner
    # dry-run never runs: say here that install-commands creates the dir.
    if [ "$DRY_RUN" -eq 1 ] && [ ! -e "$COMMANDS_DIR" ]; then
        say "would create: $COMMANDS_DIR (owner root, mode 755; install-commands creates it)"
    fi
    run "$PREFIX/bin/housesitter" install-commands --exec "$PREFIX/bin/housesitter" \
        || die "could not install the command drop-ins; fix the error above, then run '$0 $COMMAND' again (after a failed install, run '$0 uninstall' first)"
    if [ "$DRY_RUN" -eq 0 ]; then
        RESTART_PRINTED=1
    fi
}

cmd_install() {
    local user=$1 home=""
    if [ ! -f "$EXAMPLE_CONFIG" ]; then
        die "example config missing: $EXAMPLE_CONFIG"
    fi
    preflight
    home=$(check_user "$user")
    if [ -e "$PREFIX" ] || [ -L "$PREFIX" ]; then
        die "$PREFIX already exists; run '$0 upgrade' to update it"
    fi
    check_commands_dir
    check_sdk
    warn_trust "$user"
    # Everything inside the run-as account's home runs AS that account, never
    # as root, so a symlink planted there can't redirect a root write. These
    # steps come first, so a failure here leaves nothing installed.
    run sudo -u "$user" -H sh -c 'umask 077; mkdir -p "$1/.config/housesitter/secrets" && chmod 700 "$1/.config/housesitter" "$1/.config/housesitter/secrets"' sh "$home" \
        || die "could not create config directories for '$user'"
    # The example arrives on stdin: the run-as account may not be able to
    # read this checkout (Bookworm homes are mode 700). set -C never clobbers.
    run sudo -u "$user" -H sh -c 'umask 077; f="$1/.config/housesitter/config.json"; if [ -e "$f" ] || [ -L "$f" ]; then exit 0; fi; set -C; cat > "$f"' sh "$home" <"$EXAMPLE_CONFIG" \
        || die "could not seed a config for '$user'"
    run sudo python3 -m venv "$PREFIX" || die "could not create the venv at $PREFIX"
    install_code || die "could not install Housesitter into $PREFIX; to retry, run '$0 uninstall', then install again"
    if [ ! -e "$BIN_LINK" ]; then
        run sudo ln -sfn "$PREFIX/bin/housesitter" "$BIN_LINK"
    elif [ -L "$BIN_LINK" ] && [ "$(readlink "$BIN_LINK")" = "$PREFIX/bin/housesitter" ]; then
        run sudo ln -sfn "$PREFIX/bin/housesitter" "$BIN_LINK"
    else
        warn "$BIN_LINK exists and does not point into $PREFIX; left in place"
    fi
    say "next step: as $user, create $home/.config/housesitter/secrets/unifi_password (mode 600) holding the View Only account's password"
    say "then check the config with: sudo -u $user -H housesitter config-check"
    install_commands
    if [ "$WITH_DASHBOARD" -eq 1 ]; then
        write_unit "$user" "$home"
        say "next step: sudo systemctl enable --now housesitter-serve"
    fi
    musegadget_step
}

cmd_upgrade() {
    local old_version="" new_version="" unit_user="" unit_home=""
    preflight
    if [ ! -d "$PREFIX" ] || [ -L "$PREFIX" ]; then
        die "$PREFIX is not installed; run '$0 install --run-as USER' first"
    fi
    check_commands_dir
    check_sdk
    old_version=$("$PREFIX/bin/housesitter" --version 2>/dev/null || true)
    if [ -z "$old_version" ]; then
        old_version="unknown"
    fi
    if [ -e "$UNIT_PATH" ]; then
        warn_trust "$(grep -m1 '^User=' "$UNIT_PATH" | cut -d= -f2-)"
    fi
    install_code --upgrade || die "could not upgrade Housesitter in $PREFIX"
    install_commands
    if [ -e "$UNIT_PATH" ]; then
        unit_user=$(grep -m1 '^User=' "$UNIT_PATH" | cut -d= -f2-)
        unit_home=$(check_user "$unit_user")
        write_unit "$unit_user" "$unit_home"
        if systemctl_is_active housesitter-serve; then
            run sudo systemctl restart housesitter-serve
        else
            say "housesitter-serve is not running; left stopped"
        fi
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        say "version: $old_version -> (re-run without --dry-run to read the new version)"
    else
        new_version=$("$PREFIX/bin/housesitter" --version 2>/dev/null || true)
        if [ -z "$new_version" ]; then
            new_version="unknown"
        fi
        say "version: $old_version -> $new_version"
    fi
    musegadget_step
}

cmd_uninstall() {
    local user=$1 home="" f="" found=0 do_purge=1 answer=""
    if [ "$PURGE" -eq 1 ]; then
        if [ -z "$user" ]; then
            die "uninstall --purge needs --run-as USER"
        fi
        home=$(check_user "$user")
    elif [ -n "$user" ]; then
        die "uninstall --run-as needs --purge"
    fi
    if [ -e "$UNIT_PATH" ]; then
        run sudo systemctl stop housesitter-serve || true
        run sudo systemctl disable housesitter-serve || true
        run sudo rm -f "$UNIT_PATH"
        run sudo systemctl daemon-reload
    else
        say "no unit file at $UNIT_PATH; skipping"
    fi
    for f in "$COMMANDS_DIR"/housesitter.*.json; do
        if [ -f "$f" ] && [ ! -L "$f" ]; then
            found=1
            run sudo rm -f "$f"
        fi
    done
    if [ "$found" -eq 0 ]; then
        say "no housesitter drop-ins in $COMMANDS_DIR; skipping"
    fi
    if [ -L "$BIN_LINK" ] && [ "$(readlink "$BIN_LINK")" = "$PREFIX/bin/housesitter" ]; then
        run sudo rm -f "$BIN_LINK"
    elif [ ! -e "$BIN_LINK" ] && [ ! -L "$BIN_LINK" ]; then
        say "no $BIN_LINK; skipping"
    else
        say "$BIN_LINK does not point into $PREFIX; left in place"
    fi
    if [ -e "$PREFIX" ] || [ -L "$PREFIX" ]; then
        case "$PREFIX" in
            /opt/housesitter | "${HOUSESITTER_TEST_ROOT:-}"/opt/housesitter) ;;
            *) die "refusing to remove $PREFIX: not the install directory" ;;
        esac
        if [ ! -d "$PREFIX" ] || [ -L "$PREFIX" ]; then
            die "refusing to remove $PREFIX: not a real directory"
        fi
        run sudo rm -rf "$PREFIX"
    else
        say "$PREFIX is not installed; skipping"
    fi
    if [ "$PURGE" -eq 1 ]; then
        local config_dir="$home/.config/housesitter" data_dir="$home/.local/share/housesitter"
        if [ "$DRY_RUN" -eq 1 ]; then
            say "would ask: Remove $config_dir and $data_dir? [y/N]"
        elif [ "$ASSUME_YES" -eq 0 ]; then
            printf 'Remove %s and %s? [y/N] ' "$config_dir" "$data_dir"
            read -r answer || true
            case "$answer" in
                [yY] | [yY][eE][sS]) do_purge=1 ;;
                *)
                    do_purge=0
                    say "purge declined; leaving config and saved data in place"
                    ;;
            esac
        fi
        if [ "$do_purge" -eq 1 ]; then
            run sudo -u "$user" -H rm -rf -- "$config_dir" "$data_dir"
        fi
    fi
    say "reminder: delete the View Only UniFi account made for Housesitter"
    musegadget_step
}

COMMAND=""
RUN_AS=""
WITH_DASHBOARD=0
RESTART_MUSEGADGET=0
PURGE=0
ASSUME_YES=0
RESTART_PRINTED=0

if [ $# -eq 0 ]; then
    usage >&2
    exit 1
fi
COMMAND=$1
shift
case "$COMMAND" in
    -h | --help) usage; exit 0 ;;
    install | upgrade | uninstall) ;;
    *)
        usage >&2
        exit 1
        ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --run-as)
            [ $# -ge 2 ] || die "--run-as needs a value"
            RUN_AS=$2
            shift 2
            ;;
        --with-dashboard) WITH_DASHBOARD=1; shift ;;
        --restart-musegadget) RESTART_MUSEGADGET=1; shift ;;
        --purge) PURGE=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$COMMAND" in
    install)
        [ -n "$RUN_AS" ] || die "install needs --run-as USER"
        if [ "$PURGE" -eq 1 ] || [ "$ASSUME_YES" -eq 1 ]; then
            die "install takes no --purge or --yes"
        fi
        ;;
    upgrade)
        if [ -n "$RUN_AS" ] || [ "$WITH_DASHBOARD" -eq 1 ] || [ "$PURGE" -eq 1 ] || [ "$ASSUME_YES" -eq 1 ]; then
            die "upgrade takes no --run-as, --with-dashboard, --purge, or --yes"
        fi
        ;;
    uninstall)
        if [ "$WITH_DASHBOARD" -eq 1 ]; then
            die "uninstall takes no --with-dashboard"
        fi
        ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    die "refusing to run as root; run as the owner's admin account"
fi

case "$COMMAND" in
    install) cmd_install "$RUN_AS" ;;
    upgrade) cmd_upgrade ;;
    uninstall) cmd_uninstall "$RUN_AS" ;;
esac
