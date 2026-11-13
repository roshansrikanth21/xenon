"""Where an action lands: app classes, denied surfaces, keys and file paths.

This module is data plus small pure functions. The classifier in
``xenon.actions`` asks it three questions about the live target:

* which app class is it (comms, browser, shell, settings, other, unknown)?
* is it a surface Xenon must never touch (consoles, UAC, identity and
  payment pages, Xenon's own windows, ...)?
* is this key chord or file path one an agent may never use?

Every unknown resolves to the stricter answer: an unreadable browser URL is
treated as comms, an unidentified process is "unknown" (handled like comms by
the classifier), and a key sequence that does not parse is refused.

Names are compared after ``strip_invisible`` + NFKC + confusable folding, so
zero-width characters and Greek or Cyrillic look-alikes cannot dodge a rule.
"""
from __future__ import annotations

import ntpath
import re
from urllib.parse import unquote, urlsplit

from xenon.actions import (
    APP_BROWSER,
    APP_COMMS,
    APP_OTHER,
    APP_SETTINGS,
    APP_SHELL,
    APP_UNKNOWN,
    ActionContext,
)
from xenon.untrusted import has_invisible, skeleton, strip_invisible

# ---------------------------------------------------------------------------
# Process images
# ---------------------------------------------------------------------------

COMMS_IMAGES = frozenset(
    {
        "WhatsApp.Root.exe",
        "WhatsApp.exe",
        "olk.exe",
        "outlook.exe",
        "Telegram.exe",
        "Discord.exe",
        "ms-teams.exe",
        "slack.exe",
        "Signal.exe",
        "claude.exe",  # the Claude Desktop app: anything typed there leaves the PC
        # Not in the design list, added because they are messaging clients too.
        "Teams.exe",
        "HxOutlook.exe",
        "thunderbird.exe",
        "Zoom.exe",
        "Messenger.exe",
        "Skype.exe",
        "Element.exe",
        "ChatGPT.exe",
        # Variants and other messengers: an unrecognised messenger would
        # otherwise read as an ordinary local app, the looser answer.
        "DiscordCanary.exe",
        "DiscordPTB.exe",
        "DiscordDevelopment.exe",
        "Mailspring.exe",
        "Beeper.exe",
        "Viber.exe",
        "WeChat.exe",
        "Weixin.exe",
        "LINE.exe",
        "KakaoTalk.exe",
        "Ferdium.exe",
        "Franz.exe",
        "Rambox.exe",
        "Session.exe",
        "Threema.exe",
        "Wire.exe",
        "Zalo.exe",
        "Spark.exe",
        "MailClient.exe",  # eM Client
        "Postbox.exe",
    }
)

BROWSER_IMAGES = frozenset(
    {
        "msedge.exe",
        "chrome.exe",
        "brave.exe",
        "opera.exe",  # Opera GX runs as opera.exe too
        "firefox.exe",
        "vivaldi.exe",
        "arc.exe",
        "chromium.exe",
        "iexplore.exe",
        "librewolf.exe",
        "waterfox.exe",
        "floorp.exe",
        "zen.exe",
    }
)

SHELL_IMAGES = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "WindowsTerminal.exe",
        "OpenConsole.exe",
        "conhost.exe",
        "wsl.exe",
        "bash.exe",
        "mintty.exe",
        "wslhost.exe",
        "powershell_ise.exe",
        "git-bash.exe",
        "sh.exe",
        "ubuntu.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "Hyper.exe",
        "Tabby.exe",
        "putty.exe",
        "kitty.exe",
        "ConEmu.exe",
        "ConEmu64.exe",
        "ConEmuC.exe",
        "ConEmuC64.exe",
        "Cmder.exe",
        "MobaXterm.exe",
        "Termius.exe",
        "SecureCRT.exe",
        "Xshell.exe",
        "ssh.exe",
        "plink.exe",
        "WinSCP.exe",
        "FluentTerminal.exe",
    }
)

# Words that mark an image as a terminal or a messenger even when it is not
# listed: "every unknown resolves to the stricter answer", and a closed list
# is exactly how ConEmu64.exe or DiscordCanary.exe slipped through as local.
_SHELL_WORDS = ("term", "ssh", "shell", "console", "conemu", "cmder", "putty", "securecrt",
                "bash", "powershell", "pwsh", "wsl")
_COMMS_WORDS = ("discord", "whatsapp", "telegram", "signal", "slack", "teams", "messenger",
                "wechat", "viber", "beeper", "skype", "thunderbird", "outlook", "mail", "chat")

