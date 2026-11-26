"""The add-on's names, in one place. A later rename is a change here and to NAME in install.sh.

NAME is the technical name: the command, the folders, the system user, the units, the Godspeed add-on id and
the credential names. DISPLAY_NAME is what a person reads: the page, every alert, `status`, the reminder.
Nothing else in the code spells either one.
"""

NAME = "godspeed-crypto-bot"
DISPLAY_NAME = "Crypto Bot"

CONFIG_DIR = f"/etc/{NAME}"
STATE_DIR = f"/var/lib/{NAME}"
CODE_DIR = f"/opt/{NAME}"
PAGE_DIR = f"{STATE_DIR}-page"  # the bot writes the page here; Godspeed's web address serves it
CREDENTIAL_PREFIX = f"{NAME}.agent-key"
RELAY_DIR = f"{STATE_DIR}-relay"  # root-owned: the relay's own files (status, page address), never in a bot-writable folder
