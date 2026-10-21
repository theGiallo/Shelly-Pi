# Shelly-Pi

Shelly-Pi is a small server for a Raspberry Pi 4 that creates its own Wi-Fi network for Shelly Gen2+
smart plugs (built for the **Plug M Gen3**), shows their power on a web dashboard, and
switches them on and off. Everything stays on your local network. Nothing needs internet.

```
 PC / phone ──── router ──── Ethernet ──┐
                                        │   Raspberry Pi 4  (Fedora IoT)
 UPS ─── Pi power supply                │     ├─ Wi-Fi access point "ShellyPi"   (NetworkManager)
                                        │     ├─ shelly-dhcp  container: dnsmasq, DHCP only
 Shelly plugs ── Wi-Fi "ShellyPi" ──────┘     ├─ shelly-app   container: dashboard + API + SQLite
                                              └─ DS3231 clock module (keeps time without internet)
```

## What you get

- **Dashboard**: total power, one row per plug with live watts, a switch, energy today and a
  history chart (1 h to 30 days).
- **Add-a-plug wizard**: scan one QR code on your phone and the plug joins the Pi's Wi-Fi; the
  Pi finds it, you name it, and it turns Shelly Cloud off for you.
- **Cost**: give energy a price per kWh, optionally with time slots (night, weekend, F1/F2/F3 and so on),
  and pick your currency. The dashboard then shows cost today and over the chart range, the price
  right now, and a Cost chart next to Power and Energy. Set it up under Settings, Electricity cost.
- **Written for an SD card**: readings are buffered in RAM and saved in one batch every 5
  minutes. Detail is kept 7 days, minute averages 90 days, 15-minute averages for good.
- **Works without internet**: all timestamps come from the Pi. With the DS3231 module the time
  survives power cuts. If the clock is ever wrong, the Pi holds readings in memory and saves
  them with the right times once you set the clock (button on the Settings page).

## Try it on your PC first (5 minutes)

No Pi needed. It runs simulated plugs with three days of made-up history.

```
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/demo.py                            # then open http://127.0.0.1:8080
```

Click through the dashboard, the Add plug page (use "I know the plug's address" with
`127.0.0.1:8104` after running `python tests/fake_shelly.py --port 8104 --mac AABBCCDD0004`)
and Settings. If anything looks wrong on your screen, tell me what you see.

## Install from Windows (one click)

`install.cmd` does everything from a Windows 10 or 11 PC. No WSL, no extra tools to install first.

**You need:** the Raspberry Pi 4 and its power supply, a microSD card of 16 GB or more in a card reader
plugged into the PC, and an Ethernet cable from the Pi to your router. No screen or keyboard is needed. The PC
needs internet for about 1.2 GB of downloads. The Pi itself does not.

**Steps**

1. Get this folder onto your PC (`git clone https://github.com/theGiallo/Shelly-Pi.git` or download the ZIP).
2. Double-click `install.cmd`. When Windows asks for administrator rights, say yes; only the card-writing
   window uses them.
3. It downloads the newest Fedora IoT image for the Pi and checks it against Fedora's checksum. If 7-Zip isn't
   installed, it installs it with winget (or fetches the official `7zr.exe` and checks its signature).
4. In the administrator window, pick the SD card from the list and type its number to confirm.
   **The card is erased.** Your system disk is never offered. Writing takes 5 to 20 minutes.
