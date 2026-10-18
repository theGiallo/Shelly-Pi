# Hardware test checklist

For the first real run with the Pi and a plug. Go in order: each step needs the one before it.
When something fails, the **If not** column says what to collect. Paste it to me and we fix it.

## 0. If you used scripts/prepare-sd.sh

| Step | Expect | If not |
|---|---|---|
| The script runs to "The SD card is ready" | Image downloaded and checked, card written, files added | The whole output. Note any line starting with `!` |
| Boot the Pi on Ethernet, wait about 10 minutes | It reboots once by itself | `ssh root@<pi-address>` then `journalctl -u shelly-firstboot -b --no-pager` and `cat /var/lib/shelly-install/firstboot.log` |
| `ls /var/lib/shelly-install` | `done` exists, `firstboot.env` is gone | `systemctl status shelly-firstboot`; `ausearch -m avc -ts boot` (SELinux) |

Then continue at step 1 below, skipping the copy and setup lines.

## 1. System and containers

| Step | Expect | If not |
|---|---|---|
| Flash Fedora IoT, boot on Ethernet, SSH in | You get a shell | Check the image and key flags in the README |
| `nmcli device` | `wlan0` listed as `wifi` | `dmesg \| grep -i brcm` |
| Copy the project, `sudo bash deploy/setup-host.sh` | Ends with "Done" and a dashboard address | The whole output of the script |
| `systemctl status shelly-app shelly-dhcp` | Both `active (running)` | `journalctl -u shelly-app -n 80`, `journalctl -u shelly-dhcp -n 80` |
| `curl localhost/healthz` | `{"ok":true,...}` | as above |
| Open `http://<pi-address>/` from your PC | Login prompt, then the empty dashboard | `sudo firewall-cmd --list-all` |

## 2. The Pi's Wi-Fi

| Step | Expect | If not |
|---|---|---|
| Phone: look for the Wi-Fi name you chose | It is listed | `nmcli connection show shelly-ap`, `iw dev wlan0 info` |
| Join it with the password | Connects, gets `192.168.50.x` | `journalctl -u shelly-dhcp -n 50` |
| On the phone open `http://192.168.50.1/` | The dashboard asks for the password | `ss -ltnp \| grep :80` on the Pi |
| `cat /var/lib/shelly-monitor/dnsmasq/dnsmasq.leases` | Your phone is listed | same |

## 3. Hardware clock (power the Pi off to wire it)

| Step | Expect | If not |
|---|---|---|
| Pins: `+` on pin 1, check against pinout.xyz, then boot | Boots normally | Unplug at once if anything is hot |
| `cat /sys/class/rtc/rtc0/name` | Something like `rtc-ds1307 1-0068` | `dmesg \| grep -i -E "rtc\|ds1307\|i2c"`, `grep -n -i -E "i2c\|rtc" /boot/efi/config.txt` |
| Dashboard: a "Clock not set" chip is shown | Yes, the first time | |
| Settings, **Set the Pi's clock** | Chip disappears, Pi time matches your PC | `journalctl -u shelly-settime -n 20` |
| `sudo hwclock -r` | The right time | |
| Unplug Ethernet, reboot, wait a minute, `date` | Still the right time, no warning on the dashboard | `systemctl status rtc-load` |

## 4. Adding a plug

Step 1 on the page should show your plug's own network once it has been plugged in for a minute (`sudo iw dev wlan0 scan ap-force | grep -i 'SSID: Shelly'` on the Pi shows what the Pi itself hears). If the page says the Pi couldn't scan, send me that message and `journalctl -u shelly-wifiscan -n 20`. With two plugs freshly plugged in you should get the choice list, strongest first.


Write down what you see; the unknowns are marked with **?**.

| Step | Expect | If not |
|---|---|---|
| Plug the new plug in; phone sees `ShellyPlugMG3-XXXXXX` | Yes | Hold the plug's button per Shelly's reset instructions |
| Join it, scan the QR from the Add plug page | The phone opens the link and shows a JSON reply. **? Note the reply**, e.g. `{"restart_required": ...}` | Open `http://192.168.33.1/` on the phone and enter the Wi-Fi details by hand (the fallback in step 1 of the wizard) |
| Wizard step 2 | The plug appears within about a minute. **? Did it need the restart code?** | `cat .../dnsmasq.leases`; "I know the plug's address" |
| Name it, **Add plug** | All steps green (the plug restarts once) | The failed step's message, plus `journalctl -u shelly-app -n 80` |
| Dashboard | The plug shows its real power, voltage, temperature | `curl -s http://<plug-address>/rpc/Shelly.GetStatus` (paste it) |

Then plug in something with a known load (a lamp or kettle) and compare with the plug's own
page at `http://<plug-address>/`.