# Native apps whose own UI decides what a click or Enter does, so a plain
# button or a newline there is local. Anything not listed (an unrecognised
# messenger, an Electron app rendering someone else's content) is read as an
# unidentified app for Enter, activation and newlines.
KNOWN_LOCAL_IMAGES = frozenset(
    {
        "notepad.exe",
        "notepad++.exe",
        "mspaint.exe",
        "CalculatorApp.exe",
        "calc.exe",
        "WINWORD.EXE",
        "EXCEL.EXE",
        "POWERPNT.EXE",
        "ONENOTE.EXE",
        "MSACCESS.EXE",
        "MSPUB.EXE",
        "wordpad.exe",
        "write.exe",
        "explorer.exe",
        "SnippingTool.exe",
        "ScreenClippingHost.exe",
        "Photos.exe",
        "Microsoft.Photos.exe",
        "Time.exe",  # Clock
        "Code.exe",
        "Code - Insiders.exe",
        "devenv.exe",
        "sublime_text.exe",
        "pycharm64.exe",
        "idea64.exe",
        "Obsidian.exe",
        "vlc.exe",
        "wmplayer.exe",
        "Microsoft.Media.Player.exe",
        "SumatraPDF.exe",
        "7zFM.exe",
        "SystemSettings.exe",
    }
)

SETTINGS_IMAGES = frozenset({"SystemSettings.exe"})

# Not denied, but anything read from them is sensitive (never sent to a
# cloud model without redaction, never into a recipe).
SENSITIVE_IMAGES = frozenset(
    {
        "BurpSuitePro.exe",
        "BurpSuiteCommunity.exe",
        "KeePass.exe",
        "KeePassXC.exe",
        "1Password.exe",
        "Bitwarden.exe",
        "Dashlane.exe",
    }
)

# Images that host someone else's window, so the image says nothing about
# the app. They classify as unknown, which the classifier treats strictly.
HOST_IMAGES = frozenset({"ApplicationFrameHost.exe", "msedgewebview2.exe", "electron.exe"})

DENIED_IMAGES = frozenset(
    {
        "StartMenuExperienceHost.exe",
        "SearchHost.exe",
        "SearchApp.exe",
        "SearchUI.exe",
        "ShellExperienceHost.exe",
        "ShellHost.exe",
        "regedit.exe",
        "regedt32.exe",
        "mmc.exe",  # Task Scheduler, certmgr, services, firewall snap-ins
        "taskschd.exe",
        "certmgr.exe",
        "SecHealthUI.exe",
        "consent.exe",
        "CredentialUIBroker.exe",
        "credwiz.exe",
        "SystemPropertiesAdvanced.exe",  # the environment-variables dialog
        "SystemPropertiesProtection.exe",
        "SystemPropertiesRemote.exe",
        "UserAccountControlSettings.exe",
        "SystemSettingsAdminFlows.exe",
        "LockApp.exe",
        "LogonUI.exe",
        "Taskmgr.exe",
        "msconfig.exe",
        "rundll32.exe",  # hosts Control Panel applets: env vars, Internet Options
        "msiexec.exe",
        "mstsc.exe",  # keystrokes there land on another machine
        "rasphone.exe",  # VPN dialler
        "smartscreen.exe",  # "Windows protected your PC" / "Run anyway"
    }
)

# Python hosts: a window here with a Xenon (or default Tk) title is Xenon's
# own UI, which its agents may never target.
XENON_HOST_IMAGES = frozenset({"python.exe", "pythonw.exe", "py.exe", "pyw.exe", "xenon.exe"})

# Editors whose integrated terminal panel is a console in disguise.
EDITOR_IMAGES = frozenset({"Code.exe", "Code - Insiders.exe", "Cursor.exe", "Windsurf.exe", "VSCodium.exe"})


def norm_image(process_image: str) -> str:
    """Basename, casefolded, with ``.exe`` added when missing.

    ``Win32Backend.process_name`` drops the extension, a UIA reader may not,
    and a full path may arrive from QueryFullProcessImageNameW: all three
    must compare equal.
    """
    name = strip_invisible(process_image or "").strip().strip('"').strip()
    if not name:
        return ""
    name = ntpath.basename(name.replace("/", "\\")).casefold()
    if name and not name.endswith(".exe"):
        name += ".exe"
    return name


def _folded(images: frozenset[str]) -> frozenset[str]:
    return frozenset(norm_image(image) for image in images)


_COMMS = _folded(COMMS_IMAGES)
_BROWSER = _folded(BROWSER_IMAGES)
_SHELL = _folded(SHELL_IMAGES)
_SETTINGS = _folded(SETTINGS_IMAGES)
_SENSITIVE = _folded(SENSITIVE_IMAGES)
_HOSTS = _folded(HOST_IMAGES)
_DENIED = _folded(DENIED_IMAGES)
_XENON_HOSTS = _folded(XENON_HOST_IMAGES)
_EDITORS = _folded(EDITOR_IMAGES)
_KNOWN_LOCAL = _folded(KNOWN_LOCAL_IMAGES)


def is_browser_image(process_image: str) -> bool:
    return norm_image(process_image) in _BROWSER


def is_shell_image(process_image: str) -> bool:
    """A listed terminal, or any image whose name says it is one."""
    image = norm_image(process_image)
    if not image:
        return False
    if image in _SHELL:
        return True
    stem = image.removesuffix(".exe")
    return stem == "cmd" or any(word in stem for word in _SHELL_WORDS)


