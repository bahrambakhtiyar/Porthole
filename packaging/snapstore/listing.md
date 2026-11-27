# Porthole in the Snap Store

Everything for Porthole's page in the Snap Store, [snapcraft.io/porthole](https://snapcraft.io/porthole): what comes with each upload, and what to set by hand on the store's dashboard. The pictures are in this folder and are made by `node tools/store_images.mjs` (see the top of that file); run it again after the pages change, then upload the new pictures.

Only snaps with a banner can be picked for the store's featured spot at the top, so the banner matters as much as the screenshots. The rest of a complete listing: the icon, a clear summary and description, screenshots, two categories, links, and a release on the stable channel.

## 1. What comes with each upload

The title, summary, description and links are read from `snap/snapcraft.yaml` when a new revision is uploaded, and so is the icon: the `icon:` key there names `snap/gui/porthole.png` (the ring logo, 256 × 256), which snapcraft puts in the snap as `meta/gui/icon.png`, the only place the store looks for an icon. Nothing to paste for them; after the upload, check that the dashboard shows them as below and shows the ring logo as the icon. If it shows a blank placeholder icon instead, upload `snap/gui/porthole.png` as the icon on the dashboard. To change one, change it in `snap/snapcraft.yaml` first and copy it here, so the next upload doesn't bring back older text.

**Title**

```text
Porthole
```

**Summary** (75 characters; the store allows 78)

```text
Share your screen with phones and PCs on your Wi-Fi. They just open a link.
```

**Description** (about 240 words, in the simple Markdown the store shows: short paragraphs and "- " lists)

```text
Porthole shows your computer's screen on phones, tablets and other computers on the same Wi-Fi. People watching scan a QR code or open a link in their browser. They install nothing and need no account.

- Nothing to install for viewers: Chrome on Android, Safari on iPhone and iPad, or any modern browser on a computer.
- Works without internet. The picture goes straight from your computer to each screen over your own network.
- You decide who watches. With Ask before someone watches, on by default, each new device waits with a 3-digit code until you press Allow.
- An easy address, on by default: people on your Wi-Fi can type porthole.local:8765 instead of the link. Some Android phones can't open such names, so they scan the QR code.
- Record what you share into your Videos folder, pause for a moment, and keep mini controls at hand in Chrome or Edge.
- Light and dark themes.

How it works: start Porthole, and it opens in your default browser. Press Start sharing and pick a screen or a window. Viewers scan the QR code or open the link on their phone or computer.

Privacy: Porthole makes no internet connections of its own, and the picture and sound are encrypted. The pages use plain HTTP on your own network, so use it on networks you trust, such as your home, office or classroom Wi-Fi.

Free and open source under the GNU AGPL-3.0.
```

**Links**

| Link | Address |
| --- | --- |
| Website | https://github.com/bahrambakhtiyar/Porthole |
| Source code | https://github.com/bahrambakhtiyar/Porthole |
| Issues | https://github.com/bahrambakhtiyar/Porthole/issues |
| Contact | https://github.com/bahrambakhtiyar/Porthole/issues |
| Donation | https://kreosus.com/bahram_bakhtiyar |

Contact points to the issues page, so no email address is published.

## 2. What to set on the dashboard

Sign in at [snapcraft.io](https://snapcraft.io) with the account that registered the name `porthole`, and open the snap's listing page on the dashboard: [snapcraft.io/porthole/listing](https://snapcraft.io/porthole/listing).

1. **Images.** The store takes up to five pictures in all, and the banner may count as one of them, so there are four screenshots. Upload them in this order:

   | # | File | Where | Size | Alt text |
   | --- | --- | --- | --- | --- |
   | 1 | `banner.png` | Banner | 1920 × 640 | Porthole's ring logo and name on blue, with the words "Share your screen with phones on your Wi-Fi", beside a PC screen and a phone showing the same slide |
   | 2 | `screenshot-1.png` | Screenshots, first | 1920 × 1080 | "Share your screen with any phone": the Porthole page in a browser on a PC while sharing a slide, and a phone on its side showing the same slide |
   | 3 | `screenshot-2.png` | Screenshots, second | 1920 × 1080 | "Phones just scan the QR code": the Join part of the Porthole page in a browser, with its QR code and link, and a phone that opened the link, watching the slide |
   | 4 | `screenshot-3.png` | Screenshots, third | 1920 × 1080 | "You decide who watches": a phone waiting with its code, and the same code on the PC next to Allow and Deny |
   | 5 | `screenshot-4.png` | Screenshots, fourth | 1920 × 1080 | "Light or dark, your choice": the Porthole page in its light theme and in its dark theme, side by side |

   Each file is a PNG under 2 MB; `tests/test_packaging.py` checks the sizes, the 3:1 banner and the store's other limits. Where the dashboard has no place for alt text, keep it here for other pages that show the pictures.

2. **Categories.** Primary: **Productivity**. Secondary: **Utilities**.
   - Productivity first, because Porthole is for showing your screen to the people in the room: a slide in a meeting, a lesson in a classroom, a document at home. That is where people look for tools to present and work together.
   - Utilities second, as the store's place for small everyday tools, so people browsing for one can find it too.

3. **Video** (optional). The listing can also show a short video from a link (YouTube, Vimeo or asciinema). There is none yet; a 30 to 60 second demo of a real share (Start sharing, a phone scanning the QR code, Allow, the phone watching) can be added later.

4. **Save, then check.** Save the changes, open [snapcraft.io/porthole](https://snapcraft.io/porthole) to see the page as visitors will, and check under the snap's **Releases** that the stable channel has the current release.

## 3. Being featured

The featured spots are picked by the Snap Store's team. A banner is needed for the spot at the top; beyond that, a complete listing (the icon, the banner, the four screenshots, both categories, the links and a stable release) gives Porthole its best chance. Once it is complete, you can suggest Porthole to the team on the [Snapcraft forum](https://forum.snapcraft.io).