## 5. Behaviour

| Step | Expect | If not |
|---|---|---|
| Switch the plug off and on from the dashboard | The plug clicks, the row follows within a second | |
| Press the plug's own button | The dashboard follows within ~10 s | |
| Pull the plug's power for a minute and restore it | Row goes "not responding", then recovers on its own | |
| `ls /var/lib/shelly-monitor/dhcp-hosts.d` | One `.conf` file per plug | |
| Reboot the Pi | Plugs reconnect by themselves, same addresses, a gap in the chart | `journalctl -u shelly-dhcp -b` |
| Cut the router's internet (or Ethernet) | Plugs keep logging; the dashboard works from the plugs' Wi-Fi | |
| Leave it for an hour, then check Settings, "Stored data" | Readings counted, no "last save failed" | |
| Open it on your phone | Layout is usable | A screenshot |

## 6. Electricity cost

| Step | Expect | If not |
|---|---|---|
| Settings, Electricity cost: tick it, set currency, time zone and a price, add a slot | Save works; "Week at a glance" shows your slot in the right hours | The message shown when saving |
| Dashboard, Cost button | Bars per period; the plug rows show "x Wh · y €" | Screenshot |
| Plug a known load for an hour and compare: kWh x price | The dashboard cost matches within a few percent | The numbers you see |
| Change a slot to include the current time | "Price now" on the dashboard changes within a minute | |

## 7. Backup and restore

| Step | Expect | If not |
|---|---|---|
| Settings, **Download a backup** | A file named `shelly-monitor_<date>_<time>.zip` with your local time | Screenshot of the file name |
| Change a plug's name, then restore that file | Old name is back, plugs keep reporting, charts intact | `journalctl -u shelly-app -n 80` |
| `ls /var/lib/shelly-monitor/pre-restore` | The data from just before the restore | |
| Try restoring some other zip | A clear refusal, nothing changed | |

### 7b. Automatic backups to a USB drive

Needs a USB stick (exFAT, FAT32, NTFS or ext4) with nothing you can't lose on it. Run `setup-host.sh` again first if this Pi was set up with an earlier version.

| Step | Expect | If not |
|---|---|---|
| Settings, **Automatic backups**, plug the stick in, **Look again** | Your stick is listed with its name, size and format | `systemctl status shelly-usb.path`; `sudo /usr/local/libexec/shelly-monitor/usb.sh` after `echo "x list" \| sudo tee /var/lib/shelly-monitor/bridge/usb.req`; then `cat /var/lib/shelly-monitor/bridge/usb.res.json` |
| Choose it, leave 24 hours / 0:00, switch on, **Save**, then **Back up now** | "Done. You can unplug the drive now"; on a computer the stick has a `shelly-monitor-backups` folder with the zip, and your other files are untouched | The red text under **Last backup** and `journalctl -u shelly-usb -n 40` |
| `findmnt /run/shelly-usb` right after | Nothing: the stick is not left mounted | |
| Unplug the stick, **Back up now** | A clear "The drive isn't connected" | |
| Set the interval to every hour, keep 2, press **Back up now** three times | Only the 2 newest files remain; any file you put in that folder yourself stays | |
| Switch the Pi off across a scheduled time, plug the stick in, switch on | A backup appears within a minute or two of the Pi being up | |
| Unplug the stick during the idle time between backups, plug it into a computer | No repair prompt, no corruption | |

## 8. HTTPS

| Step | Expect | If not |
|---|---|---|
| Settings, Secure connection | Status "On", an address, a certificate valid about 13 months, the names it covers | The message in the yellow box; `journalctl -u shelly-app -n 50` |
| Download the authority, trust it on your PC and phone, open `https://shellypi.local/` | Padlock, no warning | The browser's error text and which device |
| Tick "Send plain HTTP visitors to HTTPS", open `http://<pi-address>/` | It jumps to https | |
| Change the Pi's address (new DHCP lease) and wait 5 minutes | Settings lists the new address among the covered names | |
| `ls /var/lib/shelly-monitor/tls` | `ca.crt ca.key server.crt server.key` | |

## 9. What to send me if something is off

```
curl -s http://<plug-address>/rpc/Shelly.GetDeviceInfo
curl -s http://<plug-address>/rpc/Shelly.GetStatus
curl -s "http://<plug-address>/rpc/Switch.GetConfig?id=0"
curl -s http://<plug-address>/rpc/WiFi.GetConfig
journalctl -u shelly-app -n 100 --no-pager
journalctl -u shelly-dhcp -n 50 --no-pager
nmcli connection show shelly-ap
rpm-ostree status
```

Plug status output contains your Wi-Fi name and the plug's MAC address. Remove them if you like.