def is_comms_image(process_image: str) -> bool:
    """A listed messenger or mail client, or any image whose name says it is one."""
    image = norm_image(process_image)
    if not image:
        return False
    if image in _COMMS:
        return True
    stem = image.removesuffix(".exe")
    return any(word in stem for word in _COMMS_WORDS)


def is_known_local_image(process_image: str) -> bool:
    """True only for the native apps listed in ``KNOWN_LOCAL_IMAGES``."""
    return norm_image(process_image) in _KNOWN_LOCAL


def is_sensitive_image(process_image: str) -> bool:
    return norm_image(process_image) in _SENSITIVE or is_shell_image(process_image)


# ---------------------------------------------------------------------------
# URLs and domains
# ---------------------------------------------------------------------------

# Browser windows on these registrable domains (or their subdomains) are
# comms: anything clicked or typed there can reach another person.
COMMS_DOMAINS = frozenset(
    {
        "mail.google.com",
        "outlook.live.com",
        "outlook.office.com",
        "outlook.office365.com",
        "web.whatsapp.com",
        "discord.com",
        "discord.gg",
        "x.com",
        "twitter.com",
        "linkedin.com",
        "reddit.com",
        "github.com",
        "slack.com",
        "teams.microsoft.com",
        "teams.live.com",
        "web.telegram.org",
        "messenger.com",
        "facebook.com",
        "instagram.com",
        "forms.gle",
        "forms.office.com",
        "docs.google.com",  # Google Forms live under docs.google.com/forms
        "mail.proton.me",
        "mail.yahoo.com",
        "chatgpt.com",
        "claude.ai",
        "gemini.google.com",
    }
)

# Identity, consent and payment hosts: DENIED outright.
DENIED_HOSTS = frozenset(
    {
        "accounts.google.com",
        "myaccount.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "login.microsoft.com",
        "login.windows.net",
        "account.live.com",
        "account.microsoft.com",
        "appleid.apple.com",
        "idmsa.apple.com",
        "pay.google.com",
        "payments.google.com",
        "paypal.com",
        "paypal.me",
        "checkout.stripe.com",
        "buy.stripe.com",
        "billing.stripe.com",
        "razorpay.com",
        "rzp.io",
        "payu.in",
        "cashfree.com",
        "ccavenue.com",
        "billdesk.com",
        "phonepe.com",
        "paytm.com",
        "chromewebstore.google.com",
        "microsoftedge.microsoft.com",
        "addons.mozilla.org",
        "addons.opera.com",
        "aka.ms",  # short links, including aka.ms/devicelogin
    }
)

# A first host label like these marks an identity or payment front door on
# any domain (login.yahoo.com, auth.openai.com, checkout.shopify.com, ...).
DENIED_HOST_LABELS = frozenset(
    {
        "login", "signin", "sso", "auth", "oauth", "accounts", "account", "id",
        "identity", "pay", "payment", "payments", "checkout", "billing", "wallet",
    }
)

DENIED_SCHEMES = frozenset(
    {
        "chrome", "edge", "opera", "brave", "vivaldi", "about", "javascript",
        "vbscript", "data", "blob", "view-source", "devtools", "chrome-extension",
        "chrome-search", "chrome-untrusted", "extension", "moz-extension",
        "ms-browser-extension", "edge-extension", "upi", "intent", "ms-settings",
        "search-ms", "shell", "ms-msdt", "ms-officecmd", "mailto", "tel", "sms",
    }
)

_OAUTH_PATH = re.compile(r"/oauth[^/?#]*/(?:[^?#]*/)?authori[sz]e")
# Path segments that mark identity, consent, device-code and payment pages.
_DENIED_SEGMENTS = frozenset(
    {
        "login", "signin", "sign-in", "log-in", "signup", "sign-up", "register",
        "auth", "authorize", "authorise", "oauth", "oauth2", "sso", "saml", "saml2",
        "consent", "devicelogin", "2fa", "mfa", "password", "reset-password",
        "checkout", "payment", "payments", "pay", "billing", "buy",
    }
)

_PAGE_EXTENSIONS = frozenset(
    {"php", "aspx", "asp", "html", "htm", "jsp", "jspx", "srf", "do", "action", "cgi", "pl", "cfm"}
)

# Public suffixes that take three labels. A small bundled subset, enough to
# group his usual sites; anything else falls back to the last two labels.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "org.in", "ac.in", "gov.in",
        "net.in", "edu.in", "res.in", "nic.in", "com.au", "net.au", "org.au",
        "edu.au", "co.jp", "co.nz", "com.br", "com.cn", "com.sg", "com.my",
        "co.za", "com.mx", "github.io", "pages.dev", "vercel.app", "netlify.app",
        "web.app", "firebaseapp.com", "herokuapp.com", "blogspot.com",
        "azurewebsites.net", "cloudfront.net", "appspot.com", "workers.dev",
    }
)


