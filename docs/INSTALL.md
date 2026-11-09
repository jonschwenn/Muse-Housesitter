# Installing Housesitter

## Guided install with Muse

Muse runs commands as the SDK's run-as account, without a sudo password.
Housesitter is read-only: Muse must not change anything in UniFi.

1. **Muse: check requirements.** Confirm Python 3.9+ with venv, git,
   openssl, internet access to GitHub and PyPI, and the running SDK:

   ```sh
   python3 -c 'import sys, venv; print(sys.version); assert sys.version_info >= (3, 9)'
   git --version
   openssl version
   systemctl status musegadget
   ```

   Confirm the SDK includes drop-in command support (commit `693ef61862`,
   2026-10-29). Its `0.1.0` version alone does not establish this; the
   installer dry run also warns about missing support. If it is too old,
   tell the owner to update the SDK first by re-running the SDK's
   `install.sh`. Confirm the run-as account name for the commands below.

2. **Muse: read and preview.** Clone into your own run-as account to read
   the installer and run the dry run only. Replace `<run-as account>` with
   that account's name:

   ```sh
   git clone https://github.com/jonschwenn/Muse-Housesitter.git
   cd Muse-Housesitter
   less install/install.sh
   bash install/install.sh install --run-as <run-as account> --with-dashboard --dry-run
   ```

   Show the owner what it would do and every warning. This preview always
   warns about D11, because here you are both the run-as account and the
   account running the installer; that is expected for this preview clone,
   which root never installs from. Use a dry run before every install,
   upgrade, or uninstall.

