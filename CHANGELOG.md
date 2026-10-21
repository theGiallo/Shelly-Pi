# Changelog

## Unreleased

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