5. Still in that window, the installer adds your PC's SSH key to the card's `root` account (Fedora's image has no
   user and no password, so there would be no way in otherwise). Windows can't write the card's Linux partition,
   so it copies that partition into a temporary file, changes it there with `debugfs` (from e2fsprogs, built and
   tested by this project's CI and attached to each release), checks the file system and writes back only the
   blocks that changed. It needs about 6 GB of free space in your temp folder for a few minutes. Windows may offer
   to format drives on the card afterwards; click Cancel, those are Linux partitions.
6. Put the card in the Pi, connect Ethernet and power, and wait about two minutes.
7. Press Enter in the install window. The script finds the Pi, grows the root partition to fill the card, copies
   the project, downloads the container image from the newest
   [release](https://github.com/theGiallo/Shelly-Pi/releases) and runs `deploy/setup-host.sh` with generated
   passwords. It saves them in `shelly-pi-credentials_<date>.txt` and prints the dashboard address.

If adding the key fails, the card is still good and the installer says so: connect a screen and keyboard, create
a user when Fedora offers to, and enter its name when asked.

**Options** (put them after `install.cmd`, or use the PowerShell script directly with
`powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1`):

| Option | Meaning |
| --- | --- |
| `-Image FILE` | Use a Fedora IoT `.raw.xz` you already have instead of downloading one. |
| `-ImageUrl ADDRESS` | Download the image from this address (copy it from fedoraproject.org/iot/download) if finding it automatically fails. |
| `-SkipSd` | The Pi already runs Fedora IoT; only install Shelly-Pi on it. |
| `-PiHost 192.168.1.50` | The Pi's address, if it can't be found by name. |
| `-User name` | The Pi user (default: the name you type at the prompt, or `root`). |
| `-Ssid`, `-ApPassword`, `-AdminPassword` | The plugs' Wi-Fi name and password, the dashboard password. |
| `-NoRtc` | No DS3231 clock module is connected. |
| `-BuildOnPi` | Let the Pi build the container image (needs internet on the Pi). |

`Get-Help .\scripts\install-windows.ps1 -Full` and `Get-Help .\scripts\install.ps1 -Full` list everything.

**If something goes wrong**

- *The Pi shows nothing*: use the HDMI port next to the USB-C power socket (HDMI0). The plain Fedora IoT image does
  boot on a Pi 4, with U-Boot already on its boot partition. A green light that never blinks usually means the
  card isn't seated; push it in until it clicks.
- *"Couldn't find a Fedora IoT aarch64 image"*: open <https://fedoraproject.org/iot/download>, copy the address of the aarch64
  raw image (it ends in `.aarch64.raw.xz`) and run `install.cmd -ImageUrl <address>`, or download it and use `-Image FILE`.
- *The Pi isn't found*: look for it in your router's device list and use `-PiHost`. SSH only works after the
  first boot has finished (give it two minutes). The Pi's name is `localhost` until Shelly-Pi renames it, so
  the installer usually has to ask for the address.
- *Login fails with "host identification has changed"*: the card was written again; run `ssh-keygen -R <address>`.
- *The image download from GitHub fails*: the script falls back to letting the Pi build it, which needs internet
  on the Pi. Or download `shelly-pi-image.tar` from the release page and put it in this folder.

These scripts were written without access to Windows or a Pi, so expect rough edges the first time; the
checks that can run elsewhere (script syntax, the settings they pass to `setup-host.sh`) are in the tests.

## Install on the Pi

### Fastest way: one script prepares the SD card

On a Fedora PC (or a Fedora live USB), with the SD card inserted, run as root:

```
sudo dnf install arm-image-installer podman qemu-user-static
lsblk                                   # find the card, for example /dev/sdb
sudo bash scripts/prepare-sd.sh --media /dev/sdb
```

It downloads the newest Fedora IoT **raw** aarch64 image for release 44 and checks it (or uses
`--image FILE`), builds the app's container image for the Pi (or uses `--app-image FILE`), writes the card
with `arm-image-installer` (your SSH key is added for root, root has no password), and copies the project
plus your answers onto it. On its first boot the Pi runs `deploy/setup-host.sh` by itself, creates the Wi-Fi
access point, starts both containers and reboots once. No internet is needed on the Pi. Wi-Fi name, host
name, channel and so on are options (`--help`); passwords are generated and saved in `sd-credentials_<date>.txt`
unless you give them. Everything on the card is erased: it asks you to type the device name first.

Choose the **raw** `.raw.xz` image, not the ISO. Use a current release: each Fedora release gets security
updates for about 13 months, and old release folders on mirrors are no longer maintained. After the first
boot, `ssh root@shellypi.local journalctl -fu shelly-firstboot` shows the progress, and
`/var/lib/shelly-install/firstboot.log` the result. If it failed, fix the cause and run
`systemctl start shelly-firstboot`.

The manual route below does the same by hand.

### 1. Put Fedora IoT on the SD card (manual)

Fedora IoT is an image-based OS: you don't install packages on it, you update the whole image
(`rpm-ostree upgrade`, then reboot) and can roll back (`rpm-ostree rollback`). Apps run as
containers, which is how this project is packaged.

On a Fedora machine, `arm-image-installer` writes the image and can add your SSH key:

```
sudo dnf install arm-image-installer
sudo arm-image-installer --image=Fedora-IoT-<version>.aarch64.raw.xz --target=rpi4 \
     --media=/dev/sdX --addkey=$HOME/.ssh/id_ed25519.pub --resizefs --norootpass
```

Get the image from <https://fedoraproject.org/iot/>. Flags can change between releases, so
check `arm-image-installer --help` and the current Fedora IoT documentation. On Windows or macOS,
the Fedora docs describe other ways to write the image.

Boot the Pi on Ethernet, find its address in your router's device list, and log in over SSH.

### 2. Copy the project and run setup

**On Windows, the installer does this step for you; see "Install from Windows" above.**

By hand, from any PC:

From your PC:

```
scp -r Shelly-Pi root@<pi-address>:/root/
```

The container image has to be built once, which needs internet. Pick one:

- **Build on the Pi**: plug your phone-tethering adapter into the router for a few minutes.
  `setup-host.sh` builds the image itself if it doesn't find one.
- **Build on your PC**: `bash scripts/build-image.sh` writes `shelly-pi-image.tar` (needs podman
  or Docker with arm64 emulation). Copy it into the project folder on the Pi and the script
  loads it, so the Pi never needs internet for this step.

Files copied from Windows lose their executable bit, so the scripts are run with `bash`. Don't
save them in an editor that converts line endings to Windows style.

Then on the Pi:

```
cd /root/Shelly-Pi
sudo bash deploy/setup-host.sh
```

It asks for the Wi-Fi name and password for the plugs and a dashboard password (press Enter to
generate them), creates the access point, opens the firewall, installs the two containers and
prints the addresses. Safe to run again. `--help` lists the options.

### 3. The DS3231 clock module

With the Pi **powered off**, plug the module onto the header so that its `+` pin is on pin 1
(3.3 V, the corner pin nearest the SD card slot). Its five pins are `+ D C NC -` for 3.3 V,
SDA, SCL, unused and ground, i.e. pins 1, 3, 5, 7 and 9 of the odd-numbered row. Check against
<https://pinout.xyz> before powering on: a module one pin out of place connects 5 V to the
wrong pin. If the module doesn't fit your case or the pins differ from the photo, use jumper
wires with the same mapping.

The setup script adds `dtparam=i2c_arm=on` and `dtoverlay=i2c-rtc,ds3231` to the Pi's
`config.txt` and installs a service that copies the module's time into the system at boot.
After the reboot it should show up as `cat /sys/class/rtc/rtc0/name` (a `ds1307` driver name is
normal: that driver handles the DS3231). Then open **Settings** and press **Set the Pi's
clock**. That also writes the module, so you only do it once, and again if its battery is
replaced.

### 4. Add your plugs

Open the dashboard (`http://<pi-address>/`, user `admin`), go to **Add plug** and follow the
steps. A new Plug M Gen3 starts its own Wi-Fi (`ShellyPlugMG3-XXXXXX`); you join it with your
phone and scan the code, which tells the plug to join the Pi's network.

## Day to day

- **From your PC**: `http://<pi-address>/` or `http://shellypi.local/`. **From the plugs'
  Wi-Fi** (for example a phone joined to it): `http://192.168.50.1/`.
- **Power cuts**: put the Pi and your router on the UPS you already have. Plugs don't need the
  router, but your PC does, to reach the dashboard. Don't power the Pi, router or UPS through
  a plug that the dashboard can switch off.
- **Chart styles**: **Power** (watts), **Energy** (Wh or kWh per period) and **Cost** (once prices are set; until then a *Set up cost* link goes to the price settings). **Instant** shows each moment or period, **Cumulative** a running total from the start of the range (it applies to Energy and Cost; choosing it on Power switches to Energy). Every chart says its unit in its corner. **All plugs together** above the rows stacks the plugs on top of each other in their own colours and follows the same choices.
- **Backup and restore**: Settings, **Backup and restore** (see below).
- **Settings** you may want to change live in `/var/lib/shelly-pi/config.json`
  (`poll_interval` 10 s, `flush_interval` 300 s, `raw_retention_days` 7, `r1m_retention_days` 90),
  then `sudo systemctl restart shelly-app`.

### Updating

When you plug in internet:

```
sudo rpm-ostree upgrade && sudo systemctl reboot      # the whole OS, as one image
sudo rpm-ostree rollback && sudo systemctl reboot     # if the new image misbehaves
```

Containers and your data live outside the OS image, so OS updates don't touch them.

**Updating the app from the dashboard.** Settings, **Update the app**. The Pi has no internet, so a device that
does have some does the downloading: "Look for a new version" asks GitHub which release is newest, "Download
the update" saves `shelly-pi-image_v<version>.tar` (about 190 MB) on that device, and you pick that file and send it to the
Pi. The Pi checks the file against the release's SHA-256 checksum, loads it and restarts the app; your data is
kept. If the new version doesn't answer within 90 seconds, the previous image is put back. (The page asks
GitHub's API from your browser, which is why the page's security policy allows `api.github.com`.) The page can
only offer an update that was published as a GitHub release, and tells a newer release from the running one
by the version number, so bump `__version__` in `app/__init__.py` before publishing a release. Pis installed
before this feature need one run of `sudo bash deploy/setup-host.sh` (or the installer again) to get the
helper that installs the update.

From a command line, build a new image (`bash scripts/build-image.sh` or `sudo bash deploy/setup-host.sh
--rebuild`) and run `sudo systemctl restart shelly-app shelly-dhcp`.

### HTTPS

Settings, **Secure connection (HTTPS)**. The Pi runs its own small certificate authority, so HTTPS needs no
internet, no domain name and no yearly renewal job:

1. On first start (once the clock is right) the Pi creates a certificate authority, valid 10 years, and a
   server certificate for `<hostname>.local`, `<hostname>`, `localhost` and the Pi's own addresses. The
   server certificate lasts about 13 months and is replaced automatically when it gets close to expiring or
   when the Pi's network address changes (checked every few minutes).
2. On each device you use, download the authority from Settings and trust it once (steps for Windows,
   macOS, iPhone, Android, Linux and Firefox are on that page). Compare the fingerprint shown there.
3. Open `https://shellypi.local/`. When every device you use trusts it, tick **Send plain HTTP visitors to
   HTTPS**. The health check and the certificate download always stay on plain HTTP.

Things to know:
- HTTP on port 80 stays available until you tick that box, because devices need it to fetch the authority.
- The authority is limited to `.local` names, the Pi's host name, `localhost` and private addresses, and its
  key stays on the Pi (`/var/lib/shelly-pi/tls`). Neither it nor the certificates are in backups, so a
  new Pi or card makes a new authority and your devices need the new one. Changing the Pi's host name also
  needs a new authority ("Start over" on that page).
- `.local` names need mDNS on your PC or phone (normal on macOS, iOS, Android and Windows 10+, and on Linux
  with Avahi). Otherwise use the Pi's address, which is also covered.
- A certificate is only made while the clock is trusted. If the clock is wrong, an existing certificate keeps
  being served. The port is `https_port` in `config.json` (443; 0 turns the feature off).
- Not tested against real browsers and phones yet, only against OpenSSL, Python and Node.

### Plugs on your router's Wi-Fi instead

By default the plugs join the Wi-Fi the Pi hosts. If you would rather they join a network of your router (for
example because the Pi has no Wi-Fi, or the router already covers the house), open Settings, **Wi-Fi for the
plugs**, choose the router option and enter the router's Wi-Fi name and password. The Pi must be on that network
too, by cable. The Add plug page then sends the router's details to the new plug, and the Pi finds the plug there:

- First by mDNS, which Shelly plugs use to announce themselves. This needs nothing from the router.
- If nothing shows up after about ten seconds, a button scans every address on the network (up to a /22, a
  minute at most). You can also type a plug's address.
- A router may give the plug a different address one day. The Pi then looks for the plug by its name
  (`<id>.local`) and carries on. Reserving the plug's address in the router avoids the question.

Plugs you already added keep working, and you can switch back any time. The Pi's own Wi-Fi keeps running while
the router option is chosen. The router's Wi-Fi password is kept on the Pi in `config.json` and is not part of
backups.

**Pis without Wi-Fi.** The setup script notices a Pi with no Wi-Fi, skips the access point and the DHCP
server, and starts in router mode; you then enter the router's details in Settings. Fedora IoT needs a 64-bit
Pi: according to Fedora's Raspberry Pi page the 3 series and the Compute Module 3 work, and the Pi 2 only if it
is the 64-bit version 1.2; the Pi Zero and Pi 1 do not. Only the Pi 4 has been tried so far.

### Shutting the Pi down

Settings, **Power**: "Shut down the Pi" and "Restart the Pi", each asking for confirmation first. Shut down
before unplugging the Pi or adding hardware such as the clock module; the green light stops blinking after
about 20 seconds, and then it is safe to unplug. (Same as `sudo systemctl poweroff` over SSH.)

### Backup and restore

Settings, **Backup and restore**, downloads one zip named like `shelly-pi_2026-10-17_03-53-00.zip`
(year-month-day_hour-minute-second, so a folder of backups sorts by date; the time is the Pi's clock in
the time zone of the device you download from). It holds the whole database (plugs, all readings and
rollups, electricity prices), the address each plug is pinned to, the polling and storage settings, and a
small manifest. The dashboard and Wi-Fi passwords are left out, so the file is safe to keep around.

To restore, choose the zip on the same page and confirm. The Pi checks the archive (a damaged or foreign
file is refused and nothing is changed), copies the current data to `/var/lib/shelly-pi/pre-restore`,
swaps the data in and carries on polling without a restart.

- A restore replaces the data, the plugs' address reservations and the settings that tune polling and
  storage. It keeps this Pi's own Wi-Fi name and password, dashboard password and network address, because
  those belong to the machine and are set by `setup-host.sh`.
- **Moving to a new Pi or SD card**: run `setup-host.sh` there first (use the same Wi-Fi name and password so
  the plugs reconnect by themselves), then restore the backup.
- **Copy it off the SD card**: the backup only protects you if it lives somewhere else.
- Readings that haven't been saved yet (up to 5 minutes' worth) are saved first, so they're included. If the
  clock is "not set", held readings can't be and are left out; the manifest says how many.
