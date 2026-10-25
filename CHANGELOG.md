# Changelog

## Unreleased

- Switching a plug on or off from the dashboard now asks first ("Switch off My PC?"). Each plug has an **Ask before switching this plug** setting in its edit dialog, on by default, for things like a lamp where you'd rather not be asked.
- Adding a plug: the page now says that the plug's reply (`restart_required: false`) is normal, and reminds you to put your phone back on the Pi's Wi-Fi (or your router's) before waiting for the plug to appear.

## 0.1.7

- The update file is now compressed (`shelly-pi-image_v<version>.tar.gz`, about a third of the size). The Update page unpacks it in your browser and sends the plain image to the Pi, and the Windows installer unpacks it on your PC, so the Pi never has to. A plain `.tar` still works.

## 0.1.6

- Setting the Pi's clock is now exact to a fraction of a second: the browser sends its time to the millisecond and the Pi adds the moment it took to get there, instead of ending up a second or two behind.
- The hardware clock is found by itself: the installer always switches on support for a DS3231 module, and Settings shows a **Hardware clock** row saying whether the Pi sees one. No on/off switch needed; plug the module in with the Pi powered off.
- Power: the confirmation now appears right under the buttons, and the page tells you when the Pi's helper isn't installed and nothing was done (instead of saying it is shutting down). An old request can no longer fire later by surprise.
- The update file you download is now named with the version, like `shelly-pi-image_v0.1.6.tar`. The Update page and the Windows installer find it by that name. The plain `shelly-pi-image.tar` stays on the release for now, because the Update page of version 0.1.5 and earlier looks for it.

## 0.1.5

- Backup files now have the app version at the end of the name, like `shelly-pi_2026-10-17_03-53-00_v0.1.5.zip`. Older names still work, and the USB drive cleanup counts both.
- Settings: **Power**, with buttons to shut down or restart the Pi, each asking for confirmation.
- Settings: the plugs can join a Wi-Fi network of your router instead of the Pi's own. The Add plug page finds them by mDNS, with a button that scans the network and the option to type an address.
- Pis without Wi-Fi: the setup script skips the access point and the DHCP server and starts in router mode.

## 0.1.4

- Settings has an "About, license and changelog" button.

## 0.1.3

- The phone QR code for the dashboard can carry the dashboard password, so the phone opens the page signed in. It is off by default.

## 0.1.2

- Only the version number changed. It was published to test updating from the dashboard.

## 0.1.1

- Settings: QR codes to join the Pi's Wi-Fi and to open the dashboard from a phone.
- Settings: **Update the app**. Download the new release on a device with internet, send it to the Pi, and it installs it and goes back to the old version by itself if the new one doesn't start.
- HTTPS: the certificate is now made for the Pi's real name (`shellypi.local`) and not for `localhost`.
- `shellypi.local` now resolves from the home network (mDNS is opened in the firewall).
- Installer: loads the image it brings even when an older one is already on the Pi.
- Installer: packing the project no longer fails when GNU tar is on the PC's path.

## 0.1.0

- First release: the dashboard for Shelly Plug M Gen3 plugs, the Wi-Fi the Pi hosts for them, history and costs, backups, HTTPS with the Pi's own certificate authority, and the one-click Windows installer.