def _compact_url(url: str) -> str:
    # Browsers drop tabs and newlines inside a URL ("java\nscript:" runs as
    # "javascript:"), so the check has to as well.
    return re.sub(r"\s+", "", strip_invisible(url or "")).casefold()


def url_scheme(url: str) -> str:
    match = re.match(r"^([a-z][a-z0-9+.-]*):", _compact_url(url))
    return match.group(1) if match else ""


def url_host(url: str) -> str:
    """Lower-cased, punycode hostname of ``url`` ("" when there is none)."""
    compact = _compact_url(url)
    if not compact:
        return ""
    if "://" not in compact:
        scheme = url_scheme(compact)
        if scheme in DENIED_SCHEMES:
            return ""
        compact = "https://" + compact
    try:
        host = urlsplit(compact).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return host


def _host_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def registrable_domain(url: str) -> str:
    """eTLD+1 of ``url`` from a bundled public-suffix subset ("" if none)."""
    host = url_host(url)
    if not host:
        return ""
    if re.fullmatch(r"[\d.]+|\[[0-9a-f:]+\]", host):
        return host
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_comms_url(url: str) -> bool:
    host = url_host(url)
    if not host:
        return False
    if _host_matches(host, COMMS_DOMAINS):
        return True
    # Any forms host (forms.example.com) collects data for someone else.
    return host.startswith("forms.") or "/forms/" in _compact_url(url)


def denied_url_reason(url: str) -> str | None:
    """Why ``url`` is a denied surface, or ``None``.

    Covers browser-internal pages, script and data URLs, identity, consent,
    device-code and payment pages. An empty URL is not denied here; the
    caller treats an unreadable URL as comms instead.
    """
    compact = _compact_url(url)
    if not compact:
        return None
    scheme = url_scheme(compact)
    if scheme in DENIED_SCHEMES:
        return f"{scheme}: pages are never automated"
    host = url_host(compact)
    if host:
        if _host_matches(host, DENIED_HOSTS):
            return f"{host} is an identity or payment page"
        first_label = host.split(".")[0]
        if first_label in DENIED_HOST_LABELS and host.count(".") >= 1:
            return f"{host} looks like an identity or payment page"
    rest = compact.split("://", 1)[1] if "://" in compact else compact
    slash = rest.find("/")
    path_and_query = rest[slash:] if slash >= 0 else ""
    # Servers decode "%61uthorize" as "authorize"; decode twice for the
    # double-encoded variant before matching.
    path_and_query = unquote(unquote(path_and_query)).casefold()
    if _OAUTH_PATH.search(path_and_query):
        return "OAuth authorize pages are never automated"
    query = path_and_query.split("?", 1)[1] if "?" in path_and_query else ""
    if "client_id=" in query and ("redirect_uri=" in query or "response_type=" in query):
        return "OAuth consent flows are never automated"
    path = re.split(r"[?#]", path_and_query, maxsplit=1)[0]
    for segment in path.split("/"):
        stem, _dot, extension = segment.partition(".")
        # "/login.php" is a login page; "/docs/auth.md" is a file about auth.
        if extension and extension not in _PAGE_EXTENSIONS:
            continue
        if stem in _DENIED_SEGMENTS:
            return f"/{stem} pages (identity, consent or payment) are never automated"
    return None


# ---------------------------------------------------------------------------
# App classes and denied surfaces
# ---------------------------------------------------------------------------


def app_class_for(process_image: str, url: str = "") -> str:
    """App class for a process image (and, for browsers, its URL).

    A browser whose URL cannot be read is comms: fail closed. Terminals
    and messengers are recognised by name words too, not only by the list.
    APP_OTHER here does not mean "known to be local": ``is_known_local_image``
    answers that, and the classifier reads anything else strictly for
    Enter, activation and newlines.
    """
    image = norm_image(process_image)
    if not image:
        return APP_UNKNOWN
    if is_shell_image(image):
        return APP_SHELL
    if image in _BROWSER:
        if not _compact_url(url) or is_comms_url(url):
            return APP_COMMS
        return APP_BROWSER
    if is_comms_image(image):
        return APP_COMMS
    if image in _SETTINGS:
        return APP_SETTINGS
    if image in _HOSTS:
        return APP_UNKNOWN
    return APP_OTHER


# Window titles of system tools that are denied whatever process hosts them.
# Matched as a title prefix, not a substring: his own files are often named
# "proxy.py" or "firewall_rules.md", and an editor showing one is not the
# firewall. Only checked for non-browser images: a browser title is
# page-authored.
_DENIED_TITLE_PREFIXES = (
    "registry editor",
    "task scheduler",
    "environment variables",
    "certificates - ",
    "certmgr",
    "credential manager",
    "windows security",
    "windows defender firewall",
    "windows firewall",
    "internet properties",
    "internet options",
    "network connections",
    "network and sharing center",
    "user account control",
    "local group policy editor",
    "system properties",
    "administrator:",
    "windows powershell",
    "command prompt",
    # "Run" / "Run anyway" on these launches whatever was downloaded.
    "open file - security warning",
    "security warning",
    "windows protected your pc",
    "microsoft defender smartscreen",
    "windows defender smartscreen",
)