- Backups from a newer version of the program are refused; older ones are accepted.
- Changes to the API (for example from a script) need an `X-Requested-With` header with any value; plain
  GET requests such as the backup download don't. This stops other websites from changing things through
  your browser's saved login.

### Adding a plug: joining its Wi-Fi by scanning

The **Add plug** page starts by looking for the plug's own setup network (named like `ShellyPlugMG3-A1B2C3`).
The Pi scans for it through a small root helper (`/usr/local/libexec/shelly-pi/wifiscan.sh`, started by
`shelly-wifiscan.path`, installed by `setup-host.sh`), keeps the networks whose names look like a Shelly's, and
shows one QR code that makes a phone join the strongest one. If it finds more than one it says so, selects the
strongest, and lists the others (with their signal, and "already added as ..." for plugs you already have);
choosing another changes the code. The next step's code then tells the plug to join the Pi's network, as before.

- Scanning while the Pi hosts its own network works on most Pi Wi-Fi chips but not every one, and a scan can
  make the plugs' connections stutter for a moment, so the page scans every 8 seconds and only while open.
  If your Pi can't, the page says why and you join the plug's network by hand as before. A second USB Wi-Fi
  adapter fixes it: add `SM_SCAN_IFACE=wlan1` (its name) to `/etc/shelly-pi/env`.