3. **You: install from your own account.** Muse cannot run the privileged
   install, must not ask for sudo, and must not suggest passwordless sudo.
   Muse gives you this one command, filling in `RUN_AS_USER`. Run it from
   your own sudo-capable account, not root. It clones a fresh copy in your
   account so root installs from a checkout Muse cannot change:

   ```sh
   git clone https://github.com/jonschwenn/Muse-Housesitter.git ~/Muse-Housesitter && bash ~/Muse-Housesitter/install/install.sh install --run-as RUN_AS_USER --with-dashboard
   ```

   If you have only one account and it is the run-as account, the installer
   warns about D11; see [About sudo](#1-check-the-pi-and-accounts).
   D11 remains open. If installation fails partway, from `~/Muse-Housesitter`
   preview `bash install/install.sh uninstall --dry-run`, then run
   `bash install/install.sh uninstall`, then run the installer again from
   that same checkout (the one-line command's `git clone` fails once the
   directory exists).

4. **You: create the account and save the password.** In the UniFi console,
   create a dedicated local **View Only** account for Network and the site,
   with remote/cloud access disabled and no SSO; Muse cannot do this.
   Muse never asks for, receives, or types the UniFi password. You write it
   yourself in a Bash shell as the run-as account (open one with
   `sudo -u RUN_AS_USER -H bash` if needed):

   ```sh
   set +x
   umask 077
   mkdir -p ~/.config/housesitter/secrets
   chmod 700 ~/.config/housesitter/secrets
   read -r -s -p 'UniFi password: ' unifi_password
   printf '\n'
   touch ~/.config/housesitter/secrets/unifi_password
   chmod 600 ~/.config/housesitter/secrets/unifi_password
   printf '%s' "$unifi_password" > ~/.config/housesitter/secrets/unifi_password
   unset unifi_password
   ```

   The secret file belongs to the run-as account, with mode 600 in its mode
   700 directory. Never put the password in config, arguments, or a message
   to Muse. Leave this shell with `exit` if you opened it above.

   Muse can technically read this file, because it runs as the same
   account. Muse must never read, print, copy, or hash it; checking it with
   `ls -l` or `stat` is fine.

5. **Muse: configure and check.** Edit
   `~/.config/housesitter/config.json` in your run-as account using values
   the owner gives: `base_url`, `site`, explicit `flavor`, `auth.username`,
   and `tls`, as in [controller configuration](#4-configure-the-controller).
   Keep `auth.password_secret` as `unifi_password`. For TLS pinning you may
   print the presented fingerprint:

   ```sh
   openssl s_client -connect <host>:<port> </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
   ```

   The owner must confirm it against the console through a trusted
   independent source before you put the hex value into config. This
   command alone does not authenticate the console. Do not disable TLS
   verification. Then run as yourself and report all findings:

   ```sh
   housesitter config-check
   housesitter doctor
   housesitter doctor --check
   ```

6. **You: restart the SDK; Muse: confirm commands loaded.**
   **Restarting musegadget kills any command Muse is running at that moment.**
   From your own sudo-capable account:

   ```sh
   sudo systemctl restart musegadget
   ```

   If you want the dashboard now and at boot, also run
   `sudo systemctl enable --now housesitter-serve`; read
   [Open the dashboard](#9-open-the-dashboard) before exposing its port.
   Muse checks that `housesitter.describe` is now a Muse command. You can
   confirm loading with `sudo journalctl -u musegadget | grep commands`.
   Continue with [a first snapshot](#8-collect-a-first-snapshot) if wanted.

## Manual install

### 1. Check the Pi and accounts

Start with a Raspberry Pi already paired with Muse through the
[Linux gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/main/linux).
The SDK requires Python 3.9 or later and a supported Linux system (including
Raspberry Pi OS Bullseye or later). Check Python and the running service:

```sh
python3 --version
systemctl status musegadget
```

Update the SDK first by re-running its `install.sh`, as described in its
README's **Install** section. Housesitter needs support for commands in
`/etc/musegadget/commands.d/`, added in SDK commit `693ef61862` on
2026-10-29 (PR #85). The SDK version still says `0.1.0` and has no tags,
so the version alone does not tell you. Older SDKs load only built-in
commands, so Housesitter's commands will not appear. The installer warns
if it finds no SDK in `/opt/musegadget` or one without this support; step 7
shows how to confirm the SDK loaded the commands. The SDK does not create
`commands.d`; Housesitter's installer creates it root-owned with mode 755
if missing.

You also need git, openssl, Python's venv support, and sudo on your own
account. During install and upgrade, the Pi needs internet access to GitHub
for the clone and to PyPI, where pip downloads setuptools to build the
package. After installation, Housesitter needs only access to the UniFi
console's HTTPS port.
Know which account the SDK runs commands as: it is the account chosen at SDK
installation, or the account supplied to its `--run-as` option. Use that
same account for Housesitter; below, replace `RUN_AS_USER` with its name.

> **About sudo.** Installation needs sudo to write system locations. The
> SDK runs commands with its run-as account's permissions; if that account
> can use sudo, so can Muse. If it can write the checkout, it can also change
> what root builds and installs. Housesitter needs no sudo at runtime.
> D11 remains open for Jon to decide.

### 2. Create a UniFi account

Sign in to your console as its owner. In the **Admins & Users** area
(menu names vary by version), create a dedicated **local** account used
only by Housesitter. Give it **View Only** access to the Network application
and the site you want to observe. Disable remote/cloud access and do not
use SSO. Keep its username for config and its password for the secret file
below. Housesitter gathers measurements; it never changes UniFi settings.

To check an existing account, open it in the console's **Admins & Users**
list and confirm its Network role or permission reads **View Only** (names
vary by version). Console errors or other scripts may call any local
account "admin"; trust the role shown in the console.

### 3. Read and run the installer

Run these commands as your own sudo-capable account, **not root**. The
installer refuses root and uses sudo only for specific installation steps.
Clone into a directory in your own account that the run-as account cannot
write. The installer builds and installs from the checkout as root, and
Muse's commands run as the run-as account, so if that account can change
the checkout, Muse could change what root runs. This includes using your
own account as the run-as account; the SDK defaults to the account that
installed the device.

```sh
git clone https://github.com/jonschwenn/Muse-Housesitter.git
cd Muse-Housesitter
less install/install.sh
bash install/install.sh install --run-as RUN_AS_USER --with-dashboard --dry-run
bash install/install.sh install --run-as RUN_AS_USER --with-dashboard
```

The dry run prints commands without running them; use it before every
install, upgrade, or uninstall. The installer warns on stderr if the run-as
account is the account running it or can write the checkout. It first
creates the run-as account's `~/.config/housesitter` and `secrets/` directories
with mode 700 and seeds the example config with mode 600 if none exists,
running those steps as that account before installing anything system-wide.
It then installs from a root-owned temporary copy of the checkout into a
root-owned venv at `/opt/housesitter`, with a `/usr/local/bin/housesitter`
symlink; nothing is written into the clone. It never handles the password.
It installs the command drop-ins and, with `--with-dashboard`, a hardened
`housesitter-serve.service` unit, but does not start it. Omit that option
if you only want to run the dashboard in the foreground.

If installation fails partway, run `bash install/install.sh uninstall`,
then install again. Install refuses when `/opt/housesitter` already exists.

To have Muse do the unprivileged steps, see
[Guided install with Muse](#guided-install-with-muse).

### 4. Configure the controller

Config, secrets, and saved data belong to the **run-as account**, not
necessarily your own account. Open a shell as that account for the next
steps (if you are already that account, just use your existing shell):

```sh
sudo -u RUN_AS_USER -H bash
nano ~/.config/housesitter/config.json
```

Check that `modules.unifi_wifi.enabled` is `true`. Edit these fields inside
`modules.unifi_wifi`:

- `base_url`: the console's HTTPS URL, with no path. UniFi OS commonly uses
  port 443 (`https://192.0.2.1`); classic Network commonly uses port 8443
  (`https://192.0.2.1:8443`). Replace these invented addresses with yours.
  The URL must include any non-default HTTPS port.
- `site`: the site's API identifier, often `default`, rather than its display name.
- `flavor`: explicitly `unifi_os` for a UniFi OS console or `classic` for a
  self-hosted Network application. `auto` is not usable yet.
- `auth.username`: the local View Only account's username.
  Leave `auth.password_secret` as `unifi_password`; it names a file.
- `tls`: use system certificate verification (`{}`) for a trusted certificate,
  `ca_file` for your own CA certificate, or `fingerprint_sha256` to pin the
  console's leaf certificate. Do not disable verification for setup.

For a pin, run this on the Pi, replacing `<host>:<port>` with your console's
host and HTTPS port:

```sh
openssl s_client -connect <host>:<port> </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
```

Confirm the fingerprint through a trusted independent source before trusting
it; this command alone does not authenticate the console. Copy only the hex
value after `=` into `tls.fingerprint_sha256`. The config accepts 64 hex
digits, case-insensitive, with optional colons or whitespace. It does not
accept the `sha256 Fingerprint=` prefix. Printing the fingerprint from
Housesitter itself is **Planned**. A replaced certificate requires a new,
independently verified pin. See the [module spec](modules/unifi_wifi.md)
for the complete config and TLS options.

### 5. Save the password privately

Still in the run-as account's Bash shell, run the following. Type the
password at the prompt; it will not echo. Do not enable shell tracing
(`set -x`). This keeps the password out of history and process arguments.

```sh
set +x
umask 077
mkdir -p ~/.config/housesitter/secrets
chmod 700 ~/.config/housesitter/secrets
read -r -s -p 'UniFi password: ' unifi_password
printf '\n'
touch ~/.config/housesitter/secrets/unifi_password
chmod 600 ~/.config/housesitter/secrets/unifi_password
printf '%s' "$unifi_password" > ~/.config/housesitter/secrets/unifi_password
unset unifi_password
```

There is one secret per file. The file must belong to the run-as account,
with mode 600, in the mode 700 directory. Never put the password in config,
a command argument, or a message to Muse.

### 6. Check setup and connectivity

In the same run-as shell:

```sh
housesitter config-check
```

A successful check prints:

```text
config ok: ~/.config/housesitter/config.json
unifi_wifi: enabled
```

This validates config, without contacting UniFi. Next run:

```sh
housesitter doctor
housesitter doctor --check
```

The first reports local setup. The second also logs in to UniFi, reads the
site's `stat/sysinfo`, and attempts logout. It reads no devices or clients
and saves nothing. In the JSON report, `data.problems` counts findings;
zero means none were found. Read the individual findings even when the
command returns a report successfully. Each entry in `data.modules` has a
`problem`: `null` means no module problem was reported; a code such as
`upstream_auth` identifies a failed check. A successful check's `logout ok`
message means logout was attempted, not confirmed.

### 7. Make the commands available to Muse

The installer already ran
`housesitter install-commands --exec /opt/housesitter/bin/housesitter`.
It writes root-owned mode 644 JSON drop-ins to
`/etc/musegadget/commands.d/`. Preview the definitions without writing:

```sh
housesitter dropins --print --exec /opt/housesitter/bin/housesitter
```

Leave the run-as shell with `exit` if you opened it above. From your own
sudo-capable account, restart the SDK to load the commands:

**Warning: restarting musegadget kills any command Muse is running at that moment.**

```sh
sudo systemctl restart musegadget
```

The installer does not restart it unless you explicitly pass
`--restart-musegadget`; the same warning applies to that option.

After installation and restart, check that the SDK loaded the commands:

```sh
sudo journalctl -u musegadget | grep commands
```

Look for a line like
`commands from /etc/musegadget/commands.d: housesitter.describe, ...`.

### 8. Collect a first snapshot

From your own account, run the collection as the run-as account:

```sh
sudo -u RUN_AS_USER -H housesitter invoke housesitter.unifi_wifi.snapshot --params '{"label":"first"}'
```

This contacts UniFi, returns measurements, and saves a snapshot under that
account's `~/.local/share/housesitter/`. Now ask Muse: “Call
`housesitter.unifi_wifi.snapshot` with label `first-from-muse`.” Both calls
use the same config and saved data.

### 9. Open the dashboard

Try the read-only server in the foreground first:

```sh
sudo -u RUN_AS_USER -H housesitter serve
```

Open `http://127.0.0.1:8080/` in a browser on the Pi. The dashboard shows saved
results; it never contacts UniFi or collects data. Ctrl-C stops the server.
It has no flags and no Muse drop-in command.

The default bind address is reachable only from the Pi. To view it from
other devices, edit the run-as account's config, adding or updating
`dashboard` inside `housesitter` while preserving the other settings:

```json
"dashboard": {
  "bind": "192.0.2.10",
  "port": 8080,
  "allowed_hosts": ["192.0.2.10", "housesitter-example"]
}
```

Replace the invented address with the Pi's LAN address, and list every name
or IP you will use in the browser in `allowed_hosts`. Run `config-check` as
the run-as account, restart the server, then open that address and port.
If Muse's earlier demo page uses port 8080, stop it or choose another port.

There is no login. **Anyone who can reach the port sees everything on the
dashboard, including device names, MACs, and IPs.** Choose an address only
trusted devices can reach.

If you installed with `--with-dashboard`, stop the foreground server, then
run these as your own sudo-capable account to start it now and at boot:

```sh
sudo systemctl enable --now housesitter-serve
systemctl status housesitter-serve
sudo journalctl -u housesitter-serve
```

The hardened unit runs as the run-as account, serves files read-only, and
has no access to the secrets directory. Restart it with
`sudo systemctl restart housesitter-serve` after changing the bind address,
port, or allowed hosts. See [dashboard behaviour](DASHBOARD.md) for details.

### Upgrading

From your own sudo-capable account, update main in the checkout, read its
installer, then preview and apply the upgrade:

```sh
git pull --ff-only
less install/install.sh
bash install/install.sh upgrade --dry-run
bash install/install.sh upgrade
sudo systemctl restart musegadget
```

Upgrade reinstalls from the checkout into `/opt/housesitter`, refreshes the
drop-ins, re-renders the dashboard unit, and restarts that unit if it was
running. Config, secrets, and saved data are preserved. The musegadget
restart has the command-killing warning above; you can instead explicitly
request it with the installer's `--restart-musegadget` option.

### Uninstalling

From your own sudo-capable account, in the checkout:

```sh
bash install/install.sh uninstall --dry-run
bash install/install.sh uninstall
sudo systemctl restart musegadget
```

This removes the dashboard unit, command drop-ins, symlink, and
`/opt/housesitter`. Config, secrets, and saved data remain. Restarting
musegadget removes its registered commands and kills any running command.
To also delete the run-as account's config, secrets, and saved data, preview
and run the purge instead:

```sh
bash install/install.sh uninstall --purge --run-as RUN_AS_USER --dry-run
bash install/install.sh uninstall --purge --run-as RUN_AS_USER
```

Purge asks for confirmation; `--yes` skips it. Delete the dedicated UniFi
account in the console when you no longer use Housesitter.

### Troubleshooting

- `config_invalid`: fix the named config field; check JSON syntax,
  `base_url`, and an explicit `unifi_os` or `classic` flavor.
- `secret_unreadable`: use the run-as account and check the secret's path,
  ownership, and mode 600, plus the directory's ownership and mode 700.
  Check with `ls -l` or `stat`; never print the file's contents.
- `upstream_tls`: check the console's certificate, trusted CA, or pin.
  Verify a changed certificate independently before updating a pin.
- `upstream_auth`: check the local username and password file, account
  access, and explicit flavor. Avoid repeated attempts that could lock it.
- `timeout`: check network reachability from the Pi, the URL's port, and
  whether the console responds. `request_timeout_s` allows 1–60 seconds.

