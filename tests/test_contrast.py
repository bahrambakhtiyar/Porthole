"""Colour contrast (WCAG 2.2 AA) of both themes, computed from the colour tokens in web/style.css.

Text needs 4.5:1 (3:1 when large: 24 px, or 18.66 px bold); the parts of a control that show what it
is and which state it is in (switch tracks and thumbs, focus rings, status dots) need 3:1 against
what is next to them. The pairs checked here are the ones the stylesheet uses: the second test
class checks that it still uses them, so a change of pairing shows up here too.

Run this file directly to print the table that docs/design.md quotes.
"""

import re
import unittest
from pathlib import Path

CSS = re.sub(r"/\*.*?\*/", "", (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8"), flags=re.S)

TEXT = 4.5
LARGE_TEXT = 3.0
NON_TEXT = 3.0
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def rules(css: str = CSS) -> list[tuple[str, str]]:
    """(selector, declarations) of every plain rule, with the selector's spaces tidied."""
    return [(" ".join(selector.split()), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def rule(selector: str) -> str:
    """The declarations of the first rule whose selector is exactly `selector`."""
    wanted = " ".join(selector.split())
    for found, body in rules():
        if found == wanted:
            return body
    raise AssertionError(f"no rule for {selector!r} in style.css")


def tokens(declarations: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", declarations))


def theme_tokens() -> dict[str, dict[str, str]]:
    media = re.search(r"@media \(prefers-color-scheme: dark\) \{(.*?)\n\}", CSS, re.S).group(1)
    return {
        "light": tokens(rule(":root")),
        "dark": tokens(rule(':root[data-theme="dark"]')),
        "auto-dark": tokens(media),
    }


def parse_color(text: str) -> tuple[float, ...]:
    """(r, g, b, a) from #rgb, #rrggbb or rgb()/rgba() with 0-255 channels."""
    text = text.strip().lower()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0)
    match = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if not match:
        raise ValueError(f"not a colour: {text!r}")
    parts = [float(p) for p in re.split(r"[\s,/]+", match.group(1).strip()) if p]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(color: tuple[float, ...], background: tuple[float, ...]) -> tuple[float, float, float]:
    """A see-through colour drawn on an opaque background, mixed as browsers do (in sRGB)."""
    alpha = color[3] if len(color) > 3 else 1.0
    return tuple(color[i] * alpha + background[i] * (1 - alpha) for i in range(3))


def _linear(channel: float) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = (_linear(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def brightness(rgb, amount: float) -> tuple[float, float, float]:
    """CSS `filter: brightness()`; the darker of the two ways browsers may apply it (sRGB or linear light)."""
    srgb = tuple(min(255.0, c * amount) for c in rgb[:3])

    def back(value: float) -> float:
        value = min(1.0, value)
        c = value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
        return c * 255

    linear = tuple(back(_linear(c) * amount) for c in rgb[:3])
    return min(srgb, linear, key=luminance)


def theme_pairs(t: dict[str, str]):
    """(what, foreground, background, required) for one theme, from its tokens."""
    c = {name[2:]: parse_color(value) for name, value in t.items()}
    hover = brightness(c["accent"], float(re.search(r"brightness\(([\d.]+)\)", rule("button.primary:hover:not(:disabled)")).group(1)))
    return [
        ("text on the page", c["text"], c["bg"], TEXT),
        ("text on grey (link box, chosen theme, hover)", c["text"], c["surface"], TEXT),
        ("quiet text (notes, Quit, times)", c["muted"], c["bg"], TEXT),
        ("quiet text on grey", c["muted"], c["surface"], TEXT),
        ("error text", c["error"], c["bg"], TEXT),
        ("weak signal text", c["weak"], c["bg"], TEXT),
        ("main button text", c["accent-text"], c["accent"], TEXT),
        ("main button text, hovered", c["accent-text"], hover, TEXT),
        ("focus ring", c["accent"], c["bg"], NON_TEXT),
        ("switch on: track against the page", c["accent"], c["bg"], NON_TEXT),
        ("switch on: thumb against the track", c["accent-text"], c["accent"], NON_TEXT),
        ("switch off: track against the page", c["control"], c["bg"], NON_TEXT),
        ("switch off: thumb against the track", c["knob"], c["control"], NON_TEXT),
        ("status dot: not sharing, paused ring", c["control"], c["bg"], NON_TEXT),
        ("status dot: live", c["live"], c["bg"], NON_TEXT),
        ("status dot: weak", c["weak"], c["bg"], NON_TEXT),
        ("status dot: can't connect", c["error"], c["bg"], NON_TEXT),
    ]


def _without_at_rules(css: str) -> str:
    """The stylesheet without its @media and @keyframes blocks (their rules apply only sometimes)."""
    out, i = [], 0
    while (at := css.find("@", i)) >= 0:
        out.append(css[i:at])
        depth, j = 1, css.index("{", at) + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    out.append(css[i:])
    return "".join(out)


def _split_selectors(text: str) -> list[str]:
    """`a, b:not(c, d)` -> ['a', 'b:not(c, d)']: commas outside brackets only."""
    parts, depth, current = [], 0, ""
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return [part for part in parts if part]


_SIMPLE = re.compile(r"\*|[a-z][a-z0-9-]*|#[\w-]+|\.[\w-]+|\[[^\]]+\]|::[\w-]+|:not\(([^()]*)\)|:[\w-]+")


def _compound(text: str, state: dict) -> tuple[bool, tuple[int, int, int]]:
    """Whether one compound selector (`button:hover:not(:disabled)`) matches a viewer bar button in
    `state` (hover, pressed, focus: it is never disabled and has no class or id), and its specificity."""
    pos, verdicts, ids, classes, types = 0, [], 0, 0, 0
    while pos < len(text):
        match = _SIMPLE.match(text, pos)
        if not match:
            raise ValueError(f"can't read the selector {text!r}")
        token, pos = match.group(0), match.end()
        if token == "*":
            verdicts.append(True)
        elif re.fullmatch(r"[a-z][a-z0-9-]*", token):
            types += 1
            verdicts.append(token == "button")
        elif token.startswith("#"):
            ids += 1
            verdicts.append(False)
        elif token.startswith("."):
            classes += 1
            verdicts.append(False)
        elif token.startswith("::"):
            types += 1
            verdicts.append(False)  # a part drawn by the element, not the element itself
        elif token.startswith(":not("):
            inner, specificity = _compound(match.group(1).strip(), state)
            ids, classes, types = ids + specificity[0], classes + specificity[1], types + specificity[2]
            verdicts.append(not inner)
        elif token.startswith("["):
            classes += 1
            known = {'[aria-pressed="true"]': state["pressed"], "[hidden]": False}
            verdicts.append(known.get(token))
        else:
            classes += 1
            known = {":hover": state["hover"], ":focus-visible": state["focus"], ":focus": state["focus"], ":disabled": False, ":root": False, ":checked": False}
            verdicts.append(known.get(token))
    if False in verdicts:
        return False, (ids, classes, types)
    if None in verdicts:
        raise ValueError(f"the contrast test doesn't know what {text!r} means for a viewer bar button")
    return True, (ids, classes, types)


def _selector(selector: str, state: dict) -> tuple[bool, tuple[int, int, int]]:
    """Like _compound for a whole selector. Viewer bar buttons sit right inside `.view-bar`, so only
    selectors with no ancestor, or with `.view-bar` (or `.view-bar >`) before them, can match."""
    *ancestors, subject = selector.split()
    if ancestors not in ([], [".view-bar"], [".view-bar", ">"]):
        return False, (0, 0, 0)
    matched, (ids, classes, types) = _compound(subject, state)
    return matched, (ids, classes + (1 if ancestors else 0), types)


def bar_button_style(*, hover=False, pressed=False, focus=False) -> dict[str, str]:
    """The background, text colour and focus ring a viewer bar button gets in one state, picked the
    way the browser's cascade picks them: the most specific matching rule wins, then the later one."""
    state = {"hover": hover, "pressed": pressed, "focus": focus}
    winners: dict[str, tuple[tuple, str]] = {}
    for order, (selectors, body) in enumerate(rules(_without_at_rules(CSS))):
        for selector in _split_selectors(selectors):
            matched, specificity = _selector(selector, state)
            if not matched:
                continue
            for name, value in re.findall(r"(?<![\w-])([\w-]+):\s*([^;]+);", body):
                if name in ("background", "background-color"):
                    found = ("background", value)
                elif name == "color":
                    found = ("color", value)
                elif name == "outline":
                    colour = re.search(r"solid\s+(.+)", value)
                    found = ("ring", colour.group(1)) if colour else None
                elif name == "outline-color":
                    found = ("ring", value)
                else:
                    found = None
                if found and (found[0] not in winners or (specificity, order) >= winners[found[0]][0]):
                    winners[found[0]] = ((specificity, order), found[1].strip())
    return {name: value for name, (_, value) in winners.items()}


BAR_STATES = (
    ("plain", {}),
    ("hovered", {"hover": True}),
    ("pressed", {"pressed": True}),
    # A mouse click leaves the pointer over the button it just pressed, and phones keep :hover on
    # the button last tapped.
    ("pressed and hovered", {"pressed": True, "hover": True}),
)


def viewer_pairs():
    """The viewer's controls float over the picture, which can be anything from black to white."""
    badge_bg = parse_color(re.search(r"background:\s*([^;]+);", rule(".badge")).group(1))
    badge_dot = parse_color(re.search(r"background:\s*([^;]+);", rule(".badge .dot")).group(1))
    tap_bg = parse_color(re.search(r"background:\s*([^;]+);", rule(".overlay.tap")).group(1))
    tap_ring = parse_color(re.search(r"outline-color:\s*([^;]+);", rule(".overlay.tap :focus-visible")).group(1))
    pairs = []
    for picture_name, picture in (("white", WHITE), ("black", BLACK)):
        for state_name, state in BAR_STATES:
            for focus in (False, True):
                style = bar_button_style(focus=focus, **state)
                on_button = over(parse_color(style["background"]), picture)
                what = f"{state_name}{', focused' if focus else ''} bar button"
                pairs.append((f"{what} text, {picture_name} picture", parse_color(style["color"]), on_button, TEXT))
                if focus:
                    # The ring is drawn inside the button, so it sits on the button's own colour.
                    pairs.append((f"{what} ring, {picture_name} picture", parse_color(style["ring"]), on_button, NON_TEXT))
        pairs += [
            (f"Weak signal badge text, {picture_name} picture", WHITE, over(badge_bg, picture), TEXT),
            (f"Weak signal badge dot, {picture_name} picture", badge_dot, over(badge_bg, picture), NON_TEXT),
            (f"Tap to play (large), {picture_name} picture", WHITE, over(tap_bg, picture), LARGE_TEXT),
            (f"Play button focus ring, {picture_name} picture", tap_ring, over(tap_bg, picture), NON_TEXT),
        ]
    return pairs


class ContrastTest(unittest.TestCase):
    def test_auto_dark_uses_the_dark_colours(self):
        themes = theme_tokens()
        self.assertEqual(themes["auto-dark"], themes["dark"])
        self.assertEqual(set(themes["light"]), set(themes["dark"]))

    def test_both_themes_meet_aa(self):
        for theme, t in theme_tokens().items():
            for what, fg, bg, required in theme_pairs(t):
                with self.subTest(theme=theme, pair=what):
                    self.assertGreaterEqual(round(ratio(fg, bg), 2), required, f"{theme}: {what} is {ratio(fg, bg):.2f}:1")

    def test_viewer_controls_meet_aa_over_any_picture(self):
        for what, fg, bg, required in viewer_pairs():
            with self.subTest(pair=what):
                self.assertGreaterEqual(round(ratio(fg, bg), 2), required, f"{what} is {ratio(fg, bg):.2f}:1")

    def test_the_maths(self):
        self.assertAlmostEqual(ratio(WHITE, BLACK), 21.0)
        self.assertAlmostEqual(ratio(parse_color("#777"), WHITE), 4.48, places=2)
        self.assertEqual(over(parse_color("rgba(0, 0, 0, 0.5)"), WHITE), (127.5, 127.5, 127.5))

    def test_the_cascade(self):
        # Each state's colours come from the rules the browser would pick, not from one rule alone.
        self.assertEqual(bar_button_style()["background"], "rgba(24, 24, 24, 0.82)")
        self.assertEqual(bar_button_style(hover=True)["background"], "rgba(48, 48, 48, 0.9)")
        self.assertEqual(bar_button_style(pressed=True), {"background": "#fff", "color": "#000"})
        self.assertEqual(bar_button_style(focus=True)["ring"], "#fff")
        self.assertEqual(bar_button_style(pressed=True, focus=True)["ring"], "#000")
        self.assertEqual(_split_selectors("a, b:not(c, d)"), ["a", "b:not(c, d)"])
        self.assertEqual(_compound("button:hover:not(:disabled)", {"hover": True, "pressed": False, "focus": False}), (True, (0, 2, 1)))
        with self.assertRaises(ValueError):
            _compound("button:first-child", {"hover": False, "pressed": False, "focus": False})


class PairingTest(unittest.TestCase):
    """The checked pairs are the ones the stylesheet uses."""

    def assert_uses(self, selector, *declarations):
        body = rule(selector)
        for declaration in declarations:
            self.assertRegex(body, re.escape(declaration).replace(r"\ ", r"\s*"), f"{selector}: {declaration}")

    def test_pairs(self):
        self.assert_uses("body", "background: var(--bg)", "color: var(--text)")
        self.assert_uses(":focus-visible", "outline: 2px solid var(--accent)")
        self.assert_uses(".segmented input:focus-visible + span", "outline: 2px solid var(--accent)")
        self.assert_uses("button.primary", "background: var(--accent)", "color: var(--accent-text)")
        self.assert_uses("button.text", "color: var(--muted)")
        self.assert_uses(".error", "color: var(--error)")
        self.assert_uses(".tag.weak", "color: var(--weak)")
        self.assert_uses(".line", "color: var(--muted)")
        self.assert_uses("#link", "background: var(--surface)")
        self.assert_uses(".toggle", "background: var(--control)")
        self.assert_uses(".toggle::before", "background: var(--knob)")
        self.assert_uses(".toggle:checked", "background: var(--accent)")
        self.assert_uses(".toggle:checked::before", "background: var(--accent-text)")
        self.assert_uses(".dot", "background: var(--control)")
        self.assert_uses('.status[data-state="live"] .dot', "background: var(--live)")
        self.assert_uses(".overlay", "background: var(--bg)", "color: var(--text)")
        self.assert_uses(".overlay-detail", "color: var(--muted)")
        self.assert_uses(".message p", "color: var(--muted)")

    def test_no_colour_outside_the_tokens_on_the_themed_pages(self):
        # Fixed colours belong to the viewer's black picture area and the QR code's white square only.
        allowed = (".qr", ".preview", ".view-page", ".stage", ".overlay.tap", ".view-bar", ".badge")
        for selector, body in rules():
            if selector.startswith((":root", "@")) or "--" in body:
                continue
            for value in re.findall(r"(?:color|background|border[\w-]*|outline[\w-]*):\s*([^;]+);", body):
                if re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", value):
                    self.assertTrue(selector.startswith(allowed), f"{selector}: {value}")


if __name__ == "__main__":
    for theme, t in theme_tokens().items():
        if theme == "auto-dark":
            continue
        print(f"\n{theme}")
        for what, fg, bg, required in theme_pairs(t):
            print(f"  {ratio(fg, bg):5.2f}:1  (needs {required})  {what}")
    print("\nviewer controls")
    for what, fg, bg, required in viewer_pairs():
        print(f"  {ratio(fg, bg):5.2f}:1  (needs {required})  {what}")