- It needs `iw` on the Pi (it falls back to `nmcli`, which may refuse while hosting). `setup-host.sh` warns if
  `iw` is missing.
- Only an open network gets a code; a plug network with a password is shown with a note to join by hand.
- Try it without a Pi: `python scripts/demo.py --aps 0|1|2|3` pretends to find that many plug networks.

### Automatic backups to a USB drive

Settings, **Automatic backups to a USB drive**. Plug a USB stick or disk into the Pi, choose it from the list
of drives that are plugged in, switch the toggle on and save. Defaults: every 24 hours at 0:00 (the Pi's clock
in the time zone shown there), keeping the newest 14. You can pick another interval (1, 2, 3, 4, 6, 8, 12, 24,
48 or 72 hours, or weekly) and any hour. **Back up now** makes one immediately and tells you when you can
unplug the drive.

- **The drive is never formatted or changed.** Backups, the same zip as the download, go into a folder
  `shelly-pi-backups` on it. Only files named `shelly-pi_<date>_<time>.zip` in that folder are
  ever deleted (the oldest, beyond the number to keep). It needs a filesystem the Pi can write: exFAT,
  FAT32, NTFS or ext4. Other drives are listed greyed out.
- The app runs in a container that can't see the Pi's drives, so a small root script on the Pi
  (`/usr/local/libexec/shelly-pi/usb.sh`, started by `shelly-usb.path`, installed by `setup-host.sh`)
  lists the USB drives and copies the finished backup. It mounts the drive only for the copy and
  unmounts it again, so the stick can be pulled out safely between backups. It only offers USB drives,
  never the SD card, and only copies files with a backup's name.