# live_context appends the Settings page (its breadcrumb) to the window
# title, because the title of every Settings page is just "Settings". When
# the page cannot be read it appends this marker instead, and the page is
# treated as denied: it could be Network, Proxy or VPN.
SETTINGS_PAGE_UNKNOWN = "(settings page unknown)"


def settings_title(title: str, page: str | None) -> str:
    """The Settings window title with the current page, or the unknown marker."""
    page = strip_invisible(page or "").strip()
    return f"{title} - {page}" if page else f"{title} - {SETTINGS_PAGE_UNKNOWN}"

# A VPN or proxy client is a network-settings surface by another name.
_DENIED_IMAGE_WORDS = ("vpn", "proxy", "wireguard", "warp")

# Settings pages that are denied: network, proxy, VPN, security, sign-in.
_DENIED_SETTINGS_PARTS = (
    "proxy",
    "vpn",
    "network & internet",
    "network and internet",
    "dns",
    "wi-fi",
    "ethernet",
    "firewall",
    "windows security",
    "sign-in options",
    "passkey",
    "email & accounts",
    "for developers",
)


def is_denied_surface(ctx: ActionContext) -> str | None:
    """Reason the surface in ``ctx`` is DENIED, or ``None``."""
    image = norm_image(ctx.process_image)
    title = skeleton(ctx.window_title).strip()
    name = skeleton(ctx.element_name).strip()
    role = re.sub(r"[\s_-]+", "", skeleton(ctx.element_role))

    if ctx.app_class == APP_SHELL or is_shell_image(image):
        return "consoles and terminals are never automated"
    if image in _DENIED:
        return f"{image} is a protected system surface"
    if image in _XENON_HOSTS and ("xenon" in title or title in {"", "tk"}):
        return "Xenon never targets its own windows"
    if image in _EDITORS and "terminal" in name:
        return "the editor's terminal panel is a console"
    if image == "explorer.exe":
        if title == "run":
            return "the Run dialog is never automated"
        if name.startswith("address") or "address band" in name or role == "breadcrumb":
            return "the Explorer address bar is never automated"
    if image in _BROWSER or ctx.app_class in (APP_BROWSER,) or ctx.url:
        reason = denied_url_reason(ctx.url)
        if reason:
            return reason
        if title.startswith("devtools"):
            return "browser DevTools are never automated"
    if image and image not in _BROWSER:
        for prefix in _DENIED_TITLE_PREFIXES:
            if title.startswith(prefix):
                return f"'{prefix.rstrip(' -:')}' windows are never automated"
        for word in _DENIED_IMAGE_WORDS:
            if word in image:
                return f"{image} controls network or VPN settings"
    if image in _SETTINGS or ctx.app_class == APP_SETTINGS:
        if SETTINGS_PAGE_UNKNOWN in title:
            return "could not tell which Settings page this is (it could be network, proxy or VPN)"
        for part in _DENIED_SETTINGS_PARTS:
            if part in name or part in title:
                return f"the {part} settings are never automated"
    return None


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

_KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "windows": "win",
    "meta": "win",
    "super": "win",
    "cmd": "win",
    "command": "win",
    "lwin": "win",
    "rwin": "win",
    "start": "win",
    "option": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "altgr": "alt",
    "lshift": "shift",
    "rshift": "shift",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "spacebar": "space",
    "bksp": "backspace",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")


def split_chords(keys: str) -> list[str]:
    """Split "ctrl+a, ctrl+c" or "ctrl + a  enter" into chords.

    Folded with ``skeleton`` so full-width, zero-width and Cyrillic
    look-alike spellings ("\u0430lt+f4") meet the same blocklist.
    """
    text = skeleton(keys or "").strip()
    text = re.sub(r"\s*\+\s*", "+", text)
    text = re.sub(r"page\s+(up|down)", r"page\1", text)
    return [chord for chord in re.split(r"[,;\s]+", text) if chord]


def normalize_chord(chord: str) -> str:
    """Canonical chord: aliases folded, modifiers in a fixed order.

    "Shift+Ctrl+ESC", "control+shift+escape" and "ctrl+shift+esc" all become
    "ctrl+shift+esc".
    """
    parts = [part for part in skeleton(chord or "").replace(" ", "").split("+")]
    keys = [_KEY_ALIASES.get(part, part) for part in parts if part]
    modifiers = [mod for mod in _MODIFIER_ORDER if mod in keys]
    others = [key for key in keys if key not in _MODIFIER_ORDER]
    return "+".join(modifiers + others)


def _chord_set(*chords: str) -> frozenset[str]:
    return frozenset(normalize_chord(chord) for chord in chords)


KEY_BLOCKLIST = _chord_set(
    "alt+f4",
    "win",
    "win+r",
    "win+x",
    "win+l",
    "win+s",
    "win+e",
    "win+q",
    "ctrl+esc",
    "ctrl+shift+esc",
    "ctrl+alt+del",
    "f12",
    "ctrl+shift+i",
    "ctrl+shift+j",
    "ctrl+shift+c",
    "ctrl+shift+delete",  # clear-browsing-data dialog
)

