# Security

Porthole is made for a home, office or classroom Wi-Fi. **Use it on networks you trust.** This page says what Porthole protects, what it doesn't, and how to report a problem privately.

## What Porthole protects, and what it doesn't

- **The picture and sound are encrypted.** They travel directly between browsers over WebRTC, which always encrypts media (DTLS-SRTP).
- **The pages and the set-up messages are not.** They use plain HTTP on the local network. Someone who can watch the traffic on the same network could read the room code and join the share. "Ask before someone watches", which is on by default, lets you see and refuse anyone you don't expect. An approval is for the device's browser and the network address it was allowed from, so someone reading the traffic can't copy an allowed phone's details and be let in from another address: they are asked about like anyone else. A phone that gets a new address (after a Wi-Fi drop, say) is asked again too.
- **Nothing leaves the network.** Porthole makes no internet connections of its own.
- **Only your PC controls sharing.** The sharer page and its actions (start, stop, record, quit) work only from the PC running Porthole, and only from Porthole's own page.
- **Viewers see whatever you share,** including notifications that pop up. Use Do not disturb, or share a single window instead of the whole screen.

Don't use Porthole on public Wi-Fi such as cafés, hotels or airports unless you're comfortable with the points above.

## Reporting a problem

Please report security problems privately, not in a public issue:

1. Use GitHub's **Report a vulnerability** button on the repository's [Security tab](https://github.com/bahrambakhtiyar/Porthole/security).
2. Include what you did, what happened, and the Porthole version and operating system.

## What happens next

- **You'll get an answer within a week.**
- **Once a fix is released,** you'll be credited in the release notes unless you'd rather not be.

## Supported versions

| Version | Security fixes |
| --- | --- |
| The newest release | Yes |
| Older releases | No |

Only the newest release gets security fixes.