- If the Pi was off at the scheduled time, or the drive wasn't plugged in, the backup is made as soon as
  both are true. A failure shows its reason on the page and is retried every ten minutes. Nothing is made
  while the clock is "not set".
- The choice (drive, interval, hour, number to keep) is part of the normal backup, so restoring on a new SD
  card brings it back; plug the same stick in and it carries on. The log of the last attempt is not.
- If you installed before this feature existed, run `setup-host.sh` again (it keeps your settings) so the
  helper is installed; until then the page says the Pi didn't answer.

### Electricity prices

Settings, **Electricity cost**: tick *Show costs*, choose the currency (any text up to 8
characters, shown before or after the amount), the time zone, the base price per kWh and, if your
tariff has them, time slots. A slot has a name, a price, the days it applies to and a time window
on a 15-minute step. Rules:

- Times not covered by a slot use the base price. If slots overlap, the higher one in the list wins.
- A window that ends before it starts runs past midnight into the next morning, so 23:00 to 07:00
  ticked for Mon to Fri covers the mornings of Tue to Sat. Add a separate slot (or tick Sunday) for
  the Monday morning if it should be cheap too. Start equal to end means the whole day.
- The *Week at a glance* strip shows what the Pi will actually apply. Check it after saving.
- Public holidays aren't known to the Pi. Add them to a slot's days only by hand-editing, which isn't
  supported, so for tariffs where holidays are priced like Sundays costs will be a little off.