# Refused once a task is tainted, in browsers: each moves focus to the
# omnibox (or a fresh tab's omnibox), where typed text becomes navigation.
TAINTED_BROWSER_KEYS = _chord_set(
    "ctrl+l", "alt+d", "f6", "ctrl+e", "ctrl+k", "ctrl+t", "ctrl+n", "ctrl+shift+n"
)

NAV_KEYS = _chord_set(
    "tab", "shift+tab", "up", "down", "left", "right", "pageup", "pagedown", "home", "end", "esc"
)


def is_nav_keys(keys: str) -> bool:
    """True when every chord in ``keys`` is a pure navigation key."""
    chords = split_chords(keys)
    return bool(chords) and all(normalize_chord(chord) in NAV_KEYS for chord in chords)


def _browserish(ctx: ActionContext | None) -> bool:
    if ctx is None:
        return True
    image = norm_image(ctx.process_image)
    if image in _BROWSER or ctx.app_class == APP_BROWSER:
        return True
    # Unknown process: assume it could be a browser.
    return not image or image in _HOSTS


def key_refused(keys: str, ctx: ActionContext | None = None) -> str | None:
    """Reason a key sequence is refused, or ``None``.

    Any chord with the Windows key is refused, not only the listed ones:
    win+<digit> launches taskbar apps, which is how a terminal gets opened.
    """
    chords = split_chords(keys)
    if not chords:
        return "empty key sequence"
    tainted = bool(ctx is not None and ctx.tainted)
    for chord in chords:
        normal = normalize_chord(chord)
        if not normal:
            return "empty key chord"
        if normal in KEY_BLOCKLIST:
            return f"{normal} is on the key blocklist"
        if "win" in normal.split("+"):
            return f"{normal}: Windows-key shortcuts are blocked"
        if tainted and normal in TAINTED_BROWSER_KEYS and _browserish(ctx):
            return f"{normal} would focus the address bar in a tainted task"
    return None


# ---------------------------------------------------------------------------
# File dialogs
# ---------------------------------------------------------------------------

BLOCKED_EXTENSIONS = frozenset(
    {
        ".bat", ".cmd", ".ps1", ".vbs", ".js", ".hta", ".lnk", ".url", ".exe",
        ".scr", ".msi", ".reg",
        # Same risk class, not in the design list.
        ".psm1", ".psd1", ".ps1xml", ".vbe", ".jse", ".wsf", ".wsh", ".com",
        ".pif", ".cpl", ".msc", ".msp", ".jar", ".dll", ".sys", ".inf", ".appx",
        ".msix", ".appinstaller", ".application", ".settingcontent-ms",
        ".library-ms", ".search-ms", ".diagcab", ".chm", ".scf", ".iqy", ".slk",
        ".xll", ".gadget", ".mht", ".mhtml",
    }
)

_BLOCKED_DIR_PARTS = (
    "\\start menu\\programs",
    "\\startup\\",
    "\\windows\\",
    "\\program files",
    "\\programdata\\",
    "\\.ssh\\",
    "\\.git\\",
    "\\windowspowershell\\",
    "\\powershell\\",
    # Code-loading and persistence: a file here runs the next time Python,
    # Claude Code, VS Code or git starts, without anyone opening it.
    "\\site-packages\\",
    "\\dist-packages\\",
    "\\.venv\\",
    "\\.claude\\",
    "\\.vscode\\",
    "\\.cursor\\",
    "\\.idea\\",
    "\\.config\\",
    "\\appdata\\roaming\\code\\user\\",
    "\\appdata\\roaming\\code - insiders\\user\\",
    "\\appdata\\roaming\\cursor\\user\\",
    "\\appdata\\roaming\\microsoft\\windows\\",
    "\\appdata\\local\\microsoft\\windowsapps\\",
    "\\node_modules\\",
)

# Files that are code by another name: Python modules (an agent-written
# fastpath.py or a .pth in site-packages runs on the next import), and
# hook/config files that name a program to run.
CODE_EXTENSIONS = frozenset({".py", ".pyw", ".pyc", ".pyo", ".pyz", ".pyzw", ".pth", ".ipynb"})
_CODE_FILENAMES = frozenset(
    {"settings.json", "settings.local.json", "keybindings.json", "tasks.json", "launch.json",
     "conftest.py", "sitecustomize.py", "usercustomize.py", "package.json", "makefile",
     "profile.ps1", "microsoft.powershell_profile.ps1", "desktop.ini", "autorun.inf"}
)


