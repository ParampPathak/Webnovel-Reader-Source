# Private iPhone distribution

The native application is privately distributed through the maintainer's existing signing workflow. It is not offered as an App Store download. The source snapshot contains neither signing material nor a claim that unattended signing renewal is reliable.

Use the unsigned simulator workflow in DEMO-SETUP.md for development and screenshots. Device installation requires the developer's own authorized identity and profile. Local.xcconfig, certificates, private origins, pairing records, profile inventories and recovery receipts must stay outside Git.

Build scripts create unsigned input packages and do not install, renew or revoke certificates. Background audio and listening-journal behavior are separate from app-signing automation. No repository instructions should uninstall an existing personal app or reset its journal merely to update a build.