- Costs are worked out from the stored readings with today's prices. Changing a price changes the
  cost shown for the past too, and there's no price history. Energy is priced in 15-minute blocks.
- Use the price you actually pay per kWh, taxes and fixed charges included if you want them in.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Plugs' Wi-Fi isn't visible | `nmcli connection show shelly-ap`, `journalctl -u NetworkManager -n 50`. Pick a different `--channel` if the router uses the same one. |
| Phone joins the Wi-Fi but gets no address | `journalctl -u shelly-dhcp -n 50` |
| Dashboard won't load | `journalctl -u shelly-app -n 50`, `curl localhost/healthz` |
| Plug never shows up in the wizard | `cat /var/lib/shelly-pi/dnsmasq/dnsmasq.leases`: if the plug is listed but not shown, open its address with "I know the plug's address". |
| Clock warning chip | Settings, **Set the Pi's clock** |
| "The Pi did not apply the new time" | `systemctl status shelly-settime.path`, `journalctl -u shelly-settime -n 20` |
| No `/dev/rtc0` | wiring and `config.txt`; `dmesg | grep -i -E "rtc|ds1307|i2c"` |

## Security

The dashboard uses one shared password over plain HTTP (user `admin`). That is fine on a home
network but not on a hostile one. The Wi-Fi password is shown to anyone who can log in. The plug
setup hotspot is open until you turn it off, which the wizard does by default.