def _path_refusal(path: str) -> str | None:
    """Refusals that apply to any path an agent writes, span or not."""
    if has_invisible(path or ""):
        return "paths with hidden or bidi characters are refused"
    raw = strip_invisible(path or "").strip()
    if not raw:
        return "empty path"
    if re.search(r"~\d", raw):
        # 8.3 short names (PROGRA~1, STARTM~1) dodge the folder rules below.
        return "8.3 short names are refused"
    if raw.startswith(("\\\\", "//")):
        return "UNC and device paths are refused"
    if "://" in raw or raw.casefold().startswith(("file:", "shell:", "http:", "https:")):
        return "URLs and shell: locations are refused"
    if "%" in raw or "$" in raw:
        return "environment-variable paths are refused"
    # A colon is only legal right after a drive letter; anything else is an
    # alternate data stream ("notes.txt:evil.ps1") or a scheme.
    if ":" in raw[2:] or (":" in raw[:2] and not re.match(r"^[A-Za-z]:", raw)):
        return "alternate data streams and schemes are refused"
    normal = raw.replace("/", "\\").casefold()
    # Windows drops trailing dots and spaces, so "run.ps1. " is "run.ps1".
    trimmed = normal.rstrip(". ")
    padded = "\\" + trimmed + "\\"
    for part in _BLOCKED_DIR_PARTS:
        if part in padded:
            return f"writing under '{part.strip(chr(92))}' is refused"
    extension = ntpath.splitext(trimmed)[1]
    if extension in BLOCKED_EXTENSIONS:
        return f"{extension} files are refused"
    if extension in CODE_EXTENSIONS:
        return f"{extension} files are code and are refused"
    basename = ntpath.basename(trimmed)
    if basename.startswith("."):
        # .gitconfig (core.fsmonitor), .bashrc, .profile, .npmrc, .env ...
        return "dotfiles (shell, git and tool settings) are refused"
    if basename in _CODE_FILENAMES:
        return f"{basename} configures programs that run on their own and is refused"
    return None


LAUNCH_EXTENSIONS = BLOCKED_EXTENSIONS | CODE_EXTENSIONS


def launch_extension(name: str) -> str | None:
    """The program/script extension at the end of a file name, or ``None``.

    Windows ignores trailing dots and spaces ("run.bat. " is "run.bat"), so
    they are trimmed first. Only the last extension counts: "invoice.pdf.bat"
    is a .bat.
    """
    trimmed = strip_invisible(name or "").strip().rstrip(". ").casefold()
    extension = ntpath.splitext(trimmed)[1]
    return extension if extension in LAUNCH_EXTENSIONS else None


def _words_contain(fragment: str, user_words: str) -> bool:
    """Case-insensitive whole-token containment, punctuation kept.

    Filenames are matched literally (dots and slashes matter), unlike the
    word-level ``exact_span`` used for message text.
    """
    needle = strip_invisible(fragment).replace("/", "\\").casefold().strip()
    haystack = strip_invisible(user_words).replace("/", "\\").casefold()
    if not needle:
        return False
    start = haystack.find(needle)
    while start >= 0:
        before = haystack[start - 1] if start > 0 else " "
        end = start + len(needle)
        after = haystack[end] if end < len(haystack) else " "
        if not (before.isalnum() or before in "_-.") and not (after.isalnum() or after in "_-\\"):
            return True
        start = haystack.find(needle, start + 1)
    return False


def file_dialog_refusal(path: str, user_words: str) -> str | None:
    """Reason a file-dialog path is refused, or ``None``.

    The filename must be an exact span of Roshan's words, and so must any
    folder the path names: a model may not pick where a file goes.
    """
    refusal = _path_refusal(path)
    if refusal:
        return refusal
    raw = strip_invisible(path).strip().replace("/", "\\")
    folder, filename = ntpath.split(raw.rstrip("\\"))
    if not filename:
        return "no filename"
    if not strip_invisible(user_words or "").strip():
        return "the filename must come from your words"
    if not _words_contain(filename, user_words):
        return f"{filename!r} is not in your words"
    if folder and not _words_contain(folder, user_words):
        return f"the folder {folder!r} is not in your words"
    return None


def path_refusal(path: str) -> str | None:
    """Public form of the span-free path rules (for write_file targets)."""
    return _path_refusal(path)


# ---------------------------------------------------------------------------
# Browser URL reader (stub; per-browser names are verified in phase 6)
# ---------------------------------------------------------------------------

_OMNIBOX_NAMES = (
    "address and search bar",  # Chrome, Edge, Brave, Vivaldi
    "address field",  # Opera / Opera GX
    "search with google or enter address",  # Firefox
    "search or enter address",
)


def read_browser_url(hwnd: int, desktop_factory=None) -> str | None:
    """Read the omnibox URL of a browser window over UIA, or ``None``.

    ``None`` means "could not read", which callers treat as comms + strict.
    pywinauto is imported lazily so this costs nothing until it is used.
    """
    try:
        if desktop_factory is None:
            from pywinauto import Desktop

            desktop = Desktop(backend="uia")
        else:
            desktop = desktop_factory()
        window = desktop.window(handle=hwnd)
        for edit in window.descendants(control_type="Edit"):
            name = skeleton(getattr(edit.element_info, "name", "") or "")
            if any(known in name for known in _OMNIBOX_NAMES):
                value = edit.get_value()
                text = strip_invisible(str(value or "")).strip()
                return text or None
    except Exception:
        return None
    return None


