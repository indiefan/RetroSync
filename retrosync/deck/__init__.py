"""Steam Deck / EmuDeck-specific code paths.

Everything in here is "Linux on the Deck running EmuDeck" and is only
loaded by the deck-side daemon, the wrap subcommand, and the deck
installer. The Pi-side daemon never imports any of these.
"""