## What has been checked, and what hasn't

Tested here: 140 automated tests (storage, rollups, clock handling, prices and costs, backup and restore, automatic USB backups,  the certificate authority and TLS, the SD card preparation script's file handling, wizard against simulated
plugs, auth, live updates) and the real pages loaded in a simulated browser (jsdom).

**Not yet tested on real hardware**, so expect to fix things on Sunday: the container build and
Quadlet units on Fedora IoT, the Wi-Fi access point on the Pi 4, the DS3231 overlay on Fedora,
and a real Plug M Gen3 (the simulated plug follows Shelly's public API docs; real firmware may
differ, for example in when it asks for a restart). The pages have only been rendered in jsdom,
never in a real browser, so the layout is unseen. `TESTING.md` is the checklist for Sunday.

The Windows scripts have been run by the author on one PC, step by step, with fixes after each run. What is
confirmed: finding and downloading the Fedora image, writing the card, and booting it on a Pi 4. CI runs the key step
(`scripts/ext4-inject.ps1` with `debugfs`) on a Windows machine against a virtual disk. Not yet confirmed on a real
card: adding the key, growing the root partition, and the rest of the install over SSH.

The SD card script and the first-boot service have only been tried against a fake file tree: the real
download page layout, `arm-image-installer`, the partition lookup, the SELinux labels on the copied files and
the first-boot run are untested.

Not built yet: Bluetooth setup of plugs (the QR flow replaces it), plugs with a password set,
exporting data as CSV.

## Layout

```
app/            the server (FastAPI) and the web pages in app/static
deploy/         setup-host.sh, Quadlet units, clock helper, udev rule
install.cmd      one click for Windows: Fedora IoT to the SD card, then Shelly-Pi onto the Pi
scripts/        demo.py (try it on your PC), build-image.sh, prepare-sd.sh (Linux: prepare the SD card),
                install-windows.ps1, flash-sd.ps1, ext4-inject.ps1, install.ps1 (the Windows installer)
tests/          pytest suite and a simulated Shelly Gen3 plug
Containerfile   one image for both containers
```

## License

MIT, see [LICENSE](LICENSE). The bundled uPlot chart library (`app/static/vendor/`) is MIT-licensed too; its own licence file sits next to it.