_SETTINGS_PAGE_IDS = ("breadcrumb", "pageheader", "pagetitle")


def read_settings_page(hwnd: int, desktop_factory=None) -> str | None:
    """The current Settings page ("Network & internet > Proxy"), or ``None``.

    Read from the breadcrumb bar over UIA, since the window title is just
    "Settings" on every page. ``None`` means "could not tell", which the
    classifier treats as a denied page. The automation ids are a best guess
    until the phase 6 live check; a miss fails closed, never open.
    """
    try:
        if desktop_factory is None:
            from pywinauto import Desktop

            desktop = Desktop(backend="uia")
        else:
            desktop = desktop_factory()
        window = desktop.window(handle=hwnd)
        for element in window.descendants():
            info = element.element_info
            ident = skeleton(f"{getattr(info, 'automation_id', '')} {getattr(info, 'class_name', '')}")
            ident = ident.replace(" ", "").replace("_", "")
            if not any(key in ident for key in _SETTINGS_PAGE_IDS):
                continue
            parts = [
                strip_invisible(getattr(child.element_info, "name", "") or "").strip()
                for child in element.descendants()
            ]
            parts = [part for part in parts if part]
            own = strip_invisible(getattr(info, "name", "") or "").strip()
            page = " > ".join(dict.fromkeys(parts)) or own
            if page:
                return page
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# App names -> the images they run as
# ---------------------------------------------------------------------------

# The friendly names a plan's focus_window / open_app target uses, and every
# image that app is known to run as. focus_window checks the foreground image
# against this after focusing, so "Notepad" can never land on a Discord
# channel called "#notepad".
APP_NAME_IMAGES: dict[str, tuple[str, ...]] = {
    "notepad": ("notepad.exe",),
    "paint": ("mspaint.exe",),
    "calculator": ("CalculatorApp.exe", "calc.exe"),
    "calc": ("CalculatorApp.exe", "calc.exe"),
    "wordpad": ("wordpad.exe",),
    "word": ("WINWORD.EXE",),
    "excel": ("EXCEL.EXE",),
    "powerpoint": ("POWERPNT.EXE",),
    "explorer": ("explorer.exe",),
    "file explorer": ("explorer.exe",),
    "vscode": ("Code.exe",),
    "visual studio code": ("Code.exe",),
    "spotify": ("Spotify.exe",),
    "obsidian": ("Obsidian.exe",),
    "settings": ("SystemSettings.exe",),
    "whatsapp": ("WhatsApp.Root.exe", "WhatsApp.exe"),
    "telegram": ("Telegram.exe",),
    "discord": ("Discord.exe",),
    "teams": ("ms-teams.exe", "Teams.exe"),
    "slack": ("slack.exe",),
    "signal": ("Signal.exe",),
    "outlook": ("olk.exe", "outlook.exe", "HxOutlook.exe"),
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
    "brave": ("brave.exe",),
    "opera": ("opera.exe",),
}


def expected_images(target: str) -> frozenset[str]:
    """Images ``target`` may land on, folded; empty when it names no known app.

    Matched on whole words of the target ("Untitled - Notepad" -> notepad).
    A target naming two apps ("Notepad - WhatsApp") expects both, so the
    check only refuses an image neither of them runs as.
    """
    folded = " ".join(re.findall(r"[a-z0-9+]+", skeleton(target or "")))
    found: set[str] = set()
    for name, images in APP_NAME_IMAGES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", folded):
            found.update(norm_image(image) for image in images)
    return frozenset(found)


__all__ = [
    "APP_NAME_IMAGES",
    "expected_images",
    "APP_BROWSER",
    "APP_COMMS",
    "APP_OTHER",
    "APP_SETTINGS",
    "APP_SHELL",
    "APP_UNKNOWN",
    "BLOCKED_EXTENSIONS",
    "BROWSER_IMAGES",
    "CODE_EXTENSIONS",
    "COMMS_DOMAINS",
    "COMMS_IMAGES",
    "DENIED_HOSTS",
    "DENIED_IMAGES",
    "KEY_BLOCKLIST",
    "KNOWN_LOCAL_IMAGES",
    "LAUNCH_EXTENSIONS",
    "NAV_KEYS",
    "SETTINGS_PAGE_UNKNOWN",
    "SHELL_IMAGES",
    "TAINTED_BROWSER_KEYS",
    "app_class_for",
    "denied_url_reason",
    "file_dialog_refusal",
    "is_comms_image",
    "is_comms_url",
    "is_denied_surface",
    "is_known_local_image",
    "is_nav_keys",
    "is_shell_image",
    "key_refused",
    "launch_extension",
    "norm_image",
    "normalize_chord",
    "path_refusal",
    "read_browser_url",
    "read_settings_page",
    "registrable_domain",
    "settings_title",
    "split_chords",
]
